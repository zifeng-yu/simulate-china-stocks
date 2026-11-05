"""Offline scheduler time, journal, lock and explicit launchd-control contracts."""

import json
import multiprocessing
import plistlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from china_stock_sim.scheduler import launchd
from china_stock_sim.scheduler.planning import plan_schedule
from china_stock_sim.scheduler.state import WorkflowJournal
from china_stock_sim.scheduler.workspace import writer_lock

NOW = '2026-11-01T20:00:00+08:00'


def calendar():
    first = date(2026, 9, 29)
    closed = {'2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07'}
    return [{'date': (day := first + timedelta(days=i)).isoformat(),
             'is_trading_day': int(day.weekday() < 5 and day.isoformat() not in closed)}
            for i in range(45)]


@pytest.mark.parametrize('clock,completed,target,allowed', [
    ('2026-10-30T18:09:59+08:00', '2026-10-29', '2026-10-30', False),
    ('2026-10-30T18:10:00+08:00', '2026-10-30', '2026-11-02', True),
    ('2026-11-01T20:00:00+08:00', '2026-10-30', '2026-11-02', True),
    ('2026-11-02T08:59:59+08:00', '2026-10-30', '2026-11-02', True),
    ('2026-11-02T09:00:00+08:00', '2026-10-30', '2026-11-02', False),
    ('2026-11-02T18:10:00+08:00', '2026-11-02', '2026-11-03', True),
    ('2026-10-30T10:10:00+00:00', '2026-10-30', '2026-11-02', True),
    ('2026-10-07T20:00:00+08:00', '2026-09-30', '2026-10-08', True),
])
def test_explicit_calendar_shanghai_boundaries_and_holidays(clock, completed, target, allowed):
    rows = calendar()
    original = deepcopy(rows)
    result = plan_schedule(clock, rows)
    assert rows == original
    assert result['state'] == 'READY'
    assert result['completed_session'] == completed and result['planning_target'] == target
    assert result['plan_allowed'] is allowed
    assert result['trading_sessions'][-1] == completed
    assert result['planning_deadline'] == target + 'T09:00:00+08:00'


def test_month_end_backlog_and_nearest_priority_wakeup():
    result = plan_schedule('2026-11-05T20:00:00+08:00', calendar())
    assert result['month_end'] is False and result['last_completed_month_end'] == '2026-10-30'
    assert plan_schedule('2026-10-30T18:09:00+08:00', calendar())['next_check_at'].endswith('18:10:00+08:00')
    assert plan_schedule('2026-11-02T08:55:00+08:00', calendar())['next_check_at'].endswith('09:00:00+08:00')


@pytest.mark.parametrize('missing', ['2026-11-01', '2026-10-31', '2026-10-14', '2026-11-02'])
def test_missing_calendar_never_guesses_weekdays(missing):
    result = plan_schedule(NOW, [r for r in calendar() if r['date'] != missing])
    assert result['state'] == 'WAITING_CALENDAR' and not result['plan_allowed']
    assert any(issue.get('date') == missing for issue in result['issues'])


@pytest.mark.parametrize('changes', [{'daily_start': '25:00'}, {'plan_cutoff': '9:00'},
                                      {'awake_check_minutes': True}, {'awake_check_minutes': 0}])
def test_invalid_schedule_contract_is_rejected(changes):
    with pytest.raises(ValueError):
        plan_schedule(NOW, calendar(), **changes)


def test_journal_read_is_zero_write_and_failed_attempts_are_recoverable(settings):
    journal = WorkflowJournal(settings)
    before = set(settings.root.rglob('*'))
    assert journal.read()['state'] == 'NOT_STARTED' and journal.get_task('close:S1:date') is None
    assert set(settings.root.rglob('*')) == before
    with writer_lock(settings):
        journal.start('daily:2026-10-30', now=NOW, metadata={'download': False})
        journal.step('daily:2026-10-30', 'close:S1', now=NOW, state='COMPLETE', result={'revision': 2})
        journal.save_task('close:S1:date:revision2', now=NOW, state='COMPLETE', result={'revision': 2})
        journal.save_task('data', now=NOW, state='WAITING', result={'reason': 'COOLDOWN'},
                          next_retry_at='2026-11-01T20:15:00+08:00')
    restarted = WorkflowJournal(settings)
    restarted.start('daily:2026-10-30', now='2026-11-01T20:16:00+08:00')
    row = restarted.read()['runs']['daily:2026-10-30']
    assert [a['state'] for a in row['attempts']] == ['INTERRUPTED', 'RUNNING']
    assert row['attempts'][0]['steps'][0]['state'] == 'COMPLETE'
    assert restarted.get_task('close:S1:date:revision2')['state'] == 'COMPLETE'
    assert restarted.get_task('data')['next_retry_at'] == '2026-11-01T20:15:00+08:00'
    restarted.finish('daily:2026-10-30', now='2026-11-01T20:17:00+08:00', state='PARTIAL',
                     result={'waiting_accounts': ['S1']})
    assert restarted.read()['state'] == 'PARTIAL'
    with pytest.raises(ValueError):
        restarted.step('daily:2026-10-30', 'late', now=NOW, state='COMPLETE')


def test_journal_atomic_failure_preserves_previous_bytes_and_cleans_temp(settings, monkeypatch):
    journal = WorkflowJournal(settings)
    journal.start('day', now=NOW)
    original = journal.path.read_bytes()
    monkeypatch.setattr('china_stock_sim.scheduler.state.os.replace',
                        lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):
        journal.finish('day', now=NOW, state='COMPLETE')
    assert journal.path.read_bytes() == original
    assert not list(journal.path.parent.glob('.journal-*'))


def test_journal_summary_is_bounded_read_only_and_not_a_pending_queue(settings):
    journal = WorkflowJournal(settings)
    before = set(settings.root.rglob('*'))
    empty = journal.summary()
    assert empty['current_run'] is None and empty['recent_tasks'] == []
    assert set(settings.root.rglob('*')) == before
    journal.start('current', now=NOW)
    journal.step('current', 'collect', now=NOW, state='WAITING', result={'large': list(range(1000))})
    value = journal.read()
    for i in range(60):
        key = f'task:{i:02}'
        record = {'state': 'WAITING', 'recorded_at': NOW, 'next_retry_at': None,
                  'result': {'large': list(range(1000))}}
        value['tasks'][key] = {'key': key, **record, 'history': [record] * 10}
    value['tasks']['task:00']['recorded_at'] = '2026-11-02T01:00:00+08:00'
    value['tasks']['task:59']['recorded_at'] = '2026-11-01T18:00:00+00:00'
    journal._write(value)
    original = journal.path.read_bytes()
    result = journal.summary()
    assert journal.path.read_bytes() == original
    assert result['run_count'] == 1 and result['task_count'] == 60
    assert len(result['recent_tasks']) == 50 and result['recent_tasks_truncated']
    assert result['recent_tasks'][0]['key'] == 'task:59'
    assert result['current_run']['step_count'] == 1
    assert result['current_run']['last_step']['state'] == 'WAITING'
    assert 'not_authoritative_pending_work' in result['task_scope']
    assert len(json.dumps(result)) < 12_000
    assert all(key not in json.dumps(result) for key in ('"history"', '"large"', '"steps"'))


def test_corrupt_journal_is_not_reinitialized(settings):
    journal = WorkflowJournal(settings)
    journal.path.parent.mkdir(parents=True)
    journal.path.write_text('{bad')
    with pytest.raises(ValueError):
        journal.start('new', now=NOW)
    assert journal.path.read_text() == '{bad'


@pytest.mark.parametrize('change', ['run_identity', 'attempt_state', 'task_history'])
def test_valid_json_with_damaged_journal_structure_is_rejected(settings, change):
    journal = WorkflowJournal(settings)
    journal.start('day', now=NOW)
    journal.save_task('task', now=NOW, state='COMPLETE', result={})
    data = journal.read()
    if change == 'run_identity':
        data['runs']['day']['run_id'] = 'other'
    elif change == 'attempt_state':
        data['runs']['day']['attempts'][0]['state'] = 'COMPLETE'
    else:
        data['tasks']['task']['history'] = []
    journal.path.write_text(json.dumps(data))
    before = journal.path.read_bytes()
    with pytest.raises(ValueError):
        journal.read()
    assert journal.path.read_bytes() == before


def test_backwards_wall_clock_cannot_rewrite_runtime_history(settings):
    journal = WorkflowJournal(settings)
    journal.start('day', now=NOW)
    before = journal.path.read_bytes()
    with pytest.raises(ValueError, match='时钟'):
        journal.finish('day', now='2026-11-01T19:59:59+08:00', state='COMPLETE')
    assert journal.path.read_bytes() == before


def test_task_retry_and_state_validation_preserve_known_completion(settings):
    journal = WorkflowJournal(settings)
    journal.save_task('same', now=NOW, state='COMPLETE', result={'value': 1})
    original = journal.path.read_bytes()
    for state, retry in [('UNKNOWN', None), ('WAITING', '2026-11-01T19:00:00+08:00')]:
        with pytest.raises(ValueError):
            journal.save_task('same', now=NOW, state=state, result={}, next_retry_at=retry)
        assert journal.path.read_bytes() == original
    with pytest.raises(ValueError):
        journal.save_task('secret', now=NOW, state='FAILED', result={'api_key': 'must-not-persist'})
    assert journal.path.read_bytes() == original


def _try_lock(settings, queue):
    try:
        with writer_lock(settings):
            queue.put('ACQUIRED')
    except RuntimeError:
        queue.put('BLOCKED')


def test_writer_lock_reenters_only_same_thread_and_releases_after_exception(settings):
    ctx = multiprocessing.get_context('fork')
    queue = ctx.Queue()
    with pytest.raises(RuntimeError, match='outer failure'):
        with writer_lock(settings):
            with writer_lock(settings, reentrant=True):
                assert settings.root.joinpath('data/state/writer.lock').exists()
            process = ctx.Process(target=_try_lock, args=(settings, queue))
            process.start()
            process.join(5)
            assert process.exitcode == 0 and queue.get(timeout=5) == 'BLOCKED'
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_try_lock, settings, queue)
                future.result(timeout=5)
                assert queue.get(timeout=5) == 'BLOCKED'
            raise RuntimeError('outer failure')
    with writer_lock(settings):
        pass
    queue.close()
    queue.join_thread()


@pytest.fixture
def launch_settings(settings, monkeypatch):
    raw = deepcopy(settings.raw)
    raw['schedule']['enabled'] = True
    settings = replace(settings, raw=raw)
    python = settings.root / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    from china_stock_sim.scheduler.entrypoints import launcher_bytes, launcher_path
    for role in ('workflow', 'homepage', 'upload'):
        entry = launcher_path(settings.root, role)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(launcher_bytes(role))
        entry.chmod(0o755)
    monkeypatch.setattr(launchd.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(launchd.shutil, 'which', lambda name: '/bin/launchctl')
    return settings


class LaunchRunner:
    def __init__(self, *, fail=None):
        self.loaded = False
        self.calls = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        command = argv[1]
        code = 0
        if command == 'print':
            code = 0 if self.loaded else 113
        elif command == self.fail:
            code = 5
        elif command == 'bootstrap':
            self.loaded = True
        elif command == 'bootout':
            self.loaded = False
        return subprocess.CompletedProcess(argv, code, stdout='', stderr='private provider detail')


def options(settings, runner):
    return {'agent_directory': settings.root / 'deploy/launchd-test', 'test_mode': True,
            'runner': runner}


def test_render_and_status_are_read_only_absolute_and_secret_free(launch_settings):
    settings, runner = launch_settings, LaunchRunner()
    before = {str(p): p.read_bytes() for p in settings.root.rglob('*') if p.is_file()}
    preview = launchd.render(settings)
    plist = plistlib.loads(preview['content'].encode())
    assert plist['ProgramArguments'][-3:] == ['workflow-run', '--download', '--scheduled']
    assert plist['ProgramArguments'][0] == str(
        settings.root / 'deploy/launchd/bin/A股模拟-业务调度')
    assert plist['WorkingDirectory'] == str(settings.root)
    assert 'StartInterval' not in plist and plist['RunAtLoad'] is True
    assert plist['StartCalendarInterval'] == [
        {'Minute': 0}, {'Minute': 15}, {'Minute': 30}, {'Minute': 45},
        {'Hour': 18, 'Minute': 10}]
    assert 'KeepAlive' not in plist
    result = launchd.status(settings, **options(settings, runner))
    assert result['state'] == 'NOT_INSTALLED'
    assert all(c[1] == 'print' for c in runner.calls)
    assert {str(p): p.read_bytes() for p in settings.root.rglob('*') if p.is_file()} == before
    assert 'private provider detail' not in json.dumps(result)


def test_explicit_install_uninstall_is_idempotent_and_does_not_change_config(launch_settings):
    settings, runner = launch_settings, LaunchRunner()
    original = deepcopy(settings.raw)
    result = launchd.install(settings, **options(settings, runner))
    assert result['state'] == 'INSTALLED' and result['loaded']
    path = Path(result['plist_path'])
    assert path.exists()
    assert launchd.install(settings, **options(settings, runner))['state'] == 'ALREADY_INSTALLED'
    assert sum(c[1] == 'bootstrap' for c in runner.calls) == 1
    assert launchd.uninstall(settings, **options(settings, runner))['state'] == 'UNINSTALLED'
    assert not path.exists()
    assert launchd.uninstall(settings, **options(settings, runner))['state'] == 'ALREADY_UNINSTALLED'
    assert settings.raw == original


def test_failed_bootstrap_removes_only_new_plist_and_bootout_failure_preserves_it(launch_settings):
    settings = launch_settings
    runner = LaunchRunner(fail='bootstrap')
    result = launchd.install(settings, **options(settings, runner))
    assert result['state'] == 'INSTALL_FAILED' and not Path(result['plist_path']).exists()
    runner.fail = None
    result = launchd.install(settings, **options(settings, runner))
    before = Path(result['plist_path']).read_bytes()
    runner.fail = 'bootout'
    failed = launchd.uninstall(settings, **options(settings, runner))
    assert failed['state'] == 'UNINSTALL_FAILED'
    assert Path(result['plist_path']).read_bytes() == before


def test_disabled_schedule_or_unknown_plist_never_bootstraps(launch_settings):
    settings, runner = launch_settings, LaunchRunner()
    settings.raw['schedule']['enabled'] = False
    with pytest.raises(ValueError, match='enabled'):
        launchd.install(settings, **options(settings, runner))
    assert not runner.calls
    settings.raw['schedule']['enabled'] = True
    result = launchd.status(settings, **options(settings, runner))
    path = Path(result['plist_path'])
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({'Label': 'not-our-task'}))
    with pytest.raises(ValueError):
        launchd.install(settings, **options(settings, runner))
    with pytest.raises(ValueError):
        launchd.uninstall(settings, **options(settings, runner))
    assert not any(c[1] in {'bootstrap', 'bootout'} for c in runner.calls)


def test_custom_install_directory_is_confined_to_explicit_isolated_test_root(launch_settings):
    with pytest.raises(ValueError):
        launchd.status(launch_settings, agent_directory='/tmp/escape', test_mode=True)
    with pytest.raises(ValueError):
        launchd.status(launch_settings, agent_directory=launch_settings.root / 'deploy')


@pytest.mark.parametrize('role', ['workflow', 'homepage'])
def test_invalid_named_launcher_blocks_install_and_status_stays_read_only(launch_settings, role):
    from china_stock_sim.scheduler import dashboard_launchd
    from china_stock_sim.scheduler.entrypoints import launcher_path
    module = launchd if role == 'workflow' else dashboard_launchd
    if role == 'homepage':
        (launch_settings.root / 'configs/dashboard.toml').write_bytes(
            (Path(__file__).resolve().parents[1] / 'configs/dashboard.toml').read_bytes())
    runner = LaunchRunner()
    args = options(launch_settings, runner)
    assert module.install(launch_settings, **args)['state'] == 'INSTALLED'
    entry = launcher_path(launch_settings.root, role)
    entry.chmod(0o644)
    before = entry.read_bytes(), entry.stat().st_mode
    status = module.status(launch_settings, **args)
    assert status['state'] == 'CONFIG_DRIFT' and not status['launcher_valid']
    assert status['file_matches'] and status['loaded']
    assert (entry.read_bytes(), entry.stat().st_mode) == before
    runner.calls.clear()
    with pytest.raises(ValueError, match='LAUNCHER_INVALID'):
        module.install(launch_settings, **args)
    assert not runner.calls
