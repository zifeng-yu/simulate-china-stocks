"""TH same-snapshot history projections; isolated SQLite, no providers or business runs."""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from china_stock_sim.ledger.kernel import LedgerError, Transaction
from china_stock_sim.ledger.service import LedgerService
from china_stock_sim.ledger.trading_history import FIELDS, trading_history_projection

NOW = datetime.fromisoformat('2026-11-04T12:00:00+08:00')


@pytest.fixture
def book(settings):
    service = LedgerService(settings, market=object(), clock=lambda: NOW, test_mode=True)
    service.initialize()
    return service


def put(book, table, rows, account='LLM'):
    with sqlite3.connect(book.settings.database) as db:
        for row in rows:
            row = {**row, 'account_id': account}
            db.execute(f'INSERT INTO {table}(id,account_id,data) VALUES (?,?,?)',
                       (row['id'], account, json.dumps(row)))


def statement(day, revision=1, **extra):
    return {'id': f'{day}:{revision}', 'session': day, 'statement_revision': revision,
            'account_revision': 3, 'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL',
            'net_assets_fen': 5_000_000, 'daily_pnl_fen': 0, 'positions': [], **extra}


def fill(identity, day, *, side='BUY', **extra):
    return {'id': identity, 'fill_id': identity, 'order_id': identity, 'plan_id': 'plan-' + day,
            'code': 'SH600000', 'target_session': day, 'effective_date': day,
            'side': side, 'quantity': 100, 'price': '10', 'amount_fen': 100000,
            'commission_fen': 500, 'stamp_fen': 0, 'transfer_fen': 1, 'fees_fen': 501,
            'net_cash_fen': -100501, 'cost_fen': 0, 'realized_pnl_fen': 0,
            'lot_allocations': [], **extra}


def projection(book, statements=(), account='LLM'):
    return book._read(account, lambda tx: trading_history_projection(
        tx, now=NOW, target_session='2026-11-04', statements=statements))


def test_opt_in_report_keeps_all_old_trades_and_one_separate_boundary(book):
    days = []
    day = date(2026, 7, 1)
    while len(days) < 65:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    put(book, 'daily_nav', [statement(day) for day in days])
    put(book, 'fills', [fill('first', days[0]), fill('last', days[-1])])
    before = book.settings.database.read_bytes()
    old = book.report_snapshot('LLM', mode='recent')
    assert 'trading_history_projection' not in old
    report = book.report_snapshot('LLM', mode='recent', include_trading_history=True)
    history = report.pop('trading_history_projection')
    assert report == old
    assert [row['id'] for row in report['fills']] == ['last']
    assert [row['id'] for row in history['fills']] == ['first', 'last']
    assert len(history['statements']) == 60
    assert [row['session'] for row in history['boundary_statements']] == [days[4]]
    assert days[4] not in [row['session'] for row in report['statements']]
    assert history['scope'] == 'all_time' and history['complete'] is True
    assert all(history[key] == report[key] for key in
               ('snapshot_at', 'account_revision', 'report_revision'))
    assert history['row_counts']['fills'] == 2
    assert book.settings.database.read_bytes() == before


def test_latest_boundary_revision_and_restatement_remain_conservative(book):
    put(book, 'daily_nav', [statement('2026-10-30'), statement('2026-10-30', 2, daily_pnl_fen=400)])
    assert projection(book, [statement('2026-11-02')])['boundary_statements'][0]['daily_pnl_fen'] == 400
    with sqlite3.connect(book.settings.database) as db:
        db.execute("UPDATE account_state SET data=json_set(data,'$.restate_from','2026-10-30') WHERE account_id='LLM'")
    boundary = projection(book, [statement('2026-11-02')])['boundary_statements'][0]
    assert boundary['statement_revision'] == 2
    assert boundary['requires_restatement'] is True
    assert boundary['valuation_status'] == 'PROVISIONAL' and boundary['daily_pnl_fen'] is None


def test_boundary_is_only_saved_predecessor_not_claimed_calendar_continuity(book):
    put(book, 'daily_nav', [statement('2026-10-20')])
    history = projection(book, [statement('2026-11-02')])
    assert history['boundary_statements'][0]['session'] == '2026-10-20'
    assert history['statements'][0]['session'] == '2026-11-02'
    assert projection(book)['boundary_statements'] == []


def test_newest_display_revision_matches_report_not_original_revision(book):
    put(book, 'daily_nav', [statement('2026-11-02'), statement('2026-11-02', 2, daily_pnl_fen=999)])
    report = book.report_snapshot('LLM', mode='recent', include_trading_history=True)
    assert [(r['statement_revision'], r['daily_pnl_fen']) for r in
            report['trading_history_projection']['statements']] == [(2, 999)]


def test_projection_keeps_zero_lots_fifo_links_and_backdated_cost_corrections(book):
    put(book, 'position_lots', [
        {'id': 'a', 'lot_id': 'a', 'code': 'SH600000', 'quantity': 0, 'cost_fen': 0,
         'original_quantity': 100, 'original_cost_fen': 100501, 'acquired_date': '2026-01-02',
         'source_order_id': 'buy-a'},
        {'id': 'b', 'lot_id': 'b', 'code': 'SH600000', 'quantity': 150, 'cost_fen': 180000,
         'original_quantity': 200, 'original_cost_fen': 240000, 'acquired_date': '2026-10-28'},
        {'id': 'rights', 'lot_id': 'rights', 'code': 'SH600000', 'quantity': 10, 'cost_fen': 9000,
         'source_case_id': 'case', 'source_lot_id': 'a', 'source_order_id': None}])
    put(book, 'fills', [fill('cross-lots', '2026-10-30', side='SELL', net_cash_fen=179499,
        cost_fen=160501, realized_pnl_fen=18998, quantity=150,
        lot_allocations=[{'lot_id': 'a', 'quantity': 100, 'cost_fen': 100501},
                         {'lot_id': 'b', 'quantity': 50, 'cost_fen': 60000}])])
    put(book, 'lot_movements', [
        {'id': 'buy', 'lot_id': 'a', 'code': 'SH600000', 'effective_date': '2026-01-02',
         'kind': 'BUY', 'quantity_delta': 100, 'cost_delta_fen': 100501},
        {'id': 'sell', 'lot_id': 'a', 'code': 'SH600000', 'effective_date': '2026-10-30',
         'kind': 'SELL', 'quantity_delta': -100, 'cost_delta_fen': -100501, 'reference_id': 'cross-lots'},
        {'id': 'late', 'lot_id': 'b', 'effective_date': '2026-10-28',
         'kind': 'FEE_COST_ADJUSTMENT', 'quantity_delta': 0, 'cost_delta_fen': -12}])
    put(book, 'corrections', [
        {'id': 'fee', 'kind': 'FEE_ADJUSTMENT', 'status': 'APPLIED', 'reference_id': 'cash-fee',
         'effective_date': '2026-10-28', 'lot_id': 'b', 'delta_fen': 12,
         'transaction_fee_adjustment_fen': -12, 'remaining_cost_adjustment_fen': -9, 'pnl_adjustment_fen': 0},
        {'id': 'fifo-fee', 'kind': 'FIFO_FEE_COST', 'parent_correction_id': 'fee',
         'lot_id': 'b', 'reference_id': 'cross-lots', 'realized_cost_adjustment_fen': 3, 'delta_fen': 0},
        {'id': 'rights-cost', 'case_id': 'case', 'reference_fill_id': 'cross-lots', 'lot_id': 'a',
         'realized_cost_adjustment_fen': 90, 'cash_adjustment_fen': 0,
         'reason': 'LATE_SHARE_COST_ALLOCATION'}])
    result = projection(book)
    assert [r['id'] for r in result['lots']] == ['a', 'b', 'rights']
    assert result['lots'][0]['quantity'] == 0
    assert result['lots'][2]['source_lot_id'] == 'a'
    assert result['fills'][0]['lot_allocations'][1]['cost_fen'] == 60000
    assert [r['id'] for r in result['lot_movements']] == ['buy', 'sell', 'late']
    assert result['lot_movements'][-1]['effective_date'] == '2026-10-28'
    assert result['corrections'][1]['parent_correction_id'] == 'fee'
    assert result['corrections'][2]['kind'] is None
    assert result['corrections'][2]['reason'] == 'LATE_SHARE_COST_ALLOCATION'
    assert result['corrections'][2]['realized_cost_adjustment_fen'] == 90


def test_case_cash_and_tax_projection_keeps_required_links_without_source_evidence(book):
    put(book, 'corporate_action_cases', [{'id': 'case', 'case_id': 'case', 'code': 'SH600000',
        'status': 'REGISTERED', 'event': {'event_id': 'source-event', 'kind': 'DISTRIBUTION',
            'status': 'PROPOSED', 'record_date': None, 'conflicts': [], 'evidence': {'private': 'omitted'}},
        'eligibility': [], 'actions': {'eligibility': {'state': 'NOT_DUE', 'operation_date': '2026-12-01',
                                                     'evidence': {'private': 'omitted'}}}}])
    put(book, 'entitlements', [{'id': 'dividend', 'case_id': 'case', 'code': 'SH600000', 'kind': 'CASH',
        'status': 'PAID', 'recognition_date': '2026-10-28', 'amount_fen': 1000, 'paid_fen': 1000,
        'allocations': [{'lot_id': 'a', 'cash_fen': 1000, 'quantity': 100}],
        'payments': [{'effective_date': '2026-10-30', 'amount_fen': 1000}], 'evidence': {'private': 'omitted'}}])
    put(book, 'tax_items', [{'id': 'tax', 'case_id': 'case', 'code': 'SH600000', 'status': 'WAITING_DATA',
        'assessed_fen': None, 'paid_fen': 0, 'allocations': [{'order_id': 'sell', 'quantity': 100}],
        'assessments': [{'effective_date': '2026-10-30', 'amount_fen': 50, 'lot_id': 'a', 'order_id': 'sell'}],
        'payments': [], 'evidence': {'private': 'omitted'}}])
    with sqlite3.connect(book.settings.database) as db:
        db.execute("INSERT INTO cash_entries VALUES (?,?,?,?,?,?,?,?)", ('cash-fee', 'LLM',
            'FEE_ADJUSTMENT', 12, NOW.isoformat(), '2026-10-28', 'original-fee',
            json.dumps({'correction_id': 'fee', 'private': 'omitted'})))
    result = projection(book)
    assert result['cases'][0]['event']['status'] == 'PROPOSED'
    assert result['cases'][0]['actions']['eligibility'] == {'state': 'NOT_DUE', 'operation_date': '2026-12-01'}
    assert result['entitlements'][0]['allocations'][0]['cash_fen'] == 1000
    assert result['tax_items'][0]['assessed_fen'] is None
    assert result['tax_items'][0]['assessments'][0]['order_id'] == 'sell'
    assert result['cash_entries'][-1]['details'] == {'correction_id': 'fee'}
    assert 'private' not in json.dumps(result)


def test_account_isolation_empty_history_and_exact_counts(book):
    put(book, 'fills', [fill('other', '2026-11-02')], account='S1')
    result = projection(book)
    assert result['fills'] == [] and result['complete'] is True
    assert result['as_of_session'] == '2026-11-04'
    for key, count in result['row_counts'].items():
        assert count == len(result[key])
    assert {row['account_id'] for row in result['cash_entries']} == {'LLM'}


@pytest.mark.parametrize('positions', [None, 'invalid', ['invalid']])
def test_missing_or_invalid_statement_positions_never_become_known_zero(book, positions):
    result = projection(book, [statement('2026-11-03', positions=positions)])
    assert result['complete'] is False and result['state'] == 'PARTIAL'
    assert result['statements'][0]['positions'] == []
    assert result['statements'][0]['valuation_status'] == 'UNAVAILABLE'
    assert result['statements'][0]['daily_pnl_fen'] is None
    assert result['issues'][0]['field'] == 'positions'


def test_narrow_projection_never_decodes_giant_evidence_and_scans_each_table_once(book, monkeypatch):
    padding = 'UNREAD_ORIGINAL_' * 100000
    put(book, 'fills', [fill('big', '2026-01-02', data_evidence={'large': padding})])
    put(book, 'corporate_action_cases', [{'id': 'case', 'case_id': 'case', 'code': 'SH600000',
        'event': {'kind': 'DISTRIBUTION', 'status': 'PROPOSED', 'raw_evidence': padding},
        'eligibility': [], 'actions': {}, 'evidence': padding}])
    original = json.loads
    def bounded(value, *args, **kwargs):
        assert len(value) < 10000, 'a complete evidence object was decoded'
        return original(value, *args, **kwargs)
    monkeypatch.setattr(json, 'loads', bounded)
    def forbidden(*args, **kwargs):
        raise AssertionError('full record decoding is forbidden')
    monkeypatch.setattr(Transaction, 'list', forbidden)
    monkeypatch.setattr(Transaction, 'get', forbidden)
    calls = []
    def read(tx):
        tx.db.set_trace_callback(calls.append)
        return trading_history_projection(tx, now=NOW, target_session='2026-11-04', statements=[])
    result = book._read('LLM', read)
    assert result['fills'][0]['id'] == 'big'
    assert len(json.dumps(result)) < 10000
    for table in FIELDS:
        assert sum(f' FROM {table} WHERE ' in query for query in calls) == 1


def test_public_report_and_history_share_snapshot_during_concurrent_commit(book, monkeypatch):
    import china_stock_sim.ledger.trading_history as module
    with sqlite3.connect(book.settings.database) as db:
        db.execute('PRAGMA journal_mode=WAL')
    original = module.trading_history_projection
    def commit_after_report_read(tx, **kwargs):
        put(book, 'fills', [fill('concurrent', '2026-11-03')])
        with sqlite3.connect(book.settings.database) as db:
            db.execute("UPDATE account_state SET data=json_set(data,'$.account_revision',9,'$.report_revision',10) WHERE account_id='LLM'")
        return original(tx, **kwargs)
    monkeypatch.setattr(module, 'trading_history_projection', commit_after_report_read)
    report = book.report_snapshot('LLM', mode='recent', include_trading_history=True)
    history = report['trading_history_projection']
    assert report['account_revision'] == history['account_revision'] == 0
    assert report['report_revision'] == history['report_revision'] == 0
    assert history['fills'] == []
    assert projection(book)['fills'][0]['id'] == 'concurrent'


def test_invalid_opt_in_is_rejected_without_writing(book):
    before = hashlib.sha256(book.settings.database.read_bytes()).hexdigest()
    with pytest.raises(LedgerError, match='布尔'):
        book.report_snapshot('LLM', include_trading_history=1)
    assert hashlib.sha256(book.settings.database.read_bytes()).hexdigest() == before
