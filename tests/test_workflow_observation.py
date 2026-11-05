"""Whole-stage start visibility without changing financial orchestration."""

from time import monotonic
from types import SimpleNamespace

import pytest

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.application.workflow_observation import stage_outcome
from china_stock_sim.scheduler.state import WorkflowJournal

NOW = '2026-11-02T10:00:00+08:00'


@pytest.mark.parametrize('phase,result,state', [
    ('close', {'receipt': {'state': 'APPLIED'}, 'statement': {
        'accounting_status': 'COMPLETE', 'valuation_status': 'PROVISIONAL'}}, 'WAITING_DATA'),
    ('close', {'statement': {'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL'}}, 'COMPLETE'),
    ('entitlements_before', {'actions': [{'result': {'state': 'FAILED'}}]}, 'FAILED'),
    ('entitlements_after', {'registrations': [], 'registration_wait': 'UNRESOLVED'}, 'WAITING_DATA'),
    ('entitlements_after', {'actions': [{'result': {'state': 'WAITING_DATA'}}]}, 'WAITING_DATA'),
    ('entitlements_before', {'registrations': [], 'actions': []}, 'SKIPPED'),
])
def test_subphase_does_not_confuse_persisted_operation_with_finished_business(phase, result, state):
    assert stage_outcome(phase, result)['state'] == state


def observed_app(settings):
    app = object.__new__(WorkflowApplication)
    app.settings, app.run_id = settings, 'observed'
    app.observing = True
    app.journal = WorkflowJournal(settings)
    app.clock = lambda: NOW
    app.deadline = monotonic() + 60
    app.observation_context = {'business_session': '2026-10-30', 'target_session': '2026-11-02'}
    app.journal.start(app.run_id, now=NOW)
    return app


def phases(app):
    return app.journal.read()['runs'][app.run_id]['attempts'][-1]['phases']


def test_phase_starts_before_expensive_backup_snapshot_and_verification(settings):
    app = observed_app(settings)
    app._accounts = lambda: [{'account_id': 'a:含冒号'}]
    calls = []
    def snapshot(account):
        assert phases(app)[-1]['phase'] == 'backup'
        assert phases(app)[-1]['state'] == 'RUNNING'
        assert account == 'a:含冒号'
        calls.append('revision')
        return {'account_revision': 1, 'report_revision': 2}
    def verify(backup):
        assert phases(app)[-1]['state'] == 'RUNNING'
        assert calls == ['revision'] and backup == 'existing'
        calls.append('verify')
        return {'state': 'READY'}
    app.ledger = SimpleNamespace(revision_state=snapshot, verify_backup=verify,
        report_snapshot=lambda *a: pytest.fail('backup preflight expanded full report'))
    app.journal.get_task = lambda key: {'state': 'COMPLETE', 'result': {'backup_id': 'existing'}}
    assert app._backup('2026-10-30')['state'] == 'SKIPPED'
    assert calls == ['revision', 'verify']
    assert [(p['phase'], p['state']) for p in phases(app)] == [
        ('backup', 'RUNNING'), ('backup', 'SKIPPED')]


def test_exception_preserves_original_failure_and_account_identity(settings):
    app = observed_app(settings)
    def failed():
        raise ValueError('provider error')
    with pytest.raises(ValueError):
        app._observe('input_review', failed, account_id='name:with:colons')
    record = phases(app)[-1]
    assert record['state'] == 'FAILED' and record['account_id'] == 'name:with:colons'
    assert record['session'] == '2026-10-30'


def test_report_nodes_distinct_even_when_both_reuse_existing_report(settings):
    app = observed_app(settings)
    app._accounts = lambda: [{'account_id': 'S1'}]
    calls = []
    def revision(account):
        assert account == 'S1' and phases(app)[-1]['state'] == 'RUNNING'
        calls.append(phases(app)[-1]['phase'])
        return {'report_pending': False}
    app.ledger = SimpleNamespace(revision_state=revision,
        report_snapshot=lambda *a: pytest.fail('report preflight expanded full report'))
    settings.report.parent.mkdir(parents=True)
    settings.report.write_text('existing')
    app._report()
    app._report(phase='report_plan')
    assert calls == ['report_daily', 'report_plan']
    assert [(p['phase'], p['state']) for p in phases(app)][1::2] == [
        ('report_daily', 'SKIPPED'), ('report_plan', 'SKIPPED')]


def test_context_and_phase_do_not_change_legacy_steps_or_financial_database(settings):
    app = observed_app(settings)
    app.journal.context(app.run_id, now=NOW, business_session='2026-10-30', process_birth='birth')
    app._observe('plan', lambda: {'state': 'WAITING', 'reason': 'RETRY_COOLDOWN',
        'next_retry_at': '2026-11-02T10:15:00+08:00'}, account_id='S1')
    attempt = app.journal.read()['runs'][app.run_id]['attempts'][-1]
    assert attempt['metadata']['process_birth'] == 'birth'
    assert attempt['steps'] == [] and not settings.database.exists()
    assert phases(app)[-1]['result']['reason'] == 'RETRY_COOLDOWN'


def test_large_stage_detail_is_bounded_without_changing_actual_result(settings):
    app = observed_app(settings)
    actual = {'state': 'PARTIAL', 'reasons': ['data issue'] * 10000}
    assert app._observe('data', lambda: actual) is actual
    assert phases(app)[-1]['state'] == 'WAITING'
    assert phases(app)[-1]['result']['reason'] == 'PHASE_DETAILS_TRUNCATED'


def test_plan_phase_step_and_actual_call_share_one_fresh_schedule_across_eod(settings):
    app = observed_app(settings)
    old = {'state': 'READY', 'completed_session': '2026-10-30', 'planning_target': '2026-11-02'}
    fresh = {'state': 'READY', 'completed_session': '2026-11-02', 'planning_target': '2026-11-03'}
    schedules = iter([old, old, fresh])
    app._schedule = lambda: next(schedules)
    app._accounts = lambda: [{'account_id': 'S1'}]
    app.ledger = SimpleNamespace(pending_work=lambda account: {'simulation_start_session': '2026-11-02'},
        get_account_policy=lambda *args: {'definition': {'uses_model': False}})
    app._collect = lambda *a, **kw: None
    app._due = lambda *args: []
    app._report = lambda **kw: None
    app._backup = lambda day: None
    app._remaining = lambda: 100
    app.steps = []
    calls, keys = [], []
    def step(key, function, **kwargs):
        keys.append(key)
        return function()
    app._step = step
    app._plan = lambda account, schedule: calls.append(schedule) or {'state': 'WAITING_DATA'}
    app._run(False)
    assert calls == [fresh] and keys == ['plan:2026-11-03:S1']
    assert [(p['session'], p['target_session']) for p in phases(app)] == [
        ('2026-11-02', '2026-11-03'), ('2026-11-02', '2026-11-03')]
