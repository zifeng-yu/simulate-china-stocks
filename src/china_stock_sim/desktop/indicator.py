"""Explicit build/install operations for the single read-only macOS indicator."""

import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

NAME = 'A股模拟-关机指示'
SOURCES = ('StatusModel.swift', 'ShutdownIndicator.swift')
PROJECT = Path(__file__).resolve().parents[3]
NOTE = '第四个只读常驻显示进程；不执行或替代原三个业务、首页和上传任务'


class IndicatorError(ValueError):
    pass


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _safe(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise IndicatorError('UNSAFE_SYMLINK')
    return path


def _root(root):
    root = _safe(root)
    if not root.is_dir():
        raise IndicatorError('PROJECT_UNAVAILABLE')
    return root.resolve()


def label(root):
    return 'local.china-stock-sim.indicator.' + _digest(str(_root(root)).encode())[:16]


def app_path(root, *, test=False):
    return _safe(_root(root) / 'deploy/macos' / (NAME + ('-测试' if test else '') + '.app'))


def _binary(app):
    return _safe(app / 'Contents/MacOS' / NAME)


def _manifest_path(app):
    # Outside the signed app: hashing a signature inside its own signed resource
    # manifest would create a circular identity.
    return _safe(app.parent / (app.stem + '.build.json'))


def _atomic(path, data):
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _lock(path):
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IndicatorError('INDICATOR_RUNNING_OR_MAINTENANCE_BUSY') from None
        yield
    finally:
        os.close(fd)


def _invoke(command, runner, *, timeout=15):
    return runner(command, capture_output=True, text=True, check=False, timeout=timeout)


def _source_manifest(root):
    return {name: _digest(_safe(root / 'deploy/macos' / name).read_bytes()) for name in SOURCES}


def _build_info(root, *, test, fixture):
    result = {'CFBundleName': NAME + ('-测试' if test else ''), 'CFBundleDisplayName': NAME,
        'CFBundleIdentifier': label(root) + ('.test' if test else ''), 'CFBundleExecutable': NAME,
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0',
        'LSUIElement': True, 'LSMinimumSystemVersion': '13.0',
        'NSHighResolutionCapable': True, 'NSPrincipalClass': 'NSApplication', 'SCSProjectRoot': str(root)}
    if test:
        result['SCSFixturePath'] = str(fixture)
    else:
        result['SCSPythonPath'] = str(root / '.venv/bin/python')
    return result


def _owned_app(root, app, *, test):
    if not app.exists():
        return
    try:
        data = json.loads(_manifest_path(app).read_text())
        expected = label(root) + ('.test' if test else '')
        if data.get('project_root') == str(root) and data.get('bundle_id') == expected:
            return
    except (OSError, ValueError):
        pass
    raise IndicatorError('UNKNOWN_APP_OWNERSHIP')


def build(root, *, runner=subprocess.run, fixture_path=None, test_mode=False):
    """Compile only; never install/start. Test fixture builds cannot be installed."""
    root = _root(root)
    if platform.system() != 'Darwin':
        return {'state': 'UNSUPPORTED_PLATFORM', 'note': NOTE}
    test = fixture_path is not None
    fixture = _safe(fixture_path) if test else None
    if test and (not test_mode or root == PROJECT or not fixture.is_relative_to(root)
                 or not _safe(root / '.shutdown-indicator-test-root').is_file()):
        raise IndicatorError('TEST_ROOT_REQUIRED')
    if not test and not os.access(root / '.venv/bin/python', os.X_OK):
        raise IndicatorError('PYTHON_UNAVAILABLE')
    source_hashes = _source_manifest(root)
    app = app_path(root, test=test)
    _owned_app(root, app, test=test)
    with (_lock(root / 'data/state/shutdown-indicator/install.lock'),
          _lock(root / 'data/state/shutdown-indicator/instance.lock')):
        app.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.indicator-build-', dir=app.parent) as temporary:
            stage = Path(temporary) / app.name
            executable = _binary(stage)
            executable.parent.mkdir(parents=True)
            _manifest_path(stage).parent.mkdir(parents=True, exist_ok=True)
            info = plistlib.dumps(_build_info(root, test=test, fixture=fixture), sort_keys=True)
            (stage / 'Contents/Info.plist').write_bytes(info)
            command = ['/usr/bin/xcrun', 'swiftc', '-O', '-swift-version', '5', '-parse-as-library',
                       '-framework', 'AppKit']
            if test:
                command += ['-D', 'INDICATOR_TEST']
            command += [str(root / 'deploy/macos' / name) for name in SOURCES] + ['-o', str(executable)]
            compiled = _invoke(command, runner, timeout=120)
            if compiled.returncode or not executable.is_file():
                raise IndicatorError('SWIFT_BUILD_FAILED')
            version = _invoke(['/usr/bin/xcrun', 'swiftc', '--version'], runner)
            manifest = {'schema_version': 1, 'project_root': str(root),
                'bundle_id': label(root) + ('.test' if test else ''),
                'mode': 'TEST_FIXTURE' if test else 'PRODUCTION_READ_ONLY',
                'source_sha256': source_hashes, 'binary_sha256': _digest(executable.read_bytes()),
                'info_sha256': _digest(info), 'compiler': version.stdout.strip()[:500], 'note': NOTE}
            signed = _invoke(['/usr/bin/codesign', '--force', '--sign', '-', str(stage)], runner)
            checked = _invoke(['/usr/bin/codesign', '--verify', '--strict', str(stage)], runner)
            if signed.returncode or checked.returncode:
                raise IndicatorError('APP_SIGNATURE_FAILED')
            # Signing can replace Mach-O signature bytes; the external manifest
            # captures the final signature without another signing cycle.
            manifest['binary_sha256'] = _digest(executable.read_bytes())
            old = Path(temporary) / 'previous-app'
            if app.exists():
                os.replace(app, old)
            published = False
            try:
                os.replace(stage, app)
                published = True
                _atomic(_manifest_path(app), (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
            except OSError:
                if published:
                    shutil.rmtree(app)
                if old.exists():
                    os.replace(old, app)
                raise
    return {'state': 'BUILT', 'app_path': str(app), 'binary_path': str(_binary(app)),
            'manifest': manifest, 'installed': False, 'note': NOTE}


def render(root):
    root = _root(root)
    identifier = label(root)
    value = {'Label': identifier, 'ProgramArguments': [str(_binary(app_path(root)))],
        'WorkingDirectory': str(root), 'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 30,
        'LimitLoadToSessionType': 'Aqua', 'AssociatedBundleIdentifiers': [identifier],
        'EnvironmentVariables': {'TZ': 'Asia/Shanghai'}, 'ProcessType': 'Standard'}
    data = plistlib.dumps(value, sort_keys=True)
    return {'label': identifier, 'plist': value, 'content': data.decode(), 'sha256': _digest(data)}


def _directory(root, directory, test_mode):
    if directory is None:
        return _safe(Path.home() / 'Library/LaunchAgents')
    directory = _safe(directory)
    if not test_mode or root == PROJECT or not directory.is_relative_to(root):
        raise IndicatorError('UNSAFE_AGENT_DIRECTORY')
    return directory


def _artifact(root, *, runner):
    app = app_path(root)
    try:
        _owned_app(root, app, test=False)
        manifest = json.loads(_manifest_path(app).read_text())
        info = _safe(app / 'Contents/Info.plist').read_bytes()
        if (manifest.get('mode') != 'PRODUCTION_READ_ONLY'
                or manifest.get('source_sha256') != _source_manifest(root)
                or manifest.get('binary_sha256') != _digest(_binary(app).read_bytes())
                or manifest.get('info_sha256') != _digest(info)
                or plistlib.loads(info) != _build_info(root, test=False, fixture=None)):
            return False
        return _invoke(['/usr/bin/codesign', '--verify', '--strict', str(app)], runner).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def status(root, *, runner=subprocess.run, agent_directory=None, test_mode=False):
    root = _root(root)
    expected = render(root)
    path = _safe(_directory(root, agent_directory, test_mode) / (expected['label'] + '.plist'))
    supported = platform.system() == 'Darwin'
    installed = path.is_file()
    matches = installed and path.read_bytes() == expected['content'].encode()
    loaded, error, pid, enabled = False, None, None, None
    if supported:
        try:
            reply = _invoke(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{expected["label"]}'], runner)
            loaded = reply.returncode == 0
            if reply.returncode not in {0, 3, 113}:
                error = 'LAUNCHCTL_STATUS_FAILED'
            if loaded:
                found = re.search(r'^\s*pid\s*=\s*(\d+)\s*$', reply.stdout, re.MULTILINE)
                pid = int(found[1]) if found else None
            disabled = _invoke(['/bin/launchctl', 'print-disabled', f'gui/{os.getuid()}'], runner)
            if disabled.returncode:
                error = 'LAUNCHCTL_DISABLED_STATUS_FAILED'
            else:
                match = re.search(r'"' + re.escape(expected['label']) + r'"\s*=>\s*(true|false)', disabled.stdout)
                enabled = match is None or match[1] == 'false'
        except (OSError, subprocess.SubprocessError):
            error = 'LAUNCHCTL_STATUS_FAILED'
    artifact = supported and _artifact(root, runner=runner)
    state = ('UNSUPPORTED_PLATFORM' if not supported else 'STATUS_UNAVAILABLE' if error
             else 'CONFIG_DRIFT' if installed and not matches else 'BUILD_REQUIRED' if not artifact
             else 'RUNNING' if loaded and pid else 'LOADED' if loaded
             else 'NOT_LOADED' if installed else 'NOT_INSTALLED')
    latest = None
    latest_path = _safe(root / 'logs/shutdown-indicator/latest.json')
    if latest_path.is_file() and latest_path.stat().st_size <= 256 * 1024:
        try:
            latest = json.loads(latest_path.read_text())
        except (OSError, ValueError):
            pass
    return {'state': state, 'supported': supported, 'label': expected['label'], 'installed': installed,
            'loaded': loaded, 'pid': pid, 'file_matches': matches, 'artifact_matches': artifact,
            'enabled': enabled,
            'query_error': error, 'plist_path': str(path), 'app_path': str(app_path(root)),
            'last_display': latest, 'note': NOTE}


def _owned_plist(root, path):
    if path.exists() and path.read_bytes() != render(root)['content'].encode():
        raise IndicatorError('UNKNOWN_PLIST_OWNERSHIP')


def install(root, *, runner=subprocess.run, agent_directory=None, test_mode=False):
    root = _root(root)
    with _lock(root / 'data/state/shutdown-indicator/install.lock'):
        current = status(root, runner=runner, agent_directory=agent_directory, test_mode=test_mode)
        if not current['supported'] or current['query_error']:
            return current
        path = Path(current['plist_path'])
        _owned_plist(root, path)
        if current['loaded'] and not current['installed']:
            raise IndicatorError('UNKNOWN_LOADED_SERVICE')
        if not current['artifact_matches']:
            raise IndicatorError('BUILD_REQUIRED')
        if current['loaded'] and current['file_matches'] and current['enabled']:
            return {**current, 'state': 'ALREADY_INSTALLED'}
        enabled = _invoke(['/bin/launchctl', 'enable', f'gui/{os.getuid()}/{current["label"]}'], runner)
        if enabled.returncode:
            return {**current, 'state': 'INSTALL_FAILED'}
        if current['loaded']:
            result = status(root, runner=runner, agent_directory=agent_directory, test_mode=test_mode)
            return {**result, 'state': 'INSTALLED' if result['enabled'] else 'INSTALL_FAILED'}
        created = not path.exists()
        _atomic(path, render(root)['content'].encode())
        reply = _invoke(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], runner)
        if reply.returncode:
            after = status(root, runner=runner, agent_directory=agent_directory, test_mode=test_mode)
            if created and not after['loaded'] and not after['query_error']:
                path.unlink(missing_ok=True)
            return {**after, 'state': 'INSTALL_FAILED'}
        result = status(root, runner=runner, agent_directory=agent_directory, test_mode=test_mode)
        return {**result, 'state': 'INSTALLED' if result['loaded'] and result['file_matches'] else 'INSTALL_FAILED'}


def uninstall(root, *, runner=subprocess.run, agent_directory=None, test_mode=False):
    root = _root(root)
    with _lock(root / 'data/state/shutdown-indicator/install.lock'):
        current = status(root, runner=runner, agent_directory=agent_directory, test_mode=test_mode)
        if not current['supported'] or current['query_error']:
            return current
        path = Path(current['plist_path'])
        _owned_plist(root, path)
        if current['loaded']:
            if not current['installed']:
                raise IndicatorError('UNKNOWN_LOADED_SERVICE')
            reply = _invoke(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{current["label"]}'], runner)
            if reply.returncode:
                return {**current, 'state': 'UNINSTALL_FAILED'}
        path.unlink(missing_ok=True)
        result = status(root, runner=runner, agent_directory=agent_directory, test_mode=test_mode)
        return {**result, 'state': 'UNINSTALLED' if not result['loaded'] else 'UNINSTALL_FAILED'}
