"""An independent fifteen-minute homepage publisher, with no business commands."""

import hashlib
import os
import platform
import plistlib
import re
import shutil
import subprocess

from china_stock_sim.config import local_path
from china_stock_sim.scheduler import launchd
from china_stock_sim.scheduler.calendar_schedule import calendar_entries
from china_stock_sim.scheduler.dashboard import dashboard_settings
from china_stock_sim.scheduler.entrypoints import launcher_path, validate_launcher


def job_label(settings):
    suffix = hashlib.sha256(str(settings.root.resolve()).encode()).hexdigest()[:16]
    return 'local.china-stock-sim.dashboard.' + suffix


def render(settings):
    config = dashboard_settings(settings)
    root = settings.root.resolve()
    plist = {'Label': job_label(settings), 'ProgramArguments': [str(launcher_path(root, 'homepage')),
        '-m', 'china_stock_sim', '--root', str(root), 'dashboard-refresh', '--scheduled'],
        'WorkingDirectory': str(root), 'RunAtLoad': True,
        'StartCalendarInterval': calendar_entries(config['refresh_minutes']),
        'EnvironmentVariables': {'TZ': 'Asia/Shanghai', 'PYTHONUNBUFFERED': '1'},
        'ProcessType': 'Background', 'ThrottleInterval': 60,
        'StandardOutPath': str(local_path(root, 'logs/dashboard-refresh.stdout.log')),
        'StandardErrorPath': str(local_path(root, 'logs/dashboard-refresh.stderr.log'))}
    data = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
    return {'state': 'RENDERED', 'label': plist['Label'], 'enabled': config['enabled'],
            'plist': plist, 'content': data.decode(), 'sha256': hashlib.sha256(data).hexdigest()}


def status(settings, *, agent_directory=None, test_mode=False, runner=subprocess.run):
    expected = render(settings)
    path = launchd._directory(settings, agent_directory, test_mode) / (expected['label'] + '.plist')
    present = path.is_file() and not path.is_symlink()
    matches = present and path.read_bytes() == expected['content'].encode()
    try:
        validate_launcher(settings.root, 'homepage')
        launcher_valid = True
    except ValueError:
        launcher_valid = False
    supported = platform.system() == 'Darwin' and shutil.which('launchctl') is not None
    loaded, disabled, error = None, None, None
    runtime = {'state': 'UNKNOWN', 'pid': None, 'last_exit_code': None}
    if supported:
        try:
            response = launchd._command(['print', f'gui/{os.getuid()}/{expected["label"]}'], runner)
            loaded = response.returncode == 0
            runtime = launchd.runtime_fields(response)
            if response.returncode not in {0, 3, 113}:
                loaded, error = None, 'LAUNCHCTL_STATUS_FAILED'
            flags = launchd._command(['print-disabled', f'gui/{os.getuid()}'], runner)
            if flags.returncode == 0:
                match = re.search(r'"' + re.escape(expected['label']) + r'"\s*=>\s*(true|false)', flags.stdout)
                disabled = bool(match and match[1] == 'true')
            else:
                error = 'LAUNCHCTL_DISABLED_STATE_UNAVAILABLE'
        except (OSError, subprocess.SubprocessError):
            error = 'LAUNCHCTL_STATUS_UNAVAILABLE'
    state = ('UNSUPPORTED_PLATFORM' if not supported else 'STATUS_UNAVAILABLE' if error
             else 'CONFIG_DRIFT' if present and (not matches or not launcher_valid) else 'DISABLED' if disabled
             else 'INSTALLED' if loaded else 'NOT_LOADED' if present else 'NOT_INSTALLED')
    return {'state': state, 'supported': supported, 'enabled': expected['enabled'],
            'label': expected['label'], 'plist_path': str(path), 'installed': present,
            'loaded': loaded, 'disabled': disabled, 'file_matches': matches, 'launcher_valid': launcher_valid,
            'query_error': error, 'expected_sha256': expected['sha256'], 'runtime': runtime}


def install(settings, *, agent_directory=None, test_mode=False, runner=subprocess.run):
    expected = render(settings)
    if not expected['enabled']:
        raise ValueError('请先显式启用首页刷新配置')
    validate_launcher(settings.root, 'homepage')
    python = settings.root / '.venv/bin/python'
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('本项目虚拟环境Python不可执行')
    current = status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner)
    if not current['supported'] or current['query_error']:
        return current
    path = launchd._directory(settings, agent_directory, test_mode) / (expected['label'] + '.plist')
    if path.is_symlink() or current['installed'] and not current['file_matches']:
        raise ValueError('已有首页调度文件与当前配置不符，不覆盖未知任务')
    if current['loaded'] and not current['file_matches']:
        raise ValueError('同名首页任务无匹配安装文件')
    target = f'gui/{os.getuid()}/{expected["label"]}'
    enable = launchd._command(['enable', target], runner)
    if enable.returncode:
        raise RuntimeError('DASHBOARD_ENABLE_FAILED')
    if current['loaded']:
        return {**status(settings, agent_directory=agent_directory, test_mode=test_mode,
                         runner=runner), 'state': 'ALREADY_INSTALLED'}
    local_path(settings.root, 'logs').mkdir(parents=True, exist_ok=True)
    created = not current['installed']
    if created:
        launchd._atomic(path, expected['content'].encode())
    reply = launchd._command(['bootstrap', f'gui/{os.getuid()}', str(path)], runner)
    if reply.returncode:
        if created:
            path.unlink(missing_ok=True)
        return {**current, 'state': 'INSTALL_FAILED', 'reason': 'DASHBOARD_BOOTSTRAP_FAILED'}
    return status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner)


def uninstall(settings, *, agent_directory=None, test_mode=False, runner=subprocess.run):
    current = status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner)
    if not current['supported'] or current['query_error']:
        return current
    path = launchd._directory(settings, agent_directory, test_mode) / (current['label'] + '.plist')
    if path.is_symlink() or current['installed'] and not current['file_matches']:
        raise ValueError('首页调度文件不匹配，不删除未知文件')
    if current['loaded']:
        reply = launchd._command(['bootout', f'gui/{os.getuid()}/{current["label"]}'], runner)
        if reply.returncode:
            return {**current, 'state': 'UNINSTALL_FAILED'}
    path.unlink(missing_ok=True)
    return {**status(settings, agent_directory=agent_directory, test_mode=test_mode,
                     runner=runner), 'state': 'UNINSTALLED'}
