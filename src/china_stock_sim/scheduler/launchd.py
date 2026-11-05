"""Explicit per-user launchd control; rendering and status never install a job."""

import hashlib
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from china_stock_sim.config import local_path
from china_stock_sim.scheduler.calendar_schedule import calendar_entries
from china_stock_sim.scheduler.entrypoints import launcher_path, validate_launcher

from .planning import _clock
from .workspace import writer_lock


def job_label(settings):
    suffix = hashlib.sha256(str(settings.root.resolve()).encode()).hexdigest()[:16]
    return 'local.china-stock-sim.' + suffix


def render(settings):
    schedule = settings.raw['schedule']
    minutes = schedule['awake_check_minutes']
    if type(minutes) is not int or not 1 <= minutes <= 60:
        raise ValueError('清醒检查间隔须为1至60整数分钟')
    daily = _clock(schedule['daily_start'])
    root = settings.root.resolve()
    plist = {'Label': job_label(settings),
        'ProgramArguments': [str(launcher_path(root, 'workflow')), '-m', 'china_stock_sim',
            '--root', str(root), 'workflow-run', '--download', '--scheduled'],
        'WorkingDirectory': str(root), 'RunAtLoad': True,
        'StartCalendarInterval': calendar_entries(minutes, daily_times=(daily,)),
        'EnvironmentVariables': {'TZ': 'Asia/Shanghai', 'PYTHONUNBUFFERED': '1'},
        'ProcessType': 'Background', 'ThrottleInterval': 60,
        'StandardOutPath': str(local_path(root, 'logs/scheduler-launchd.stdout.log')),
        'StandardErrorPath': str(local_path(root, 'logs/scheduler-launchd.stderr.log'))}
    data = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
    return {'state': 'RENDERED', 'label': plist['Label'], 'plist': plist,
            'content': data.decode(), 'sha256': hashlib.sha256(data).hexdigest(),
            'enabled': schedule['enabled'],
            'timezone_note': 'CalendarInterval follows launchd host time; application checks and '
                             'workflow gates use Asia/Shanghai. TZ only affects the launched program.',
            'wake_note': 'RunAtLoad and missed-calendar wake coalescing are configured; no missed tick means no immediate wake trigger. Powered-off '
                         'or logged-out execution and physical wake timing are not guaranteed.'}


def _directory(settings, agent_directory, test_mode):
    if agent_directory is None:
        return Path.home() / 'Library/LaunchAgents'
    actual = Path(__file__).resolve().parents[3]
    directory = Path(agent_directory).resolve()
    if not test_mode or settings.root.resolve() == actual or not directory.is_relative_to(settings.root.resolve()):
        raise ValueError('自定义LaunchAgent目录仅允许隔离项目测试')
    return directory


def _command(arguments, runner):
    return runner(['/bin/launchctl', *arguments], capture_output=True, text=True,
                  timeout=15, check=False)


def runtime_fields(reply):
    """Return only process state and numeric fields, never launchd environment."""
    result = {'state': 'UNKNOWN', 'pid': None, 'last_exit_code': None}
    if reply.returncode:
        return result
    state = re.search(r'^\s*state\s*=\s*([^\n]+)', reply.stdout, re.MULTILINE)
    if state:
        result['state'] = {'running': 'RUNNING', 'not running': 'IDLE',
                           'waiting': 'IDLE'}.get(state[1].strip(), 'UNKNOWN')
    for field, label in [('pid', 'pid'), ('last_exit_code', 'last exit code')]:
        match = re.search(r'^\s*' + label + r'\s*=\s*(-?\d+)\s*$', reply.stdout, re.MULTILINE)
        if match:
            result[field] = int(match[1])
    return result


def status(settings, *, agent_directory=None, test_mode=False, runner=subprocess.run):
    expected = render(settings)
    path = _directory(settings, agent_directory, test_mode) / (expected['label'] + '.plist')
    present = path.is_file() and not path.is_symlink()
    matches = present and path.read_bytes() == expected['content'].encode()
    try:
        validate_launcher(settings.root, 'workflow')
        launcher_valid = True
    except ValueError:
        launcher_valid = False
    supported = platform.system() == 'Darwin' and shutil.which('launchctl') is not None
    loaded, query_error = None, None
    runtime = {'state': 'UNKNOWN', 'pid': None, 'last_exit_code': None}
    if supported:
        try:
            reply = _command(['print', f'gui/{os.getuid()}/{expected["label"]}'], runner)
            loaded = reply.returncode == 0
            runtime = runtime_fields(reply)
            # launchctl uses exit 113 for a missing service, and 3 for a missing domain.
            if reply.returncode not in {0, 3, 113}:
                query_error = 'LAUNCHCTL_STATUS_FAILED'
                loaded = None
        except (OSError, subprocess.SubprocessError):
            query_error = 'LAUNCHCTL_STATUS_UNAVAILABLE'
    state = ('UNSUPPORTED_PLATFORM' if not supported else 'STATUS_UNAVAILABLE' if query_error
             else 'CONFIG_DRIFT' if present and (not matches or not launcher_valid) else 'INSTALLED' if loaded
             else 'NOT_LOADED' if present else 'NOT_INSTALLED')
    return {'state': state, 'supported': supported, 'enabled': settings.raw['schedule']['enabled'],
            'label': expected['label'], 'plist_path': str(path), 'installed': present,
            'loaded': loaded, 'file_matches': matches, 'launcher_valid': launcher_valid, 'query_error': query_error,
            'expected_sha256': expected['sha256'], 'runtime': runtime}


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.launchd-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(settings, *, agent_directory=None, test_mode=False, runner=subprocess.run):
    if settings.raw['schedule']['enabled'] is not True:
        raise ValueError('请先显式启用schedule.enabled；安装不会改配置或激活账户')
    validate_launcher(settings.root, 'workflow')
    python = settings.root / '.venv/bin/python'
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('本项目虚拟环境Python不可执行')
    with writer_lock(settings):
        current = status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner)
        if not current['supported'] or current['query_error']:
            return current
        path = Path(current['plist_path'])
        if path.is_symlink() or current['installed'] and not current['file_matches']:
            raise ValueError('已有调度文件与当前项目配置不符，请先核对并显式卸载')
        if current['loaded']:
            if not current['file_matches']:
                raise ValueError('同名服务已加载但无匹配安装文件，不覆盖未知任务')
            return {**current, 'state': 'ALREADY_INSTALLED'}
        created = not current['installed']
        expected = render(settings)
        local_path(settings.root, 'logs').mkdir(parents=True, exist_ok=True)
        if created:
            _atomic(path, expected['content'].encode())
        try:
            response = _command(['bootstrap', f'gui/{os.getuid()}', str(path)], runner)
            if response.returncode:
                raise RuntimeError('LAUNCHCTL_BOOTSTRAP_FAILED')
        except (OSError, subprocess.SubprocessError, RuntimeError):
            after = status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner)
            if created and not after['loaded'] and path.is_file():
                path.unlink()
            return {**after, 'state': 'INSTALL_FAILED', 'reason': 'LAUNCHCTL_BOOTSTRAP_FAILED'}
        return {**status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner),
                'state': 'INSTALLED'}


def uninstall(settings, *, agent_directory=None, test_mode=False, runner=subprocess.run):
    with writer_lock(settings):
        current = status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner)
        if not current['supported'] or current['query_error']:
            return current
        path = Path(current['plist_path'])
        if path.is_symlink():
            raise ValueError('不通过符号链接删除调度文件')
        if current['installed']:
            existing = plistlib.loads(path.read_bytes())
            if (existing.get('Label') != current['label']
                    or existing.get('WorkingDirectory') != str(settings.root.resolve())
                    or existing.get('ProgramArguments') != render(settings)['plist']['ProgramArguments']):
                raise ValueError('调度文件不属于本项目，不删除未知任务')
        if current['loaded']:
            try:
                response = _command(['bootout', f'gui/{os.getuid()}/{current["label"]}'], runner)
                if response.returncode:
                    return {**current, 'state': 'UNINSTALL_FAILED', 'reason': 'LAUNCHCTL_BOOTOUT_FAILED'}
            except (OSError, subprocess.SubprocessError):
                return {**current, 'state': 'UNINSTALL_FAILED', 'reason': 'LAUNCHCTL_BOOTOUT_FAILED'}
        if current['installed']:
            path.unlink()
        return {**status(settings, agent_directory=agent_directory, test_mode=test_mode, runner=runner),
                'state': 'UNINSTALLED' if current['installed'] or current['loaded'] else 'ALREADY_UNINSTALLED'}
