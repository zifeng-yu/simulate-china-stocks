"""SG: bounded recovery control flow and historical-journal reads."""

import hashlib
import json
from datetime import datetime, timedelta
from time import monotonic
from types import SimpleNamespace

import pytest

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.scheduler.state import WorkflowJournal

NOW = '2026-11-03T12:00:00+08:00'


def _app(settings):
    app = WorkflowApplication(settings, clock=lambda: datetime.fromisoformat(NOW), test_mode=True)
    app.run_id = 'test-run'
    app.deadline = monotonic() + 60
    app.journal.start(app.run_id, now=NOW)
    return app


@pytest.mark.parametrize('stop_before', ['execution', 'after', 'close'])
def test_budget_is_checked_between_financial_stages(settings, stop_before):
    app = _app(settings)
    remaining, called = [1], []
    app._remaining = lambda: remaining[0]

    def stage(name):
        called.append(name)
        if {'before': 'execution', 'execution': 'after', 'after': 'close'}.get(name) == stop_before:
            remaining[0] = 0
        return {'state': 'APPLIED'}

    app.days = SimpleNamespace(before_execution=lambda *a: stage('before'),
        after_execution=lambda *a, **k: stage('after'), close=lambda *a: pytest.fail('late close'))
    app.execution = SimpleNamespace(settle=lambda *a, **k: stage('execution'))
    app.ledger = SimpleNamespace(get_plan_header=lambda *a, **k: {'plan_id': 'p'},
                                 workflow_status=lambda *a, **k: {'orders': []})
    result = app._settle_day('S1', '2026-11-02')
    assert result['budget_exhausted'] is True
    assert called == {'execution': ['before'], 'after': ['before', 'execution'],
                      'close': ['before', 'execution', 'after']}[stop_before]


def test_expired_budget_never_starts_backup_or_report_preflight(settings):
    app = _app(settings)
    app.deadline = monotonic() - 1
    app._accounts = lambda: pytest.fail('expired budget enumerated accounts')
    app.ledger = SimpleNamespace(report_snapshot=lambda *a: pytest.fail('full audit'),
                                 verify_backup=lambda *a: pytest.fail('full backup verification'))
    assert app._backup('2026-11-02')['budget_exhausted']
    assert app._report()['budget_exhausted']


def test_final_due_scan_after_budget_uses_only_metadata(settings):
    app = _app(settings)
    app.observing = True
    app._observe = lambda name, function, **kwargs: function()
    app.deadline = monotonic() - 1
    app.ledger = SimpleNamespace(workflow_status=lambda *a, **k: {
        'simulation_start_session': '2026-11-02', 'restate_from': None, 'orders': [],
        'statements': [{'session': '2026-11-02', 'accounting_status': 'COMPLETE',
                        'valuation_status': 'FINAL'}]},
        report_snapshot=lambda *a: pytest.fail('full audit'),
        statement_input_changes=lambda *a, **k: pytest.fail('late input replay'))
    assert app._due('S1', {'completed_session': '2026-11-02',
                          'trading_sessions': ['2026-11-02']}) == []


def test_unchanged_deterministic_failure_is_not_reexecuted_and_changed_input_can_retry(settings):
    app = _app(settings)
    calls = []

    def broken():
        calls.append(1)
        raise OverflowError('isolated known over-size input')

    first = app._step('settle:day:S1', broken, fingerprint='original')
    assert first['state'] == 'FAILED' and first['deterministic_failure']
    repeated = app._step('settle:day:S1', broken, fingerprint='original')
    assert repeated['reason'] == 'UNCHANGED_DETERMINISTIC_FAILURE' and len(calls) == 1
    changed = app._step('settle:day:S1', lambda: {'state': 'COMPLETE'}, fingerprint='repaired')
    assert changed['state'] == 'COMPLETE'


def test_transient_failure_keeps_cooldown_instead_of_permanent_failure_cache(settings):
    app = _app(settings)

    def network_error():
        raise TimeoutError('offline fixture')

    first = app._step('data', network_error, fingerprint='same')
    assert not first.get('deterministic_failure')
    second = app._step('data', lambda: pytest.fail('cooldown bypassed'), fingerprint='same')
    assert second['reason'] == 'RETRY_COOLDOWN'


def test_journal_legacy_read_is_zero_write_and_explicit_write_archives_exact_bytes(settings):
    journal = WorkflowJournal(settings)
    journal.path.parent.mkdir(parents=True)
    legacy = {'schema_version': 1, 'state': 'NOT_STARTED', 'runs': {}, 'tasks': {}, 'current': None}
    original = (json.dumps(legacy, indent=3) + '\n').encode()
    journal.path.write_bytes(original)
    assert journal.read_current()['state'] == 'NOT_STARTED'
    assert journal.path.read_bytes() == original
    assert not (journal.path.parent / 'history').exists()
    journal.start('new', now=NOW)
    index = json.loads(journal.path.read_bytes())
    assert index['storage_version'] == 2
    archived = settings.root / index['legacy_original']['path']
    assert archived.read_bytes() == original
    assert index['legacy_original']['sha256'] == hashlib.sha256(original).hexdigest()


def test_journal_current_reads_do_not_open_historical_objects(settings, monkeypatch):
    journal = WorkflowJournal(settings)
    now = datetime.fromisoformat(NOW)
    for i in range(55):
        stamp = (now + timedelta(seconds=i)).isoformat()
        journal.start(f'run:{i}', now=stamp)
        journal.save_task('calendar', now=stamp, state='COMPLETE', result={'iteration': i})
        journal.finish(f'run:{i}', now=stamp, state='COMPLETE')
    full = journal.read()
    assert len(full['runs']) == 55 and len(full['tasks']['calendar']['history']) == 55
    before = journal.path.read_bytes()
    monkeypatch.setattr(journal, '_get_object', lambda *a: pytest.fail('hot path opened old history'))
    assert journal.get_task('calendar')['result']['iteration'] == 54
    current = journal.read_current()
    assert len(current['runs']) == 50 and current['run_count'] == 55
    assert journal.summary()['task_count'] == 1
    assert len(journal.get_task('calendar')['history']) == 1
    assert journal.path.read_bytes() == before


def test_corrupt_archived_journal_fails_explicit_audit_without_affecting_current_index(settings):
    journal = WorkflowJournal(settings)
    journal.start('run', now=NOW)
    journal.save_task('task', now=NOW, state='COMPLETE', result={'value': 1})
    journal.save_task('task', now=NOW, state='COMPLETE', result={'value': 2})
    task = journal.get_task('task')
    (settings.root / task['history_ref']['path']).write_bytes(b'{}')
    assert journal.read_current()['tasks']['task']['result'] == {'value': 2}
    with pytest.raises(ValueError, match='摘要'):
        journal.read()
