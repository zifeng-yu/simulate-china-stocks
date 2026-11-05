"""Independent reproduction of completion-audit gaps; isolated public services only."""

import json
import sqlite3
from pathlib import Path
from time import monotonic

import pytest
from execution_fixtures import (
    CODES,
    FIRST,
    PREVIOUS,
    advance,
    execution_book,
    set_coverage,
    set_quote,
    source,
)
from test_execution_integration import freeze
from test_ledger_cross_layer import put_event
from test_workflow_acceptance import offline_application

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.config import load_settings
from china_stock_sim.ledger.kernel import LedgerError
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import MarketRepository

assert execution_book


def test_accepted_long_workflow_budget_respects_public_collection_limit(execution_book):
    book = execution_book
    root = book.root
    config = root / 'configs/default.toml'
    config.write_text(config.read_text().replace('max_run_seconds = 3600',
        'max_run_seconds = 10000').replace('max_active_collection_seconds = 1800',
                                         'max_active_collection_seconds = 7200'))
    (root / 'configs/preparation.toml').write_text(
        (Path(__file__).resolve().parents[1] / 'configs/preparation.toml').read_text().replace(
            'max_run_seconds = 1800', 'max_run_seconds = 7200'))
    settings = load_settings(root)
    app = WorkflowApplication(settings, market=book.market, ledger=book,
        clock=lambda: book.test_now[0], test_mode=True)
    app.deadline = monotonic() + settings.raw['schedule']['max_run_seconds']
    # This is the same public data adapter used by _collect, with a real updater
    # and no requested symbols/network. A legal workflow config cannot fail admission.
    result = app.data.collect(PREVIOUS, candidates=(), include_indices=False,
                              download=False, budget_seconds=app._data_budget())
    assert result['state'] == 'COMPLETE', result
    assert app._data_budget() <= 7200


def completed_workflow(book):
    freeze(book)
    advance(book, '2026-11-03')
    app = offline_application(book)
    assert app.run()['state'] == 'COMPLETE'
    for day in (FIRST, '2026-11-02', '2026-11-03'):
        statement = book.daily_statement('LLM', day)
        assert statement['accounting_status'] == 'COMPLETE'
        assert statement['valuation_status'] == 'FINAL'
    assert not app.inspect()['accounts'][0].get('due_sessions')
    return app


def test_late_cash_event_is_discovered_after_all_existing_days_were_final(execution_book):
    book = execution_book
    app = completed_workflow(book)
    fills = book.fills('LLM')['rows']
    prior = book.daily_statement('LLM', '2026-11-02')
    cash = book.account_state('LLM', '2026-11-03')['cash_fen']
    assert book.corporate_action_cases('LLM') == []
    advance(book, '2026-11-03', at='21:00:00')
    put_event(book, fetched=book._now().isoformat())
    assert book.market.events(CODES[0], knowledge_cutoff=book._now().isoformat()).ready
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    assert len(book.corporate_action_cases('LLM')) == 1
    assert book.account_state('LLM', '2026-11-03')['cash_fen'] == cash + 10000
    assert book.daily_statement('LLM', '2026-11-02')['statement_revision'] > prior['statement_revision']
    assert book.daily_statement('LLM', '2026-11-02', statement_revision=prior[
        'statement_revision'])['net_assets_fen'] == prior['net_assets_fen']
    assert book.fills('LLM')['rows'] == fills
    before = book.settings.database.read_bytes()
    assert app.run()['state'] == 'COMPLETE'
    assert book.settings.database.read_bytes() == before


def test_late_price_revision_revalues_history_and_next_day_without_retrading(execution_book):
    book = execution_book
    app = completed_workflow(book)
    fills = book.fills('LLM')['rows']
    original = {day: book.daily_statement('LLM', day) for day in ('2026-11-02', '2026-11-03')}
    advance(book, '2026-11-03', at='21:00:00')
    set_quote(book, CODES[0], '2026-11-02', price='11.00', preclose='10.00',
              fetched=book._now().isoformat())
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    revised = book.daily_statement('LLM', '2026-11-02')
    assert revised['market_value_fen'] == original['2026-11-02']['market_value_fen'] + 100000
    following = book.daily_statement('LLM', '2026-11-03')
    assert following['daily_pnl_fen'] == original['2026-11-03']['daily_pnl_fen'] - 100000
    assert book.daily_statement('LLM', '2026-11-02', statement_revision=1) == original['2026-11-02']
    assert book.fills('LLM')['rows'] == fills
    before = book.settings.database.read_bytes()
    assert app.run()['state'] == 'COMPLETE'
    assert book.settings.database.read_bytes() == before


def test_changed_inputs_persist_across_run_limit_and_same_clock_second_change(execution_book):
    book = execution_book
    app = completed_workflow(book)
    original_fills = book.fills('LLM')['rows']
    original_plans = book.report_snapshot('LLM')['plans']
    book.settings.raw['schedule']['max_sessions_per_run'] = 1
    advance(book, '2026-11-03', at='21:00:00')
    for price in ('11.00', '12.00'):
        # Both revisions arrive at the same controlled clock. A previously
        # committed refresh operation must not suppress the next financial state.
        set_quote(book, CODES[0], '2026-11-02', price, fetched=book._now().isoformat())
        result = app.run()
        assert result['state'] == 'PARTIAL' and result['budget_exhausted'], result
        pending = book.pending_work('LLM')
        assert pending['restate_from'] == pending['restate_through'] == '2026-11-03', (price, result)
        later = book.daily_statement('LLM', '2026-11-03')
        assert later['requires_restatement'] and later['net_assets_fen'] is None
        # New application instance reopens the same durable marker.
        app = offline_application(book)
        assert app.run()['state'] == 'COMPLETE'
        assert book.pending_work('LLM')['restate_from'] is None
    assert book.fills('LLM')['rows'] == original_fills
    assert book.report_snapshot('LLM')['plans'] == original_plans
    before = book.settings.database.read_bytes()
    assert app.run()['state'] == 'COMPLETE'
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('change', ['same_close', 'coverage_extension', 'future_event',
                                   'future_conflict', 'nonvaluation_bar_field'])
def test_same_semantics_or_future_facts_do_not_invalidate_old_statements(execution_book, change):
    book = execution_book
    app = completed_workflow(book)
    advance(book, '2026-11-03', at='21:00:00')
    if change == 'same_close':
        set_quote(book, CODES[0], '2026-11-02', '10.0000', fetched=book._now().isoformat())
    elif change == 'nonvaluation_bar_field':
        set_quote(book, CODES[0], '2026-11-02', '10.00', volume=200000,
                  fetched=book._now().isoformat())
    elif change == 'coverage_extension':
        rows = [{'code': CODES[0], 'date': '2026-11-03', 'purpose': 'events',
                 'start': '2025-01-01', 'end': '2026-11-03', 'complete': True,
                 'scope': 'reported_cash_bonus_capital_distributions'}]
        MarketRepository(book.root).put('coverage', rows, source(rows, book._now().isoformat()))
    else:
        row = {'code': 'sh.600000', 'dividPlanAnnounceDate': '2026-11-01',
            'dividPlanDate': '2026-11-03', 'dividRegistDate': '2026-11-06',
            'dividOperateDate': '2026-11-09', 'dividPayDate': '2026-11-10',
            'dividCashPsBeforeTax': '0.1', 'dividStocksPs': '0', 'dividReserveToStockPs': '0'}
        rows = [row, {**row, 'dividCashPsBeforeTax': '0.2'}] if change == 'future_conflict' else [row]
        events = normalize_baostock_events(rows, CODES[0], book._now().isoformat())
        MarketRepository(book.root).put('event', events, source(rows, book._now().isoformat(), 'baostock'))
        if change == 'future_conflict':
            assert book.market.events(CODES[0]).state == 'CONFLICT'
    before = book.settings.database.read_bytes()
    assert book.statement_input_changes('LLM', '2026-11-03')['state'] == 'READY'
    result = book.refresh_statement_inputs('LLM', {'session': '2026-11-03',
        'knowledge_cutoff': book._now().isoformat()}, operation_id='same-semantics',
        expected_revision=book.account_state('LLM', '2026-11-03')['account_revision'])
    assert result['state'] == 'ALREADY_APPLIED' and not result['changed'], result
    assert book.settings.database.read_bytes() == before
    assert app.run()['state'] == 'COMPLETE'
    assert book.settings.database.read_bytes() == before


def test_refresh_rechecks_public_facts_and_rejects_external_change_claim(execution_book):
    book = execution_book
    completed_workflow(book)
    advance(book, '2026-11-03', at='21:00:00')
    before = book.settings.database.read_bytes()
    result = book.refresh_statement_inputs('LLM', {'session': '2026-11-03',
        'knowledge_cutoff': book._now().isoformat(), 'affected_from': FIRST},
        operation_id='invented-change', expected_revision=book.account_state('LLM', FIRST)['account_revision'])
    assert result['state'] == 'INVALID_INPUT'
    assert book.settings.database.read_bytes() == before


def test_default_twenty_session_limit_keeps_durable_restatement_for_next_process(execution_book):
    book = execution_book
    freeze(book)
    through = '2026-11-30'
    advance(book, through)
    sessions = [row['date'] for row in book.market.calendar().data
                if row['is_trading_day'] and FIRST <= row['date'] <= through]
    assert len(sessions) == 22
    for day in sessions:
        if day > '2026-11-04':
            set_quote(book, CODES[0], day)
            set_coverage(book, CODES[0], day)
    book.settings.raw['schedule']['max_sessions_per_run'] = 30
    app = offline_application(book)
    assert app.run()['state'] == 'COMPLETE'
    fills = book.fills('LLM')['rows']
    plans = book.report_snapshot('LLM')['plans']
    book.settings.raw['schedule']['max_sessions_per_run'] = 20
    advance(book, through, at='21:00:00')
    set_quote(book, CODES[0], FIRST, '11.00', fetched=book._now().isoformat())
    result = app.run()
    assert result['state'] == 'PARTIAL' and result['budget_exhausted'], result
    pending = book.pending_work('LLM')
    assert pending['restate_from'] == sessions[20] and pending['restate_through'] == through
    assert book.daily_statement('LLM', through)['requires_restatement']
    assert offline_application(book).run()['state'] == 'COMPLETE'
    assert book.pending_work('LLM')['restate_from'] is None
    assert book.fills('LLM')['rows'] == fills
    assert book.report_snapshot('LLM')['plans'] == plans


@pytest.mark.parametrize('damage', ['unknown_query', 'mismatched_cutoff'])
def test_stored_statement_query_damage_is_rejected_without_financial_mutation(execution_book, damage):
    book = execution_book
    completed_workflow(book)
    with sqlite3.connect(book.settings.database) as db:
        identity, raw = db.execute("SELECT id,data FROM daily_nav WHERE account_id='LLM' ORDER BY id LIMIT 1").fetchone()
        row = json.loads(raw)
        query = row['data_evidence'][0]
        if damage == 'unknown_query':
            query['method'] = 'update'
        else:
            query['kwargs']['knowledge_cutoff'] = '2026-11-03T19:00:00+08:00'
        db.execute("UPDATE daily_nav SET data=? WHERE account_id='LLM' AND id=?", (json.dumps(row), identity))
    before = book.settings.database.read_bytes()
    with pytest.raises(LedgerError) as caught:
        book.statement_input_changes('LLM', '2026-11-03')
    assert caught.value.state == 'INTEGRITY_ERROR'
    assert book.settings.database.read_bytes() == before


def test_history_collection_marks_following_final_day_before_closing_repaired_gap(
        execution_book, monkeypatch):
    book = execution_book
    freeze(book)
    advance(book, FIRST)
    app = offline_application(book)
    assert app.run()['state'] == 'COMPLETE'
    # Construct an isolated previously published gap: A has no source quote,
    # B's own price is final, but B cannot calculate P&L against provisional A.
    with MarketRepository(book.root).connection(write=True) as db:
        db.execute("DELETE FROM observations WHERE kind='bar' AND code=? AND session=?",
                   (CODES[0], '2026-11-02'))
    advance(book, '2026-11-03')
    for account in ('LLM', 'S1'):
        for day in ('2026-11-02', '2026-11-03'):
            app.days.missing_plan(account, day)
            app.days.close(account, day)
    assert book.daily_statement('LLM', '2026-11-02')['valuation_status'] == 'PROVISIONAL'
    following = book.daily_statement('LLM', '2026-11-03')
    assert following['valuation_status'] == 'FINAL' and following['daily_pnl_fen'] is None
    fills = book.fills('LLM')['rows']
    book.settings.raw['schedule']['max_sessions_per_run'] = 1
    book.settings.raw['market']['enabled'] = True
    app.journal.save_task('month-end:' + FIRST, now=book._now(), state='COMPLETE',
                          result={'state': 'COMPLETE', 'completion': True})
    advance(book, '2026-11-03', at='21:00:00')
    collected = []

    def collect(day, *args, **kwargs):
        collected.append(day)
        if day == '2026-11-02':
            set_quote(book, CODES[0], day, '11.00', fetched=book._now().isoformat())
        return {'state': 'COMPLETE', 'completion': True}

    monkeypatch.setattr(app.data, 'collect', collect)
    result = app.run()
    assert result['state'] == 'PARTIAL' and result['budget_exhausted'], result
    assert collected == ['2026-11-03', '2026-11-02']
    pending = book.pending_work('LLM')
    assert pending['restate_from'] == pending['restate_through'] == '2026-11-03'
    restarted = offline_application(book)
    monkeypatch.setattr(restarted.data, 'collect', collect)
    assert restarted.run()['state'] == 'COMPLETE'
    assert book.daily_statement('LLM', '2026-11-03')['daily_pnl_fen'] == -100000
    assert book.pending_work('LLM')['restate_from'] is None
    assert book.fills('LLM')['rows'] == fills
