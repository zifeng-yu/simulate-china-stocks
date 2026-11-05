"""Named entrypoints preserve Python arguments, process identity and termination."""

import json
import os
import selectors
import signal
import stat
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

from china_stock_sim.scheduler.entrypoints import (
    launcher_bytes,
    launcher_path,
    validate_launcher,
)

ROLES = ('workflow', 'homepage', 'upload')
NAMES = ('A股模拟-业务调度', 'A股模拟-首页刷新', 'A股模拟-云端上传')
REPOSITORY = Path(__file__).resolve().parents[1]


def install_fixture(root, role):
    path = launcher_path(root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launcher_bytes(role))
    path.chmod(0o755)
    python = root / '.venv/bin/python'
    python.parent.mkdir(parents=True, exist_ok=True)
    if not python.exists():
        python.symlink_to(sys.executable)
    return validate_launcher(root, role)


@pytest.mark.parametrize(('role', 'name'), zip(ROLES, NAMES, strict=True))
def test_named_paths_are_fixed_and_rendering_has_no_side_effects(tmp_path, role, name):
    root = tmp_path / '尚未存在 模拟项目'
    assert launcher_path(root, role) == root / 'deploy/launchd/bin' / name
    assert not root.exists()


@pytest.mark.parametrize('role', ['other', '../workflow', '', None, [], 'A股模拟-业务调度'])
def test_unknown_roles_cannot_choose_an_executable(tmp_path, role):
    for function in (launcher_path, validate_launcher):
        with pytest.raises(ValueError, match='^UNKNOWN_LAUNCHER_ROLE$'):
            function(tmp_path, role)
    with pytest.raises(ValueError, match='^UNKNOWN_LAUNCHER_ROLE$'):
        launcher_bytes(role)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('role', ROLES)
def test_shipped_launchers_are_exact_regular_executable_templates(role):
    if role == 'upload' and find_spec('china_stock_sim.publishing') is None:
        pytest.skip('Local publishing component is not installed')
    path = validate_launcher(REPOSITORY, role)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert path.read_bytes() == launcher_bytes(role)
    assert not path.suffix and not path.is_symlink()
    result = subprocess.run(['/bin/sh', '-n', str(path)], capture_output=True, timeout=5)
    assert result.returncode == 0


@pytest.mark.parametrize('kind', ['missing', 'not_executable', 'changed_bytes', 'appended_bytes',
                                  'file_symlink', 'parent_symlink', 'directory', 'fifo'])
def test_validation_rejects_uncontrolled_entrypoints_without_repair(tmp_path, kind):
    root = tmp_path / 'root'
    path = install_fixture(root, 'workflow')
    if kind == 'missing':
        path.unlink()
    elif kind == 'not_executable':
        path.chmod(0o644)
    elif kind == 'changed_bytes':
        path.write_bytes(path.read_bytes().replace(b'python', b'pytHon'))
    elif kind == 'appended_bytes':
        path.write_bytes(path.read_bytes() + b'exit 0\n')
    elif kind == 'file_symlink':
        saved = tmp_path / 'saved'
        path.rename(saved)
        path.symlink_to(saved)
    elif kind == 'parent_symlink':
        saved = tmp_path / 'saved-bin'
        path.parent.rename(saved)
        path.parent.symlink_to(saved, target_is_directory=True)
    elif kind == 'directory':
        path.unlink()
        path.mkdir()
    else:
        path.unlink()
        os.mkfifo(path)
    before = path.lstat() if path.exists() or path.is_symlink() else None
    with pytest.raises(ValueError, match='^LAUNCHER_INVALID$'):
        validate_launcher(root, 'workflow')
    after = path.lstat() if path.exists() or path.is_symlink() else None
    assert before == after


@pytest.mark.parametrize('role', ROLES)
def test_original_arguments_and_nonzero_exit_survive_unicode_spaces_and_untrusted_cwd(tmp_path, role):
    root = tmp_path / "含 空格'以及中文 项目"
    path = install_fixture(root, role)
    outside = tmp_path / 'unrelated'
    outside.mkdir()
    # A launcher does not source credentials or depend on shell search paths/CDPATH.
    (root / '.env').write_text('touch SHOULD_NOT_EXIST\n')
    args = ['-m', 'china_stock_sim', '--root', str(root), 'workflow-run', '--download',
            '--scheduled', '', 'one two', '中文', "'double\"quoted'", '$(touch BAD)', '*']
    script = 'import json,os,sys;print(json.dumps([sys.argv[1:],os.getcwd()]));sys.exit(37)'
    result = subprocess.run([str(path), '-c', script, *args], cwd=outside,
                            env={'PATH': '/nonexistent', 'CDPATH': '/nonexistent'},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 37, result.stderr
    actual, cwd = json.loads(result.stdout)
    assert actual == args and Path(cwd) == outside.resolve()
    assert not (outside / 'BAD').exists() and not (outside / 'SHOULD_NOT_EXIST').exists()
    assert not (root / 'SHOULD_NOT_EXIST').exists()
    assert (root / '.venv/bin/python').is_symlink()


@pytest.mark.parametrize('role', ROLES)
def test_exec_keeps_pid_and_delivers_sigterm_to_python_without_shell_parent(tmp_path, role):
    path = install_fixture(tmp_path / '隔离 项目', role)
    script = ('import os,signal,sys,time;'
              'signal.signal(signal.SIGTERM,lambda *_:sys.exit(41));'
              'print(os.getpid(),flush=True);time.sleep(20)')
    child = subprocess.Popen([str(path), '-c', script], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    reported_pid = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(5), 'Python did not start promptly'
            reported_pid = int(child.stdout.readline())
        assert reported_pid == child.pid
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 41 and stdout == '' and stderr == ''
    finally:
        if reported_pid and reported_pid != child.pid:
            try:
                os.kill(reported_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_absent_python_fails_without_falling_back_to_system_or_path(tmp_path):
    root = tmp_path / 'root'
    path = install_fixture(root, 'upload')
    (root / '.venv/bin/python').unlink()
    result = subprocess.run([str(path), '-c', 'raise SystemExit(0)'],
                            capture_output=True, timeout=5)
    assert result.returncode != 0
    assert str(root / '.venv/bin/python').encode() in result.stderr
