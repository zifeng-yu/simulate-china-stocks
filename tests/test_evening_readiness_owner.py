"""Real local flock ownership and observational-sidecar failure isolation."""

import json
import subprocess
import sys

import pytest

from china_stock_sim.readiness import probe, writer_owner
from china_stock_sim.scheduler import workspace

CHILD = '''import json,sys
from china_stock_sim.config import load_settings
from china_stock_sim.scheduler.workspace import writer_lock
settings=load_settings(sys.argv[1])
with writer_lock(settings):
 print((settings.root/'data/state/writer-owner.json').read_text(),flush=True)
 sys.stdin.readline()
'''


def launch(settings):
    process = subprocess.Popen([sys.executable, '-c', CHILD, str(settings.root)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    owner = json.loads(process.stdout.readline())
    return process, owner


def finish(process):
    if process.poll() is None:
        process.communicate('\n', timeout=10)
    assert process.returncode == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='The production owner protocol targets macOS lsof')
def test_real_darwin_owner_confirms_only_the_safe_business_tail(settings):
    process, owner = launch(settings)
    try:
        observed = probe.locks(settings)
        assert observed['writer']['state'] == 'HELD'
        assert observed['writer']['owner'] == process.pid, observed
        assert probe.probe_process(process.pid)['birth'] == owner['process_birth']
        running = {'state': 'WORKING', 'tail_safe': True, 'pid': process.pid,
                   'birth': owner['process_birth'], 'phase': 'backup'}
        value = probe.operational(running, observed, {'active': False})
        assert value['state'] == 'READY' and value['optional_work'] == ['备份仍进行中']
        assert probe.operational({**running, 'tail_safe': False, 'phase': 'execution'},
                                 observed, {'active': False})['state'] == 'UNKNOWN'
        assert probe.operational({**running, 'birth': 'reused-pid'}, observed,
                                 {'active': False})['state'] == 'UNKNOWN'
    finally:
        finish(process)
    assert not (settings.root / writer_owner.OWNER_PATH).exists()
    assert probe.locks(settings)['writer']['state'] == 'FREE'


@pytest.mark.parametrize('damage', ['missing', 'pid', 'fd', 'inode', 'malformed'])
def test_unknown_or_mismatched_owner_never_exempts_a_held_lock(settings, damage):
    process, owner = launch(settings)
    path = settings.root / writer_owner.OWNER_PATH
    try:
        if damage == 'missing':
            path.unlink()
        elif damage == 'malformed':
            path.write_text('{}')
        else:
            changed = dict(owner)
            changed[{'pid': 'pid', 'fd': 'fd', 'inode': 'lock_ino'}[damage]] += 1000
            path.write_text(json.dumps(changed))
        observed = probe.locks(settings)
        assert observed['writer']['state'] == 'HELD' and observed['writer']['owner'] is None
        running = {'state': 'WORKING', 'tail_safe': True, 'pid': process.pid,
                   'birth': owner['process_birth'], 'phase': 'backup'}
        assert probe.operational(running, observed, {'active': False})['state'] == 'UNKNOWN'
    finally:
        finish(process)


def test_reentrant_owner_is_stable_and_exception_cleans_before_unlock(settings):
    path = settings.root / writer_owner.OWNER_PATH
    with pytest.raises(RuntimeError, match='original business error'):
        with workspace.writer_lock(settings):
            original = path.read_bytes()
            with workspace.writer_lock(settings, reentrant=True):
                assert path.read_bytes() == original
            assert path.read_bytes() == original
            raise RuntimeError('original business error')
    assert not path.exists()
    with workspace.writer_lock(settings):
        assert path.exists()
    assert not path.exists()


def test_observation_write_or_cleanup_failure_does_not_change_business_or_lock(settings, monkeypatch):
    def fail(*args):
        raise OSError('observation-only failure')

    monkeypatch.setattr(workspace, 'publish_owner', fail)
    monkeypatch.setattr(workspace, 'clear_owner', fail)
    with workspace.writer_lock(settings):
        assert probe.lock_fact(settings.root / 'data/state/writer.lock')['state'] == 'HELD'
    assert probe.lock_fact(settings.root / 'data/state/writer.lock')['state'] == 'FREE'
    with pytest.raises(ValueError, match='financial result preserved'):
        with workspace.writer_lock(settings):
            raise ValueError('financial result preserved')
    assert probe.lock_fact(settings.root / 'data/state/writer.lock')['state'] == 'FREE'


def test_clear_owner_only_removes_its_own_token(settings):
    with workspace.writer_lock(settings):
        path = settings.root / writer_owner.OWNER_PATH
        original = path.read_bytes()
        writer_owner.clear_owner(settings.root, '0' * 32)
        assert path.read_bytes() == original


def test_writer_sidecar_has_owner_only_permissions(settings):
    with workspace.writer_lock(settings):
        path = settings.root / writer_owner.OWNER_PATH
        assert path.stat().st_mode & 0o777 == 0o600
        value = json.loads(path.read_bytes())
        assert set(value) == {'schema_version', 'pid', 'process_birth', 'token', 'fd', 'lock_dev', 'lock_ino'}
        assert len(path.read_bytes()) <= 2048


def test_successful_publish_but_failed_cleanup_does_not_authorize_unknown_next_writer(settings, monkeypatch):
    monkeypatch.setattr(workspace, 'clear_owner', lambda *a: None)
    path = settings.root / 'data/state/writer.lock'
    with workspace.writer_lock(settings):
        saved = json.loads((settings.root / writer_owner.OWNER_PATH).read_bytes())
    assert (settings.root / writer_owner.OWNER_PATH).exists()
    assert probe.lock_fact(path)['state'] == 'FREE'
    child = subprocess.Popen([sys.executable, '-c', '''import fcntl,sys
with open(sys.argv[1],'a') as stream:
 fcntl.flock(stream,fcntl.LOCK_EX)
 print('locked',flush=True)
 sys.stdin.readline()
''', str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        facts = probe.locks(settings)
        assert facts['writer']['state'] == 'HELD' and facts['writer']['owner'] is None
        journal = {'state': 'WORKING', 'tail_safe': True, 'phase': 'backup',
                   'pid': saved['pid'], 'birth': saved['process_birth']}
        assert probe.operational(journal, facts, {'active': False})['state'] == 'UNKNOWN'
    finally:
        finish(child)
