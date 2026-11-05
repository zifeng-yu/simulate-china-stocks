"""Display-only P/L attribution, date boundaries and bounded local market reads."""

import json
from collections import Counter
from copy import deepcopy

import pytest

from china_stock_sim.market.contracts import DataResult
from china_stock_sim.reporting import trading_data
from china_stock_sim.reporting.trading_data import build_trading_view

PREVIOUS = '2026-11-02'
DAY = '2026-11-03'
NEXT = '2026-11-04'
CUTOFF = '2026-11-03T22:00:00+08:00'
CODE = 'SH600036'


class SavedMarket:
    def __init__(self):
        self.calls = []
        self.states = {}

    def security(self, code, **kwargs):
        self.calls.append(('security', code, kwargs['knowledge_cutoff']))
        return DataResult('READY', {'code': code, 'name': '已有名称', '_provenance': {'secret': 'omit'}})

    def calendar_operation(self, operation, session, **kwargs):
        assert operation == 'previous_session'
        self.calls.append(('calendar', session, kwargs['knowledge_cutoff']))
        previous = {DAY: PREVIOUS, NEXT: DAY, PREVIOUS: '2026-10-30'}.get(session)
        return DataResult('READY' if previous else 'MISSING', previous)

    def prices(self, code, start, end, **kwargs):
        assert start == end  # Never request 121 days or a whole market just for a quote.
        self.calls.append(('prices', code, start, kwargs['knowledge_cutoff']))
        state = self.states.get((code, start), 'MISSING' if start == NEXT else 'READY')
        if state != 'READY':
            return DataResult(state, [])
        return DataResult('READY', [{'code': code, 'date': start, 'open': '10.00',
            'high': '12.00', 'low': '9.00', 'close': '11.00' if start == DAY else '10.00',
            'preclose': '9.99', '_provenance': {'secret': 'omit'}}])


def position(day, *, shares=100, value=110_000, cost=100_500, code=CODE):
    return {'code': code, 'shares': shares, 'cost_fen': cost, 'price': '11.00',
            'price_date': day, 'market_value_fen': value, 'status': 'FINAL',
            'receivable_quantity': '0', 'receivable_cost_fen': 0, 'share_receivable_value_fen': 0}


def statement(day, positions, pnl):
    return {'id': day + ':1', 'session': day, 'statement_revision': 1,
            'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL',
            'daily_pnl_fen': pnl, 'positions': positions, 'cash_receivable_fen': 0,
            'tax_payable_fen': 0, 'correction_pnl_fen': 0}


def report():
    return {'account': {'account_id': 'S1', 'target_session': NEXT,
                'simulation_start_session': PREVIOUS, 'positions': [
                    {'code': CODE, 'total_shares': 100, 'sellable_shares': 100, 'reserved_shares': 0}]},
            'statements': [statement(PREVIOUS, [position(PREVIOUS, value=100_000)], 0),
                           statement(DAY, [position(DAY)], 10_000)],
            'plans': [{'id': 'plan-next', 'plan_id': 'plan-next', 'target_session': NEXT,
                       'market_as_of': DAY, 'frozen_at': CUTOFF}],
            'orders': [{'id': 'order-next', 'order_id': 'order-next', 'plan_id': 'plan-next',
                        'target_session': NEXT, 'code': CODE, 'side': 'BUY', 'quantity': 100,
                        'limit_price': '11.00', 'status': 'PENDING', 'reason': '持有后继续完成目标'}],
            'fills': [], 'cash_entries': [], 'cases': [], 'entitlements': [], 'tax_items': []}


def add_fill(value, side, quantity, net, *, identity='fill-1', day=DAY, code=CODE):
    value['fills'].append({'id': identity, 'fill_id': identity, 'order_id': identity,
        'plan_id': 'plan-today', 'target_session': day, 'effective_date': day,
        'code': code, 'side': side, 'quantity': quantity, 'price': '11.00',
        'fees_fen': 500, 'net_cash_fen': net, 'realized_pnl_fen': 0, 'state': 'SIMULATED_FILL'})
    value['cash_entries'].append({'entry_id': identity, 'kind': side, 'effective_date': day,
                                  'reference_id': identity, 'amount_fen': net})


def build(value, market=None, **kwargs):
    return build_trading_view(value, market=market or SavedMarket(), knowledge_cutoff=CUTOFF, **kwargs)


def test_saved_position_values_floating_and_daily_pnl_have_distinct_dates():
    value = report()
    view = build(value)
    holding = view['holdings'][0]
    assert (holding['name'], holding['price_date'], holding['market_value_fen']) == (
        '已有名称', DAY, 110_000)
    assert holding['floating_pnl_fen'] == 9_500
    assert view['stock_daily_pnl']['rows'][0]['daily_pnl_fen'] == 10_000
    assert view['valuation_session'] == view['stock_daily_pnl']['session'] == DAY
    assert view['account_target_session'] == NEXT
    assert view['stock_daily_pnl']['state'] == 'READY'
    assert view['stock_daily_pnl']['unattributed_fen'] == 0


@pytest.mark.parametrize('scenario,expected', [
    ('buy', 9_500), ('partial_sell', 9_500), ('sold_out', 9_500), ('buy_and_sell', 9_000),
])
def test_ordinary_trade_pnl_includes_net_fees_once_and_keeps_sold_out_stock(scenario, expected):
    value = report()
    if scenario in {'buy', 'buy_and_sell'}:
        value['statements'][0]['positions'] = []
        add_fill(value, 'BUY', 100, -100_500)
    if scenario == 'partial_sell':
        add_fill(value, 'SELL', 50, 54_500)
    if scenario == 'sold_out':
        add_fill(value, 'SELL', 100, 109_500)
        value['statements'][1]['positions'] = []
        value['account']['positions'] = []
    if scenario == 'buy_and_sell':
        add_fill(value, 'SELL', 50, 54_500, identity='fill-2')
    if scenario in {'partial_sell', 'buy_and_sell'}:
        value['statements'][1]['positions'] = [position(DAY, shares=50, value=55_000, cost=50_250)]
    value['statements'][1]['daily_pnl_fen'] = expected
    pnl = build(value)['stock_daily_pnl']
    assert pnl['state'] == 'READY'
    assert pnl['rows'][0]['code'] == CODE
    assert pnl['rows'][0]['daily_pnl_fen'] == pnl['sum_fen'] == expected
    assert pnl['unattributed_fen'] == 0


def test_first_simulation_day_has_known_zero_initial_position_not_missing_history():
    value = report()
    value['account']['simulation_start_session'] = DAY
    value['statements'] = [value['statements'][1]]
    value['statements'][0]['daily_pnl_fen'] = 9_500
    add_fill(value, 'BUY', 100, -100_500)
    assert build(value)['stock_daily_pnl']['rows'][0]['daily_pnl_fen'] == 9_500


@pytest.mark.parametrize('scenario', [
    'missing_previous', 'noncontinuous', 'old_price', 'partial', 'restatement', 'shares',
    'receivable', 'case', 'tax', 'cash_correction', 'pnl_correction', 'missing_fill_net',
])
def test_uncertain_or_unsupported_daily_attribution_is_null_with_chinese_reason(scenario):
    value = report()
    current = value['statements'][1]
    if scenario == 'missing_previous':
        value['statements'].pop(0)
    elif scenario == 'noncontinuous':
        value['statements'][0]['session'] = '2026-10-30'
    elif scenario == 'old_price':
        current['positions'][0]['price_date'] = PREVIOUS
    elif scenario == 'partial':
        current['valuation_status'] = 'PROVISIONAL'
    elif scenario == 'restatement':
        current['requires_restatement'] = True
    elif scenario == 'shares':
        current['positions'][0]['shares'] += 100
    elif scenario == 'receivable':
        current['positions'][0]['receivable_quantity'] = '10'
    elif scenario == 'case':
        value['cases'] = [{'code': CODE, 'record_date': PREVIOUS, 'status': 'REGISTERED'}]
    elif scenario == 'tax':
        value['tax_items'] = [{'code': CODE, 'trigger_date': DAY, 'assessed_fen': 100}]
    elif scenario == 'cash_correction':
        value['cash_entries'] = [{'kind': 'CORRECTION', 'effective_date': DAY, 'amount_fen': 1}]
    elif scenario == 'pnl_correction':
        current['correction_pnl_fen'] = 1
    elif scenario == 'missing_fill_net':
        add_fill(value, 'BUY', 100, -100_500)
        value['fills'][0]['net_cash_fen'] = None
    pnl = build(value)['stock_daily_pnl']
    assert pnl['state'] == 'PARTIAL'
    assert pnl['rows'][0]['daily_pnl_fen'] is None
    assert pnl['rows'][0]['reason']
    assert any('\u4e00' <= c <= '\u9fff' for c in pnl['rows'][0]['reason'])
    assert pnl['unavailable_codes'] == [CODE]
    assert pnl['sum_fen'] == 0  # Explicit subtotal of known rows, not a fabricated stock result.
    assert pnl['unattributed_fen'] == 10_000


def test_unknown_account_pnl_and_unassigned_difference_are_never_smeared_over_stocks():
    value = report()
    value['statements'][1]['daily_pnl_fen'] = None
    pnl = build(value)['stock_daily_pnl']
    assert pnl['account_daily_pnl_fen'] is pnl['unattributed_fen'] is None
    assert pnl['rows'][0]['daily_pnl_fen'] is None
    value['statements'][1]['daily_pnl_fen'] = 10_001
    pnl = build(value)['stock_daily_pnl']
    assert pnl['state'] == 'PARTIAL' and pnl['rows'][0]['daily_pnl_fen'] == 10_000
    assert pnl['unattributed_fen'] == 1


def test_swapped_cash_references_cannot_pass_just_because_account_total_matches():
    value = report()
    value['statements'][0]['positions'] = []
    value['statements'][1]['positions'][0]['shares'] = 200
    add_fill(value, 'BUY', 100, -100_500, identity='one')
    add_fill(value, 'BUY', 100, -110_500, identity='two')
    value['cash_entries'][0]['reference_id'] = 'two'
    value['cash_entries'][1]['reference_id'] = 'one'
    pnl = build(value)['stock_daily_pnl']
    assert pnl['rows'][0]['daily_pnl_fen'] is None
    assert '流水' in pnl['rows'][0]['reason']


def test_explicit_future_event_does_not_contaminate_prior_daily_pnl():
    value = report()
    value['cases'] = [{'code': CODE, 'record_date': NEXT, 'ex_date': '2026-11-05'}]
    assert build(value)['stock_daily_pnl']['state'] == 'READY'


@pytest.mark.parametrize('form', ['proposal', 'cancelled', 'not_applicable', 'not_due'])
def test_explicit_non_economic_observation_does_not_block_daily_pnl(form):
    value = report()
    event = {'code': CODE, 'kind': 'DISTRIBUTION', 'status': 'PROPOSED',
             'record_date': None, 'ex_date': None, 'payment_date': None,
             'share_listing_date': None, 'conflicts': []}
    case = {'case_id': 'observation', 'code': CODE, 'event': event, 'actions': {},
            'eligibility': [], 'status': 'WAITING_DATA'}
    if form == 'cancelled':
        event['status'] = 'CANCELLED'
    elif form == 'not_applicable':
        event.update(status='IMPLEMENTED', record_date=PREVIOUS)
        case['actions'] = {'eligibility': {'state': 'NOT_APPLICABLE'}}
    elif form == 'not_due':
        case['actions'] = {'eligibility': {'state': 'NOT_DUE', 'operation_date': NEXT}}
    value['cases'] = [case]
    pnl = build(value)['stock_daily_pnl']
    assert pnl['state'] == 'READY' and pnl['rows'][0]['daily_pnl_fen'] == 10_000


@pytest.mark.parametrize('guard', ['entitlement', 'tax', 'old_payment', 'conflict', 'due_action'])
def test_observation_label_cannot_hide_existing_economics_or_uncertain_case(guard):
    value = report()
    case = {'case_id': 'case-old', 'code': CODE, 'event': {'code': CODE,
            'kind': 'DISTRIBUTION', 'status': 'CANCELLED', 'conflicts': []},
            'actions': {'eligibility': {'state': 'NOT_APPLICABLE'}}, 'eligibility': []}
    value['cases'] = [case]
    if guard == 'entitlement':
        value['entitlements'] = [{'code': CODE, 'case_id': 'case-old', 'amount_fen': 100}]
    elif guard == 'tax':
        value['tax_items'] = [{'code': CODE, 'case_id': 'case-old', 'assessed_fen': 100}]
    elif guard == 'old_payment':
        value['cash_entries'] = [{'kind': 'DIVIDEND', 'reference_id': 'case-old',
                                  'effective_date': PREVIOUS, 'amount_fen': 100}]
    elif guard == 'conflict':
        case['event']['conflicts'] = ['SOURCE_CONFLICT']
    elif guard == 'due_action':
        case['actions'] = {'eligibility': {'state': 'NOT_DUE', 'operation_date': DAY}}
    pnl = build(value)['stock_daily_pnl']
    assert pnl['rows'][0]['daily_pnl_fen'] is None


def test_target_reference_and_execution_quote_dates_do_not_replace_each_other():
    value = report()
    add_fill(value, 'SELL', 100, 109_500)
    view = build(value)
    order = view['plan_orders'][0]
    assert order['id'] == order['order_id'] == 'order-next'
    assert order['limit_price'] == '11.00'
    assert order['target_quote']['session'] == NEXT
    assert order['target_quote']['close'] is None
    assert order['target_quote']['previous_close'] == '11.00'
    assert order['reference_quote']['session'] == DAY
    assert order['reference_quote']['close'] == '11.00'
    assert order['reference_quote']['previous_close'] == '10.00'
    assert order['reference_quote']['source_preclose'] == '9.99'
    assert view['executions'][0]['quote']['session'] == DAY
    assert view['executions'][0]['price'] == '11.00'


@pytest.mark.parametrize('state', ['CONFLICT', 'CORRUPT', 'MISSING'])
def test_bad_market_quote_never_replaces_saved_fill_or_account_valuation(state):
    value, market = report(), SavedMarket()
    market.states[(CODE, DAY)] = state
    view = build(value, market)
    assert view['plan_orders'][0]['reference_quote']['state'] == state
    assert view['plan_orders'][0]['reference_quote']['close'] is None
    assert view['holdings'][0]['price'] == '11.00'
    assert view['holdings'][0]['market_value_fen'] == 110_000
    assert view['stock_daily_pnl']['state'] == 'READY'


def test_shared_cache_reads_each_code_date_once_across_accounts_and_does_not_alias():
    value, market, cache = report(), SavedMarket(), {}
    original = deepcopy(value)
    first = build(value, market, quote_cache=cache)
    count = len(market.calls)
    second = build(value, market, quote_cache=cache)
    assert len(market.calls) == count
    assert max(Counter(market.calls).values()) == 1
    assert {c[2] for c in market.calls if c[0] == 'prices'} == {PREVIOUS, DAY, NEXT}
    first['plan_orders'][0]['reference_quote']['close'] = 'bad'
    assert second['plan_orders'][0]['reference_quote']['close'] == '11.00'
    assert value == original


def test_missing_mock_methods_and_exceptions_degrade_without_breaking_report():
    value = report()
    view = build_trading_view(value, market=object(), knowledge_cutoff=CUTOFF)
    assert view['holdings'][0]['name'] is None
    assert view['stock_daily_pnl']['rows'][0]['daily_pnl_fen'] is None
    assert view['plan_orders'][0]['target_quote']['state'] == 'UNAVAILABLE'

    class BrokenMarket(SavedMarket):
        def prices(self, *args, **kwargs):
            raise RuntimeError('private-secret-do-not-display')

    assert 'private-secret' not in json.dumps(build(value, BrokenMarket()))


def test_quote_budget_is_shared_and_excess_rows_keep_order_identity(monkeypatch):
    monkeypatch.setattr(trading_data, 'MAX_QUOTE_PAIRS', 1)
    value, market, cache = report(), SavedMarket(), {}
    view = build(value, market, quote_cache=cache)
    assert view['plan_orders'][0]['id'] == 'order-next'
    assert '上限' in view['plan_orders'][0]['target_quote']['reason']
    count = len(market.calls)
    build(value, market, quote_cache=cache)
    assert len(market.calls) == count


def test_empty_history_and_receivable_floating_have_explicit_missing_values():
    value = report()
    value['statements'] = []
    view = build(value)
    assert view['valuation_session'] is None
    assert view['holdings'][0]['floating_pnl_fen'] is None
    assert view['stock_daily_pnl']['account_daily_pnl_fen'] is None
    value = report()
    value['statements'][1]['positions'][0].update(receivable_quantity='10',
        receivable_cost_fen=1_000, share_receivable_value_fen=11_000)
    holding = build(value)['holdings'][0]
    assert holding['floating_pnl_fen'] == 9_500
    assert holding['receivable_floating_pnl_fen'] == 10_000


def test_display_never_copies_or_expands_nested_business_originals():
    class ForbiddenBody(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('Business originals must not be copied')

        def __iter__(self):
            raise AssertionError('Business originals must not be traversed')

    value = report()
    for row in [value['plans'][0], value['orders'][0], value['statements'][1]]:
        row['input_bundle'] = ForbiddenBody(secret='PRIVATE_ORIGINAL')
        row['data_evidence'] = ForbiddenBody(secret='PRIVATE_ORIGINAL')
        row['original_record_ref'] = ForbiddenBody(secret='PRIVATE_ORIGINAL')
    output = json.dumps(build(value), ensure_ascii=False)
    assert len(output) < 12_000
    assert 'PRIVATE_ORIGINAL' not in output
    assert 'input_bundle' not in output and 'data_evidence' not in output


def test_naive_knowledge_cutoff_is_rejected_before_any_market_reads():
    market = SavedMarket()
    with pytest.raises(ValueError, match='时区'):
        build_trading_view(report(), market=market, knowledge_cutoff='2026-11-03T22:00:00')
    assert market.calls == []


def test_terminal_order_uses_final_result_and_keeps_old_wait_only_as_history():
    value = report()
    order = value['orders'][0]
    order.update(status='FILLED', reason='OPEN_PRICE_MATCH',
        last_attempt={'state': 'WAITING_DATA', 'reason_code': 'WAITING_INPUTS'},
        result={'state': 'SIMULATED_FILL', 'reason_code': 'OPEN_PRICE_MATCH', 'quantity': 100})
    original = deepcopy(value)
    assert build(value)['plan_orders'][0]['execution_reason'] == 'OPEN_PRICE_MATCH'
    assert value == original
    order.update(status='NOT_FILLED', reason='LIMIT_NOT_MET',
                 result={'state': 'NOT_FILLED', 'reason_code': 'LIMIT_NOT_MET'})
    assert build(value)['plan_orders'][0]['execution_reason'] == 'LIMIT_NOT_MET'
    order.pop('result')
    assert build(value)['plan_orders'][0]['execution_reason'] == 'LIMIT_NOT_MET'
    order.update(status='WAITING_DATA', reason='PENDING')
    assert build(value)['plan_orders'][0]['execution_reason'] == 'WAITING_INPUTS'
