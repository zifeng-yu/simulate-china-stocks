"""Complete current execution design acceptance through real public services."""

import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from hashlib import sha256

import pytest
from execution_fixtures import (
    CODES,
    FIRST,
    advance,
    execution_book,
    proposal,
    set_coverage,
    set_event_settlement_dates,
    set_pool,
    set_quote,
)
from test_ledger_cross_layer import close, corporate, put_event
from test_ledger_trading import call

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.kernel import Transaction
from china_stock_sim.ledger.store import read_snapshot
from china_stock_sim.reporting.ledger import write_ledger_report

assert execution_book


def application(book):
    return ExecutionApplication(book.settings, market=book.market, ledger=book,
                                clock=lambda: book.test_now[0], test_mode=True)


def freeze(book, *, target=FIRST, account='LLM', intents=None, no_order_reason=None):
    request = proposal(book, target=target, account=account, intents=intents,
                       no_order_reason=no_order_reason)
    result = application(book).freeze(request, operation_id=f'freeze:{account}:{target}')
    assert result['commit']['state'] in {'APPLIED', 'ALREADY_APPLIED'}, result
    plan = book.get_plan(account, target_session=target)
    assert plan is not None
    return plan


def settle(book, plan, *, suffix='normal'):
    return application(book).settle(plan['account_id'], plan['plan_id'],
                                    operation_id=f'settle:{plan["plan_id"]}:{suffix}')


def test_real_validation_freeze_buy_sell_close_html_csv_and_isolation(execution_book):
    book = execution_book
    original = book.settings.database.read_bytes()
    validated = application(book).validate(proposal(book))
    assert validated['plan_envelope'] is not None, validated
    assert book.settings.database.read_bytes() == original
    plan = freeze(book)
    assert plan['buy_budget_fen'] == 1000510
    advance(book, FIRST)
    result = settle(book, plan)
    assert result['completion_status'] == 'COMPLETE', result
    assert book.fills('LLM')['total'] == 1
    assert book.account_state('LLM', FIRST)['cash_fen'] == 3999490
    assert close(book, FIRST)['net_assets_fen'] == 4999490
    assert close(book, FIRST, account='S1')['net_assets_fen'] == 5000000
    sale = freeze(book, target='2026-11-02', intents=[{
        'code': CODES[0], 'side': 'SELL', 'limit_price': '10.30',
        'sell_fraction': '1', 'order_index': 0, 'reason': 'full exit'}])
    set_quote(book, CODES[0], '2026-11-02', '10.30', preclose='10.00')
    advance(book, '2026-11-02')
    assert settle(book, sale)['completion_status'] == 'COMPLETE'
    day = close(book, '2026-11-02')
    assert day['net_assets_fen'] == day['closing_cash_fen'] == 5028465
    assert book.positions('LLM', '2026-11-02') == []
    assert sum(f['fees_fen'] for f in book.fills('LLM')['rows']) == 1535
    report = write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    assert report['published']
    assert '50,284.65' in book.settings.report.read_text()
    csvs = list(book.settings.report.parent.glob('LLM-r*.csv'))
    assert len(csvs) == 1 and '5028465' in csvs[0].read_text()
    assert book.fills('S1')['total'] == 0
    assert book.account_state('S1', '2026-11-03')['cash_fen'] == 5000000


def test_waiting_no_progress_preserves_resources_and_new_fact_resumes(execution_book):
    book = execution_book
    plan = freeze(book)
    set_quote(book, CODES[0], FIRST, is_st=None, fetched=FIRST + 'T18:20:00+08:00')
    advance(book, FIRST)
    first = settle(book, plan)
    assert first['completion_status'] == 'PARTIAL', first
    assert book.orders('LLM')['rows'][0]['status'] == 'WAITING_DATA'
    assert book.account_state('LLM', FIRST)['reserved_cash_fen'] == 1000510
    assert book.fills('LLM')['total'] == 0
    before = book.settings.database.read_bytes()
    again = settle(book, plan)
    assert again['completion_status'] == 'PARTIAL'
    assert book.settings.database.read_bytes() == before
    set_quote(book, CODES[0], FIRST, fetched=FIRST + 'T21:00:00+08:00')
    advance(book, FIRST, '22:00:00')
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['total'] == 1
    assert book.account_state('LLM', FIRST)['cash_fen'] == 3999490


def test_proved_limit_miss_does_not_wait_for_unrelated_unknown_st(execution_book):
    book = execution_book
    plan = freeze(book)
    set_quote(book, CODES[0], FIRST, '10.20', preclose='10.00', is_st=None,
              fetched=FIRST + 'T18:20:00+08:00')
    advance(book, FIRST)
    result = settle(book, plan)
    assert result['completion_status'] == 'COMPLETE', result
    order = book.orders('LLM')['rows'][0]
    assert order['status'] == 'NOT_FILLED'
    assert book.fills('LLM')['total'] == 0
    account = book.account_state('LLM', FIRST)
    assert account['cash_fen'] == 5000000 and account['reserved_cash_fen'] == 0


def test_sell_then_re_read_pending_buy_completes_without_using_sale_budget(execution_book):
    book = execution_book
    initial = freeze(book)
    advance(book, FIRST)
    assert settle(book, initial)['completion_status'] == 'COMPLETE'
    close(book, FIRST)
    plan = freeze(book, target='2026-11-02', intents=[
        {'code': CODES[1], 'side': 'BUY', 'limit_price': '10.00', 'quantity': 100,
         'order_index': 0, 'reason': 'fixed original cash buy'},
        {'code': CODES[0], 'side': 'SELL', 'limit_price': '10.00', 'quantity': 1000,
         'order_index': 1, 'reason': 'prior holding exit'}])
    assert [o['side'] for o in plan['orders']] == ['SELL', 'BUY']
    assert plan['buy_budget_fen'] == 100501
    advance(book, '2026-11-02')
    view = book.execution_view('LLM', plan['plan_id'])
    buy = next(o for o in view['orders'] if o['order']['side'] == 'BUY')
    assert any(i['kind'] == 'SELL_PHASE_PENDING' for i in buy['issues'])
    result = settle(book, plan)
    assert result['completion_status'] == 'COMPLETE', result
    fills = [f for f in book.fills('LLM')['rows'] if f['plan_id'] == plan['plan_id']]
    assert [f['side'] for f in fills] == ['SELL', 'BUY']
    assert fills[1]['quantity'] == 100
    assert book.account_state('LLM', '2026-11-02')['reserved_cash_fen'] == 0


def test_final_orders_replay_from_current_state_without_rewriting_book(execution_book):
    book = execution_book
    plan = freeze(book)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.get_plan('LLM', plan['plan_id'])['orders'][0]['status'] == 'PENDING'
    assert book.orders('LLM')['rows'][0]['status'] == 'FILLED'
    before = book.settings.database.read_bytes()
    assert settle(book, plan, suffix='fresh-process')['completion_status'] == 'COMPLETE'
    assert book.settings.database.read_bytes() == before
    assert book.fills('LLM')['total'] == 1


def test_empty_plan_and_missing_plan_never_create_synthetic_orders(execution_book):
    book = execution_book
    plan = freeze(book, intents=[], no_order_reason='explicit HOLD')
    assert plan['orders'] == []
    advance(book, FIRST)
    before = book.settings.database.read_bytes()
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.settings.database.read_bytes() == before
    missing = application(book).match('S1', 'never-frozen')
    assert missing['state'] != 'COMPLETE'
    assert book.orders('S1')['total'] == book.fills('S1')['total'] == 0
    assert close(book, FIRST)['net_assets_fen'] == 5000000


def test_closed_machine_recovery_uses_only_original_session(execution_book):
    book = execution_book
    plan = freeze(book)
    advance(book, '2026-11-03')
    result = settle(book, plan)
    assert result['completion_status'] == 'COMPLETE', result
    fill = book.fills('LLM')['rows'][0]
    assert fill['target_session'] == fill['effective_date'] == FIRST
    assert book.orders('LLM')['total'] == 1
    assert book.get_plan('LLM', target_session='2026-11-02') is None


def test_frozen_fee_policy_survives_current_configuration_change(execution_book):
    book = execution_book
    plan = freeze(book)
    before = deepcopy(plan['fee_policy'])
    book.settings.raw['fees']['commission_rate'] = '0.001'
    advance(book, FIRST)
    outcome = settle(book, plan)
    assert outcome['completion_status'] == 'COMPLETE', outcome
    assert book.get_plan('LLM', plan['plan_id'])['fee_policy'] == before
    assert book.fills('LLM')['rows'][0]['fees_fen'] == 510


def test_past_account_revision_cannot_be_hidden_by_new_freeze_read(execution_book):
    book = execution_book
    stale = proposal(book)
    freeze(book, intents=[], no_order_reason='already frozen by another caller')
    before = book.settings.database.read_bytes()
    result = application(book).validate(stale)
    assert result['plan_envelope'] is None
    assert book.settings.database.read_bytes() == before


def test_unconfirmed_source_repair_does_not_touch_other_account(execution_book):
    book = execution_book
    first = freeze(book)
    other = freeze(book, account='S1', intents=[{'code': CODES[1], 'side': 'BUY',
        'reason': 'independent account', 'order_index': 0, 'limit_price': '10.00', 'quantity': 100}])
    set_quote(book, CODES[0], FIRST, is_st=None, fetched=FIRST + 'T18:20:00+08:00')
    advance(book, FIRST)
    assert settle(book, first)['completion_status'] == 'PARTIAL'
    assert settle(book, other)['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['total'] == 0 and book.fills('S1')['total'] == 1


def test_cash_entitlement_tax_through_actual_calculators(execution_book):
    book = execution_book
    event = put_event(book)
    plan = freeze(book)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert corporate(book, 'register_entitlements', event, FIRST, 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    close(book, FIRST)
    sale = freeze(book, target='2026-11-02', intents=[{'code': CODES[0], 'side': 'SELL',
        'reason': 'sell dividend-qualified holding', 'order_index': 0,
        'limit_price': '10.30', 'quantity': 1000}])
    advance(book, '2026-11-02')
    assert corporate(book, 'recognize_entitlements', event, '2026-11-02', 'cash_accrual', case)['state'] == 'APPLIED'
    assert settle(book, sale)['completion_status'] == 'COMPLETE'
    day = close(book, '2026-11-02')
    assert day['cash_receivable_fen'] == 10000 and day['tax_payable_fen'] == 2000
    advance(book, '2026-11-03')
    assert corporate(book, 'settle_entitlements', event, '2026-11-03', 'cash_payment', case)['state'] == 'APPLIED'
    close(book, '2026-11-03')
    advance(book, '2026-11-04')
    assert call(book, 'record_tax', {'effective_date': '2026-11-04',
                                  'payment_id': 'acceptance-tax'})['state'] == 'APPLIED'
    final = close(book, '2026-11-04')
    assert final['net_assets_fen'] == final['closing_cash_fen'] == 5036465
    assert final['tax_payable_fen'] == 0


def test_sale_creates_unknown_tax_and_original_buy_stays_pending(execution_book):
    book = execution_book
    event = put_event(book, bonus='0.1')
    plan = freeze(book)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert corporate(book, 'register_entitlements', event, FIRST, 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    close(book, FIRST)
    orders = [
        {'code': CODES[0], 'side': 'SELL', 'quantity': 1000, 'limit_price': '9.50',
         'reason': 'exit old shares', 'order_index': 0},
        {'code': CODES[1], 'side': 'BUY', 'quantity': 100, 'limit_price': '10.00',
         'reason': 'independent original budget', 'order_index': 1}]
    trading = freeze(book, target='2026-11-02', intents=orders)
    advance(book, '2026-11-02')
    for action in ('cash_accrual', 'share_accrual'):
        assert corporate(book, 'recognize_entitlements', event, '2026-11-02', action, case)['state'] == 'APPLIED'
    outcome = settle(book, trading)
    assert outcome['completion_status'] == 'PARTIAL', outcome
    current = book.orders('LLM', plan_id=trading['plan_id'])['rows']
    assert [o['status'] for o in current] == ['FILLED', 'WAITING_DATA']
    assert any(t['assessed_fen'] is None for t in book.tax_items('LLM'))
    assert book.account_state('LLM', '2026-11-02')['reserved_cash_fen'] == 100501
    assert book.positions('LLM', '2026-11-02') == []


def test_month_end_plan_uses_new_target_pool_instead_of_previous_day_pool(execution_book):
    """T04: October's last session closes with one pool; November uses another."""
    book = execution_book
    set_pool(book, '2026-10-29', FIRST, [CODES[0]])
    advance(book, FIRST)
    close(book, FIRST)
    set_pool(book, FIRST, '2026-11-02', [CODES[1]])
    cutoff = book._now().isoformat()
    old_pool = book.market.pool(FIRST, knowledge_cutoff=cutoff)
    new_pool = book.market.pool('2026-11-02', knowledge_cutoff=cutoff)
    assert old_pool.ready and new_pool.ready
    assert [r['code'] for r in old_pool.data['selected']] == [CODES[0]]
    assert [r['code'] for r in new_pool.data['selected']] == [CODES[1]]
    request = proposal(book, target='2026-11-02', intents=[{
        'code': code, 'side': 'BUY', 'quantity': 100, 'limit_price': '10.00',
        'reason': 'target-month pool qualification', 'order_index': i}
        for i, code in enumerate(CODES)])
    validation = application(book).validate(request)
    envelope = validation['plan_envelope']
    assert envelope is not None, validation
    assert [o['code'] if isinstance(o, dict) else o.code for o in envelope.orders] == [CODES[1]]
    assert [(r['code'], r['reason_code']) for r in envelope.skipped_intents] == [(CODES[0], 'OUTSIDE_POOL')]
    frozen = application(book).freeze(request, operation_id='freeze:new-month-pool')
    assert frozen['commit']['state'] == 'APPLIED', frozen
    plan = book.get_plan('LLM', target_session='2026-11-02')
    assert [o['code'] for o in plan['orders']] == [CODES[1]]
    advance(book, '2026-11-02')
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert [f['code'] for f in book.fills('LLM')['rows']] == [CODES[1]]
    assert close(book, '2026-11-02')['accounting_status'] == 'COMPLETE'


def test_cost_transfer_and_later_share_delivery_preserve_original_sell_and_fifo(execution_book):
    """T15/T23: recover a partially completed original plan after real bonus delivery."""
    book = execution_book
    event = put_event(book, bonus='0.1', face='1')
    event = set_event_settlement_dates(book, event, payment_date='2026-11-02',
                                      share_listing_date='2026-11-02')
    purchases = freeze(book, intents=[{
        'code': code, 'side': 'BUY', 'quantity': quantity, 'limit_price': '10.00',
        'reason': 'original holdings', 'order_index': i}
        for i, (code, quantity) in enumerate(zip(CODES, (1000, 100), strict=True))])
    advance(book, FIRST)
    assert settle(book, purchases)['completion_status'] == 'COMPLETE'
    assert corporate(book, 'register_entitlements', event, FIRST, 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    close(book, FIRST)
    sales = freeze(book, target='2026-11-02', intents=[
        {'code': CODES[1], 'side': 'SELL', 'quantity': 100, 'limit_price': '10.00',
         'reason': 'independent original sell', 'order_index': 0},
        {'code': CODES[0], 'side': 'SELL', 'quantity': 1000, 'limit_price': '9.50',
         'reason': 'frozen quantity excludes later bonus shares', 'order_index': 1}])
    sale_order = next(o for o in sales['orders'] if o['code'] == CODES[0])
    frozen_view = book.execution_view('LLM', sales['plan_id'])
    frozen_lots = next(o['frozen_lot_allocations'] for o in frozen_view['orders']
                       if o['order_id'] == sale_order['order_id'])
    original_lot = next(lot for lot in book.lots('LLM') if lot['code'] == CODES[0])
    advance(book, '2026-11-02')
    partial = settle(book, sales, suffix='before-cost-transfer')
    assert partial['completion_status'] == 'PARTIAL', partial
    assert {o['code']: o['status'] for o in book.orders('LLM', plan_id=sales['plan_id'])['rows']} == {
        CODES[1]: 'FILLED', CODES[0]: 'WAITING_DATA'}
    independent_fill = next(f for f in book.fills('LLM')['rows']
                            if f['side'] == 'SELL' and f['code'] == CODES[1])
    for action in ('cash_accrual', 'share_accrual'):
        assert corporate(book, 'recognize_entitlements', event, '2026-11-02', action, case)['state'] == 'APPLIED'
    after_cost = next(lot for lot in book.lots('LLM') if lot['lot_id'] == original_lot['lot_id'])
    assert after_cost['quantity'] == 1000
    assert 0 < after_cost['cost_fen'] < original_lot['cost_fen']
    assert corporate(book, 'settle_entitlements', event, '2026-11-02', 'cash_payment', case)['state'] == 'APPLIED'
    assert corporate(book, 'settle_entitlements', event, '2026-11-02', 'share_delivery', case)['state'] == 'APPLIED'
    current_lots = [lot for lot in book.lots('LLM') if lot['code'] == CODES[0]]
    assert sum(lot['quantity'] for lot in current_lots) == 1100
    assert sum(lot['cost_fen'] for lot in current_lots) == original_lot['cost_fen']
    bonus_lot = next(lot for lot in current_lots if lot['lot_id'] != original_lot['lot_id'])
    assert bonus_lot['quantity'] == 100 and bonus_lot['source_lot_id'] == original_lot['lot_id']
    assert book.get_plan('LLM', sales['plan_id']) == sales
    recovered = settle(book, sales, suffix='after-share-delivery')
    assert recovered['completion_status'] == 'COMPLETE', {
        'results': [{k: r.get(k) for k in ('state', 'reason_code', 'reason', 'issues')}
                    for r in recovered.get('results', [])],
        'issues': [(o['order_id'], o['issues']) for o in recovered['account_view']['orders']]}
    fills = book.fills('LLM')['rows']
    assert next(f for f in fills if f['order_id'] == independent_fill['order_id']) == independent_fill
    sold = [f for f in fills if f['order_id'] == sale_order['order_id']]
    assert len(sold) == 1 and sold[0]['quantity'] == sale_order['quantity'] == 1000
    assert [(a['lot_id'], a['quantity']) for a in sold[0]['lot_allocations']] == [
        (a['lot_id'], a['quantity']) for a in frozen_lots]
    assert sold[0]['lot_allocations'][0]['cost_fen'] == after_cost['cost_fen']
    remaining = [lot for lot in book.lots('LLM') if lot['quantity']]
    assert remaining == [bonus_lot]
    assert close(book, '2026-11-02')['accounting_status'] == 'COMPLETE'
    advance(book, '2026-11-03')
    assert call(book, 'record_tax', {'effective_date': '2026-11-03'})['state'] == 'APPLIED'
    assert close(book, '2026-11-03')['accounting_status'] == 'COMPLETE'
    assert write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)['published']


def test_expired_five_session_task_rejected_at_freeze_without_moving_dates(execution_book):
    """T20: valid proposal format cannot bypass the original rebalance task window."""
    book = execution_book
    task = {'task_id': 'five-session-task', 'start_session': FIRST, 'end_session': '2026-11-05',
            'targets': [{'code': CODES[0], 'side': 'BUY', 'quantity': 100}]}
    created = call(book, 'save_rebalance_task', task)
    assert created['state'] == 'APPLIED'
    assert created['sessions'] == [FIRST, '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05']
    for code in CODES:
        set_quote(book, code, '2026-11-05')
        set_coverage(book, code, '2026-11-05')
    for day in created['sessions']:
        advance(book, day)
        close(book, day)
    request = replace(proposal(book, target='2026-11-06', intents=[{
        'code': CODES[0], 'side': 'BUY', 'quantity': 100, 'limit_price': '10.00',
        'reason': 'expired parent task must not roll forward', 'order_index': 0}]),
        rebalance_task_id=task['task_id'])
    validation = application(book).validate(request)
    assert validation['plan_envelope'] is not None, validation
    assert validation['plan_envelope'].task_id == task['task_id']
    before = book.account_state('LLM', '2026-11-06')
    rejected = application(book).freeze(request, operation_id='freeze:expired-task')
    assert rejected['commit']['state'] == 'INVALID_INPUT', rejected
    assert '窗口' in str(rejected['commit']), rejected
    assert book.get_plan('LLM', target_session='2026-11-06') is None
    assert book.orders('LLM')['total'] == book.fills('LLM')['total'] == 0
    after = book.account_state('LLM', '2026-11-06')
    assert (after['cash_fen'], after['reserved_cash_fen']) == (before['cash_fen'], 0)
    expired = next(t for t in book.pending_work('LLM')['expired_rebalance_tasks']
                   if t['task_id'] == task['task_id'])
    assert expired['start_session'] == FIRST and expired['end_session'] == '2026-11-05'
    assert expired['sessions'] == created['sessions']
    assert expired['child_plan_ids'] == []
    assert expired['remaining_targets'] == task['targets']


def test_read_only_api_does_not_write_data_logs_or_snapshot_files(execution_book):
    book = execution_book
    plan = freeze(book)
    advance(book, FIRST)
    def tree():
        return {str(p.relative_to(book.root)): sha256(p.read_bytes()).hexdigest()
                for p in book.root.rglob('*') if p.is_file()}
    before = tree()
    response = application(book).match('LLM', plan['plan_id'])
    assert response['results']
    assert tree() == before


def test_dict_proposal_and_dto_proposal_have_same_pure_output(execution_book):
    book = execution_book
    dto = proposal(book)
    app = application(book)
    assert app.validate(dto) == app.validate(asdict(dto))


def test_subset_commit_complete_is_not_complete_plan(execution_book):
    book = execution_book
    plan = freeze(book, intents=[{'code': code, 'side': 'BUY', 'reason': 'two orders',
        'order_index': i, 'limit_price': '10.00', 'quantity': 100} for i, code in enumerate(CODES)])
    advance(book, FIRST)
    computed = application(book).match('LLM', plan['plan_id'])
    result = computed['results'][0]
    result = asdict(result) if is_dataclass(result) else result
    subset = call(book, 'apply_execution_results', {'results': [result]})
    assert subset['completion_status'] == 'COMPLETE'
    assert [o['status'] for o in book.orders('LLM')['rows']].count('FILLED') == 1
    assert application(book).match('LLM', plan['plan_id'])['completion_status'] != 'COMPLETE'
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['total'] == 2


def test_failure_inside_fill_transaction_rolls_back_money_shares_and_fee(execution_book, monkeypatch):
    book = execution_book
    plan = freeze(book)
    advance(book, FIRST)
    original = Transaction.append
    def disk_full(tx, table, identity, row):
        if table == 'fills':
            raise OSError('isolated simulated full disk')
        return original(tx, table, identity, row)
    monkeypatch.setattr(Transaction, 'append', disk_full)
    result = settle(book, plan)
    assert result['completion_status'] != 'COMPLETE'
    account = book.account_state('LLM', FIRST)
    assert account['cash_fen'] == 5000000 and account['reserved_cash_fen'] == 1000510
    assert book.positions('LLM', FIRST) == [] and book.fills('LLM')['total'] == 0
    monkeypatch.setattr(Transaction, 'append', original)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.account_state('LLM', FIRST)['cash_fen'] == 3999490


def test_process_interrupt_after_first_fill_resumes_from_persistent_orders(execution_book, monkeypatch):
    book = execution_book
    plan = freeze(book, intents=[{'code': code, 'side': 'BUY', 'reason': 'interrupt acceptance',
        'order_index': i, 'limit_price': '10.00', 'quantity': 100} for i, code in enumerate(CODES)])
    advance(book, FIRST)
    original, calls = Transaction.append, []
    def interrupted(tx, table, identity, row):
        if table == 'fills':
            calls.append(identity)
            if len(calls) == 2:
                raise KeyboardInterrupt('isolated process interruption')
        return original(tx, table, identity, row)
    monkeypatch.setattr(Transaction, 'append', interrupted)
    with pytest.raises(KeyboardInterrupt):
        settle(book, plan)
    assert book.fills('LLM')['total'] == 1
    monkeypatch.setattr(Transaction, 'append', original)
    restarted = LedgerService(book.settings, market=book.market,
        clock=lambda: book.test_now[0], test_mode=True)
    restarted.test_now = book.test_now
    assert settle(restarted, plan)['completion_status'] == 'COMPLETE'
    assert restarted.fills('LLM')['total'] == 2
    assert restarted.account_state('LLM', FIRST)['cash_fen'] == 5000000 - 2 * 100501


def test_report_failure_after_execution_does_not_retrade(execution_book, monkeypatch):
    book = execution_book
    plan = freeze(book)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    close(book, FIRST)
    original = book.settings.database.read_bytes()
    def no_space(*args, **kwargs):
        raise OSError('isolated report write failure')
    monkeypatch.setattr('china_stock_sim.reporting.ledger.atomic_text', no_space)
    with pytest.raises(OSError):
        write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    assert book.settings.database.read_bytes() == original
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['total'] == 1


def test_other_process_lock_blocks_execution_then_releases_on_exit(execution_book):
    book = execution_book
    plan = freeze(book)
    advance(book, FIRST)
    script = ('from pathlib import Path; import sys; '
              'from china_stock_sim.ledger.kernel import ledger_lock; '
              'lock=ledger_lock(Path(sys.argv[1])); lock.__enter__(); '
              'print("locked",flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', script, str(book.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        blocked = settle(book, plan)
        assert blocked['completion_status'] == 'ERROR'
        assert book.fills('LLM')['total'] == 0
        assert book.account_state('LLM', FIRST)['cash_fen'] == 5000000
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['total'] == 1
