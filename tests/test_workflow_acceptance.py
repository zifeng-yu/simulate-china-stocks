"""Independent workflow acceptance through real public market and ledger services."""

import json
from datetime import timedelta

from execution_fixtures import (
    CODES,
    FIRST,
    advance,
    execution_book,
    set_event_settlement_dates,
    source,
)
from test_execution_integration import freeze, settle
from test_ledger_cross_layer import put_event
from test_ledger_trading import call

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.application.workflow_ledger import LedgerWorkflow
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import MarketRepository

assert execution_book


def workflow_ledger(book):
    return LedgerWorkflow(book.settings, ledger=book, market=book.market,
                          clock=lambda: book.test_now[0])


def buy_first(book):
    plan = freeze(book)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    return plan


def assert_no_failed(value):
    if isinstance(value, dict):
        assert value.get('state') != 'FAILED', value
        for item in value.values():
            assert_no_failed(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_failed(item)


def test_workflow_real_dividend_bonus_before_sell_and_once_only(execution_book):
    """WF12/13/14/16: mature shares precede original sell, cash follows it."""
    book = execution_book
    event = put_event(book, bonus='0.1', face='1')
    set_event_settlement_dates(book, event, payment_date='2026-11-02',
                               share_listing_date='2026-11-02')
    flow = workflow_ledger(book)
    buy_first(book)
    assert flow.before_execution('LLM', FIRST)['registrations'] == []
    assert book.corporate_action_cases('LLM') == []
    assert_no_failed(flow.after_execution('LLM', FIRST))
    case = book.corporate_action_cases('LLM')[0]
    assert sum(row['quantity'] for row in case['eligibility']) == 1000
    assert flow.close('LLM', FIRST)['statement']['accounting_status'] == 'COMPLETE'
    sale = freeze(book, target='2026-11-02', intents=[{
        'code': CODES[0], 'side': 'SELL', 'quantity': 1000, 'limit_price': '9.50',
        'order_index': 0, 'reason': 'original quantity cannot expand after bonus'}])
    original_cost = sum(row['cost_fen'] for row in book.lots('LLM'))
    cash_before = book.account_state('LLM', FIRST)['cash_fen']
    advance(book, '2026-11-02')
    assert_no_failed(flow.before_execution('LLM', '2026-11-02'))
    lots = book.lots('LLM')
    assert sum(row['quantity'] for row in lots) == 1100
    assert sum(row['cost_fen'] for row in lots) == original_cost
    assert book.account_state('LLM', '2026-11-02')['cash_fen'] == cash_before
    assert settle(book, sale)['completion_status'] == 'COMPLETE'
    assert_no_failed(flow.after_execution('LLM', '2026-11-02'))
    assert sum(row['quantity'] for row in book.lots('LLM')) == 100
    dividends = [row for row in book.report_snapshot('LLM')['cash_entries']
                 if row['kind'] == 'DIVIDEND']
    assert len(dividends) == 1 and dividends[0]['amount_fen'] == 10000
    before = book.report_snapshot('LLM')
    assert_no_failed(flow.before_execution('LLM', '2026-11-02'))
    assert_no_failed(flow.after_execution('LLM', '2026-11-02'))
    after = book.report_snapshot('LLM')
    assert after['cash_entries'] == before['cash_entries']
    assert after['lots'] == before['lots']
    assert after['entitlements'] == before['entitlements']
    assert book.fills('LLM')['total'] == 2
    assert book.corporate_action_cases('S1') == []


def test_workflow_record_day_waits_for_orders_then_includes_purchase(execution_book):
    """WF15: only the real completed record-date purchase grants entitlement."""
    book = execution_book
    put_event(book)
    flow = workflow_ledger(book)
    plan = freeze(book)
    advance(book, FIRST)
    assert flow.before_execution('LLM', FIRST)['registrations'] == []
    waiting = flow.after_execution('LLM', FIRST, orders_complete=False)
    assert waiting['registration_wait'] == 'TARGET_SESSION_ORDERS_UNRESOLVED'
    assert book.corporate_action_cases('LLM') == []
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert_no_failed(flow.after_execution('LLM', FIRST))
    assert sum(row['quantity'] for row in book.corporate_action_cases('LLM')[0][
        'eligibility']) == 1000
    assert book.report_snapshot('LLM')['entitlements'] == []


def test_workflow_record_day_sale_reduces_true_eligibility(execution_book):
    """WF15: register the post-sale 700 shares, not pre-trade 1000 shares."""
    book = execution_book
    flow = workflow_ledger(book)
    buy_first(book)
    assert flow.close('LLM', FIRST)['statement']['accounting_status'] == 'COMPLETE'
    fetched = FIRST + 'T19:00:00+08:00'
    raw = {'code': 'sh.600000', 'dividPlanAnnounceDate': '2026-09-01',
        'dividPlanDate': '2026-10-29', 'dividRegistDate': '2026-11-02',
        'dividOperateDate': '2026-11-03', 'dividPayDate': '2026-11-04',
        'dividCashPsBeforeTax': '0.1', 'dividStocksPs': '0', 'dividReserveToStockPs': '0'}
    event = normalize_baostock_events([raw], CODES[0], fetched)[0]
    MarketRepository(book.root).put('event', [event], source([raw], fetched, 'baostock'))
    sale = freeze(book, target='2026-11-02', intents=[{
        'code': CODES[0], 'side': 'SELL', 'quantity': 300, 'limit_price': '10.00',
        'order_index': 0, 'reason': 'record-day partial sale'}])
    advance(book, '2026-11-02')
    assert flow.before_execution('LLM', '2026-11-02')['registrations'] == []
    assert book.corporate_action_cases('LLM') == []
    assert settle(book, sale)['completion_status'] == 'COMPLETE'
    assert_no_failed(flow.after_execution('LLM', '2026-11-02'))
    assert sum(row['quantity'] for row in book.corporate_action_cases('LLM')[0][
        'eligibility']) == 700


def test_workflow_future_delivery_is_receivable_not_sellable(execution_book):
    """WF12/16: known future listing/payment stays ordinary pending work."""
    book = execution_book
    put_event(book, bonus='0.1', face='1')
    flow = workflow_ledger(book)
    buy_first(book)
    assert_no_failed(flow.after_execution('LLM', FIRST))
    flow.close('LLM', FIRST)
    advance(book, '2026-11-02')
    assert_no_failed(flow.before_execution('LLM', '2026-11-02'))
    assert_no_failed(flow.after_execution('LLM', '2026-11-02'))
    snapshot = book.report_snapshot('LLM')
    assert sum(row['quantity'] for row in snapshot['lots']) == 1000
    assert len(snapshot['entitlements']) == 2
    assert {row['status'] for row in snapshot['entitlements']} == {'RECEIVABLE'}
    assert not [row for row in snapshot['cash_entries'] if row['kind'] == 'DIVIDEND']
    statement = flow.close('LLM', '2026-11-02')['statement']
    assert statement['accounting_status'] == 'COMPLETE'


def test_workflow_repeated_eligibility_with_later_clock_is_no_financial_revision(execution_book):
    """WF05/25: an unchanged source must not invalidate a subsequent frozen input."""
    book = execution_book
    put_event(book)
    flow = workflow_ledger(book)
    buy_first(book)
    assert_no_failed(flow.after_execution('LLM', FIRST))
    before = book.report_snapshot('LLM')
    advance(book, FIRST, at='20:15:00')
    assert_no_failed(flow.after_execution('LLM', FIRST))
    after = book.report_snapshot('LLM')
    assert after['account_revision'] == before['account_revision']
    assert after['report_revision'] == before['report_revision']
    assert after['cases'] == before['cases']


def test_workflow_no_positions_does_not_turn_ready_pool_events_into_cases(execution_book):
    """WF16: readiness is permission to consume, never an instruction to book."""
    book = execution_book
    put_event(book)
    advance(book, FIRST)
    before = book.settings.database.read_bytes()
    flow = workflow_ledger(book)
    assert flow.before_execution('S1', FIRST) == {'registrations': [], 'actions': []}
    assert flow.after_execution('S1', FIRST) == {'registrations': [], 'actions': []}
    assert book.settings.database.read_bytes() == before
    assert book.corporate_action_cases('S1') == []


def offline_application(book):
    book.settings.raw['execution']['enabled'] = True
    return WorkflowApplication(book.settings, market=book.market, ledger=book,
                               clock=lambda: book.test_now[0], test_mode=True)


def test_workflow_one_account_failure_does_not_block_other_account(execution_book, monkeypatch):
    """WF22/23: controlled boundary failure, real independent close/report/backup."""
    book = execution_book
    app = offline_application(book)
    advance(book, FIRST)
    original = app.days.before_execution

    def failed_account(account, day):
        if account == 'LLM':
            raise OSError('isolated private source detail')
        return original(account, day)

    monkeypatch.setattr(app.days, 'before_execution', failed_account)
    first = app.run()
    assert first['state'] == 'PARTIAL', first
    assert book.daily_statement('LLM', FIRST) is None
    assert book.daily_statement('S1', FIRST)['accounting_status'] == 'COMPLETE'
    independent = book.report_snapshot('S1')
    assert 'isolated private source detail' not in app.journal.path.read_text()
    monkeypatch.setattr(app.days, 'before_execution', original)
    blocked = app.run()
    assert any(row.get('reason') == 'RETRY_COOLDOWN' for row in blocked['steps'])
    assert book.daily_statement('LLM', FIRST) is None
    book.test_now[0] += timedelta(minutes=16)
    assert app.run()['state'] == 'COMPLETE'
    assert book.daily_statement('LLM', FIRST)['accounting_status'] == 'COMPLETE'
    assert book.report_snapshot('S1')['account_revision'] == independent['account_revision']
    assert book.fills('LLM')['total'] == book.fills('S1')['total'] == 0


def test_workflow_partial_backup_waits_then_complete_backup_is_deduplicated(
        execution_book, monkeypatch):
    """WF27: real backup writer sees a controlled unavailable evidence inventory."""
    book = execution_book
    app = offline_application(book)
    advance(book, FIRST)
    original = book._evidence_inventory

    def unavailable_evidence(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, 'state': 'PARTIAL', 'issues': [*result['issues'],
            {'kind': 'ISOLATED_EVIDENCE_UNAVAILABLE'}]}

    monkeypatch.setattr(book, '_evidence_inventory', unavailable_evidence)
    assert app.run()['state'] == 'PARTIAL'
    folder = book.root / 'data/backups/ledger'
    manifests = sorted(folder.glob('daily-*/manifest.json'))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())['state'] == 'PARTIAL'
    tasks = [row for key, row in app.journal.read()['tasks'].items() if key.startswith('backup:')]
    assert len(tasks) == 1 and tasks[0]['state'] != 'COMPLETE'
    monkeypatch.setattr(book, '_evidence_inventory', original)
    assert app.run()['state'] == 'PARTIAL'
    assert sorted(folder.glob('daily-*/manifest.json')) == manifests
    book.test_now[0] += timedelta(minutes=16)
    assert app.run()['state'] == 'COMPLETE'
    final = sorted(folder.glob('daily-*/manifest.json'))
    assert len(final) == 2
    assert sorted(json.loads(p.read_text())['state'] for p in final) == ['COMPLETE', 'PARTIAL']
    before = book.settings.database.read_bytes()
    book.test_now[0] += timedelta(minutes=16)
    assert app.run()['state'] == 'COMPLETE'
    assert sorted(folder.glob('daily-*/manifest.json')) == final
    assert book.settings.database.read_bytes() == before


def test_workflow_changed_registered_terms_remain_explicit_conflict(execution_book):
    """WF16: skipping unchanged registration must not hide a genuine revision."""
    book = execution_book
    put_event(book)
    flow = workflow_ledger(book)
    buy_first(book)
    assert_no_failed(flow.after_execution('LLM', FIRST))
    previous = book.corporate_action_cases('LLM')[0]
    put_event(book, cash='0.2', fetched=FIRST + 'T20:16:00+08:00')
    advance(book, FIRST, at='20:30:00')
    assert_no_failed(flow.after_execution('LLM', FIRST))
    cases = book.corporate_action_cases('LLM')
    assert len(cases) == 1
    assert cases[0]['case_id'] == previous['case_id']
    assert cases[0]['status'] == 'REVISION_PENDING'
    assert cases[0]['event']['cash_per_share'] == '0.1'
    assert cases[0]['revision_candidate']['cash_per_share'] == '0.2'
    assert book.report_snapshot('LLM')['entitlements'] == []


def test_workflow_internal_eligibility_failure_is_pending_and_recovers(
        execution_book, monkeypatch):
    """WF16/22: a swallowed source failure cannot become a successful day forever."""
    book = execution_book
    put_event(book)
    freeze(book)
    advance(book, FIRST)
    app = offline_application(book)
    original = book.market.event_settlement

    def unavailable(*args, **kwargs):
        raise OSError('isolated event query unavailable')

    monkeypatch.setattr(book.market, 'event_settlement', unavailable)
    result = app.run()
    assert result['state'] == 'PARTIAL', result
    assert book.daily_statement('S1', FIRST)['accounting_status'] == 'COMPLETE'
    assert book.corporate_action_cases('LLM') == []
    assert book.fills('LLM')['total'] == 1
    monkeypatch.setattr(book.market, 'event_settlement', original)
    book.test_now[0] += timedelta(minutes=16)
    assert app.run()['state'] == 'COMPLETE'
    cases = book.corporate_action_cases('LLM')
    assert len(cases) == 1 and cases[0]['actions']['eligibility']['state'] == 'APPLIED'
    assert book.fills('LLM')['total'] == 1


def test_workflow_incomplete_calendar_cannot_skip_observation_start(execution_book, monkeypatch):
    """WF03/11: a known recent calendar does not prove missing earlier account days."""
    book = execution_book
    advance(book, '2026-11-03')
    app = offline_application(book)
    original = book.market.calendar

    def recent_only(*args, **kwargs):
        result = original(*args, **kwargs)
        rows = [row for row in result.data if row['date'] >= '2026-11-02']
        return DataResult(result.state, rows, result.issues, result.observation_ids)

    monkeypatch.setattr(book.market, 'calendar', recent_only)
    result = app.run()
    assert result['state'] == 'PARTIAL', result
    for account in ('LLM', 'S1'):
        snapshot = book.report_snapshot(account)
        assert snapshot['statements'] == []
        assert not [row for row in snapshot['steps'] if row.get('kind') == 'NO_PLAN']
        assert snapshot['plans'] == []


def test_workflow_acknowledges_only_revision_captured_by_real_renderer(execution_book):
    """WF25: a new audit revision during publishing must remain report pending."""
    book = execution_book
    app = offline_application(book)
    advance(book, FIRST)
    app.days.close('LLM', FIRST)
    app.days.close('S1', FIRST)
    original = app.renderer
    rendered = []

    def changed_after_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        rendered.append(next(row['report_revision'] for row in result['published']
                             if row['account_id'] == 'LLM'))
        call(book, 'record_step_result', {'effective_date': FIRST, 'step': 'isolated-audit',
            'status': 'SUCCEEDED'}, account='LLM')
        return result

    app.renderer = changed_after_capture
    assert app._publish()['state'] == 'COMPLETE'
    pending = book.pending_work('LLM')
    assert pending['report_pending'] is True
    snapshot = book.report_snapshot('LLM')
    assert snapshot['last_rendered_revision'] == rendered[0]
    assert snapshot['report_revision'] > rendered[0]
    app.renderer = original
    assert app._publish()['state'] == 'COMPLETE'
    assert book.pending_work('LLM')['report_pending'] is False


