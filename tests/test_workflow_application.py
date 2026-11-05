"""Whole workflow acceptance against real isolated public services."""

from datetime import datetime, timedelta

import pytest
from execution_fixtures import FIRST, PREVIOUS, advance, execution_book
from test_strategy_acceptance import strategy_world, tree_state

from china_stock_sim.application.workflow import WorkflowApplication

assert execution_book and strategy_world


def application(world, **kwargs):
    book, make_strategy, model = world
    raw = book.settings.raw
    raw['market']['enabled'] = False
    raw['execution']['enabled'] = True
    for value in raw['strategies'].values():
        value['enabled'] = True
    app = WorkflowApplication(book.settings, market=book.market, ledger=book,
        strategy=make_strategy(), clock=lambda: book.test_now[0], test_mode=True, **kwargs)
    return book, app, model


def test_inspect_has_no_writes_or_models_and_run_disabled_without_side_effects(strategy_world):
    book, make_strategy, model = strategy_world
    app = WorkflowApplication(book.settings, market=book.market, ledger=book,
        strategy=make_strategy(), clock=lambda: book.test_now[0], test_mode=True)
    before = tree_state(book.root)
    result = app.inspect()
    assert result['state'] == 'READY', result
    assert result['schedule']['planning_target'] == FIRST
    assert app.run()['state'] == 'DISABLED'
    assert app.run(scheduled=True)['state'] == 'DISABLED'
    assert tree_state(book.root) == before
    assert model.calls == []


def test_three_accounts_full_workflow_freeze_settle_report_backup_and_replay(strategy_world):
    book, app, model = application(strategy_world)
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    for account in ('S1', 'S1-LT', 'LLM'):
        assert book.get_plan(account, target_session=FIRST)
    assert len(model.calls) == 1
    first_db = book.settings.database.read_bytes()
    backups = list((book.root / 'data/backups/ledger').glob('daily-*'))
    repeated = app.run()
    assert repeated['state'] == 'COMPLETE', repeated
    assert book.settings.database.read_bytes() == first_db
    assert list((book.root / 'data/backups/ledger').glob('daily-*')) == backups
    assert len(model.calls) == 1
    advance(book, FIRST)
    # Keep this test about the original cycle; next-day strategies are independently tested.
    for value in book.settings.raw['strategies'].values():
        value['enabled'] = False
    settled = app.run()
    assert settled['state'] == 'COMPLETE', settled
    for account in ('S1', 'S1-LT', 'LLM'):
        row = book.daily_statement(account, FIRST)
        assert row['accounting_status'] == 'COMPLETE' and row['valuation_status'] == 'FINAL'
        assert book.fills(account)['total'] > 0
        assert not book.pending_work(account)['report_pending']
    before = book.settings.database.read_bytes()
    app.run()
    assert book.settings.database.read_bytes() == before
    assert len(model.calls) == 1


def test_after_cutoff_never_builds_historical_or_tomorrow_plan(strategy_world):
    book, app, model = application(strategy_world)
    advance(book, FIRST, at='09:00:00')
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    assert model.calls == []
    for account in ('S1', 'S1-LT', 'LLM'):
        assert book.get_plan(account, target_session=FIRST) is None
        assert book.get_plan(account, target_session='2026-11-02') is None
        assert any(s.get('kind') == 'NO_PLAN' and s['status'] == 'MISSED_CUTOFF'
                   for s in book.report_snapshot(account, FIRST)['steps'])


def test_offline_days_close_without_invented_orders_then_plan_only_future(strategy_world):
    book, app, model = application(strategy_world)
    advance(book, '2026-11-03')
    for value in book.settings.raw['strategies'].values():
        value['enabled'] = False
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    for account in ('S1', 'S1-LT', 'LLM'):
        for day in (FIRST, '2026-11-02', '2026-11-03'):
            assert book.daily_statement(account, day)['accounting_status'] == 'COMPLETE'
            assert book.get_plan(account, target_session=day) is None
        assert not book.fills(account)['rows']
    assert not model.calls


def test_budget_stops_at_date_boundary_and_next_run_continues(strategy_world):
    book, app, _ = application(strategy_world)
    advance(book, '2026-11-03')
    book.settings.raw['schedule']['max_sessions_per_run'] = 1
    for value in book.settings.raw['strategies'].values():
        value['enabled'] = False
    first = app.run()
    assert first['state'] == 'PARTIAL' and first['budget_exhausted']
    assert book.daily_statement('S1', FIRST)
    assert not book.daily_statement('S1', '2026-11-02')
    app.run()
    assert book.daily_statement('S1', '2026-11-02')
    assert not book.daily_statement('S1', '2026-11-03')
    assert app.run()['state'] == 'COMPLETE'


def test_crash_after_financial_commit_recovers_without_duplicate_fills(strategy_world, monkeypatch):
    book, app, model = application(strategy_world)
    app.run()
    advance(book, FIRST)
    for value in book.settings.raw['strategies'].values():
        value['enabled'] = False
    original = app.days.close
    calls = []

    def crash_once(account, day):
        if not calls:
            calls.append(account)
            raise KeyboardInterrupt('isolated process interruption after execution')
        return original(account, day)

    monkeypatch.setattr(app.days, 'close', crash_once)
    with pytest.raises(KeyboardInterrupt):
        app.run()
    count = book.fills(calls[0])['total']
    assert count > 0
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    assert book.fills(calls[0])['total'] == count
    assert any(r['state'] == 'INTERRUPTED' for r in app.journal.read()['runs'].values())
    assert len(model.calls) == 1


def test_report_failure_keeps_committed_plan_and_recovers_without_model_reask(strategy_world):
    book, app, model = application(strategy_world)
    original = app.renderer

    def broken(*args, **kwargs):
        raise OSError('private provider text must never enter journal')

    app.renderer = broken
    result = app.run()
    assert result['state'] == 'PARTIAL'
    assert book.get_plan('LLM', target_session=FIRST)
    assert len(model.calls) == 1
    assert 'private provider text' not in app.journal.path.read_text()
    app.renderer = original
    book.test_now[0] += timedelta(minutes=16)
    recovered = app.run()
    assert recovered['state'] == 'COMPLETE', recovered
    assert len(model.calls) == 1
    assert not book.pending_work('LLM')['report_pending']


def test_disabled_execution_does_not_skip_old_accounting_to_create_new_plan(strategy_world):
    book, app, model = application(strategy_world)
    advance(book, FIRST)
    book.settings.raw['execution']['enabled'] = False
    result = app.run()
    assert result['state'] == 'PARTIAL'
    assert not book.daily_statement('S1', FIRST)
    assert not book.get_plan('S1', target_session='2026-11-02')
    assert not model.calls


def test_data_time_crossing_cutoff_is_rechecked_before_strategy(strategy_world):
    book, app, model = application(strategy_world)
    book.test_now[0] = datetime.fromisoformat(FIRST + 'T08:59:59+08:00')
    original = app._report

    def cross_cutoff(**kwargs):
        book.test_now[0] = datetime.fromisoformat(FIRST + 'T09:00:00+08:00')
        return original(**kwargs)

    app._report = cross_cutoff
    app.run()
    assert not book.get_plan('LLM', target_session=FIRST)
    assert not model.calls
    assert PREVIOUS < FIRST
