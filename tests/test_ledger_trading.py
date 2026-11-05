"""A02-A06/A15-A16: full public ledger calls with an isolated deterministic market."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from china_stock_sim.common.policies import load_policy_bundle
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.evidence import fee_breakdown
from china_stock_sim.ledger.execution_evidence import account_input_hash, rule_context_from_market
from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.signals import execution_facts

CODE = 'SH600000'
DAY = '2026-10-30'
FIXTURE_EXECUTION_CONTEXT = {'rule_id': 'fixture-main-board-v1',
    'effective_from': '2026-01-01', 'price_tick': '0.01', 'limit_rate': '0.1'}


class Facts:
    def __init__(self):
        self.snapshots = {}
        self.quotes = {}
        start = date(2026, 10, 20)
        self.calendar_rows = [{'date': (start + timedelta(days=i)).isoformat(),
            'is_trading_day': str(int((start + timedelta(days=i)).weekday() < 5))} for i in range(60)]

    def calendar(self, *, start=None, end=None, **kwargs):
        rows = [r for r in self.calendar_rows if (not start or r['date'] >= start)
                and (not end or r['date'] <= end)]
        return DataResult('READY' if rows else 'MISSING', rows, (), ('calendar-proof',))

    def calendar_operation(self, operation, day, **kwargs):
        size = kwargs.get('size', 121)
        result = (TradingCalendar(self.calendar_rows).window(day, size) if operation == 'window'
                  else getattr(TradingCalendar(self.calendar_rows), operation)(day))
        return DataResult(result.state, result.data, result.issues, ('calendar-proof',))

    def prices(self, code, start, end, **kwargs):
        rows = []
        for r in self.calendar_rows:
            if not start <= r['date'] <= end or r['is_trading_day'] != '1':
                continue
            day = r['date']
            price = self.quotes.get((code, day), '10.00')
            previous = self.calendar_operation('previous_session', day).data
            rows.append({'date': day, 'code': code, 'open': price, 'high': price, 'low': price,
                'close': price, 'preclose': self.quotes.get((code, previous), '10.00'),
                'volume': 10000, 'amount': str(Decimal(price) * 10000),
                'tradestatus': 1, 'isST': 0, 'adjustflag': '3'})
        return DataResult('READY', rows, (), tuple(f'{code}:{r["date"]}' for r in rows))

    def security(self, code, **kwargs):
        return DataResult('READY', [{'code': code, 'listed': '2000-01-01', 'board': 'MAIN',
            'eligibility_known': True, 'source_status': 1}], (), ('security-proof',))

    def events(self, code, **kwargs):
        return DataResult('READY', [], (), ('events-proof',))

    def event_coverage(self, code, start, end, **kwargs):
        return DataResult('READY', {'code': code, 'start': start, 'end': end, 'complete': True}, (), ('coverage-proof',))

    def read_snapshot(self, identity):
        return deepcopy(self.snapshots[identity])

    def readiness(self, code, as_of, purpose, *, knowledge_cutoff=None, rule_context=None):
        if purpose == 'valuation':
            return self.prices(code, as_of, as_of)
        assert purpose == 'execution'
        price = self.quotes.get((code, as_of), '10.00')
        previous = self.calendar_operation('previous_session', as_of).data
        previous_close = self.quotes.get((code, previous), '10.00')
        bar = {'date': as_of, 'code': code, 'open': price, 'high': price, 'low': price,
               'close': price, 'preclose': previous_close, 'previous_close': previous_close,
               'volume': 10000, 'amount': str(Decimal(price) * 10000),
               'tradestatus': 1, 'isST': 0, 'adjustflag': '3'}
        security = {'code': code, 'listed': '2000-01-01', 'board': 'MAIN',
                    'eligibility_known': True, 'source_status': 1}
        facts = execution_facts(bar, security, [], knowledge_cutoff,
                                events_complete=True, rule_context=rule_context)
        return DataResult(facts['state'], facts, tuple(facts['issues']),
                          (f'{code}:{as_of}', f'{code}:{previous}', 'security-proof', 'coverage-proof'))

    def snapshot(self, code, day, cutoff, *, execution_context=None):
        identity = f'{code}-{day}-{len(self.snapshots)}'
        results = {'valuation': self.prices(code, day, day).as_dict()}
        if execution_context is not None:
            results['execution'] = self.readiness(code, day, 'execution', knowledge_cutoff=cutoff,
                                                   rule_context=execution_context).as_dict()
        ids = list(dict.fromkeys(identity for result in results.values()
                                 for identity in result['observation_ids']))
        self.snapshots[identity] = {'snapshot_id': identity, 'body': {'schema': 1,
            'observation_ids': ids, 'content': {
                'as_of': day, 'knowledge_cutoff': cutoff, 'purposes': list(results),
                'results': {code: results}}}}
        return identity


@pytest.fixture
def book(settings, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('ledger must not network'))
    now = [datetime.fromisoformat('2026-10-29T20:00:00+08:00')]
    market = Facts()
    service = LedgerService(settings, market=market, clock=lambda: now[0], test_mode=True)
    service.initialize()
    service.test_now = now
    result = call(service, 'activate_simulation', {'target_session': DAY})
    assert result['state'] == 'APPLIED'
    return service


def revision(book, account='LLM'):
    return book._read(account, lambda tx: tx.state['account_revision'])


def call(book, method, payload, *, account='LLM', op=None, expected=None):
    return getattr(book, method)(account, payload, operation_id=op or uuid4().hex,
        expected_revision=revision(book, account) if expected is None else expected)


def plan_payload(book, day=DAY, orders=None, plan_id=None):
    previous = book.market.calendar_operation('previous_session', day).data
    now = book._now().isoformat()
    orders = [{'code': CODE, 'side': 'BUY', 'quantity': 1000, 'limit_price': '10.00', 'reason': 'fixture'}] if orders is None else orders
    snapshots = [book.market.snapshot(o['code'], previous, now) for o in orders]
    if not snapshots:
        snapshots = [book.market.snapshot(CODE, previous, now)]
    account = book.account_state('LLM', day)
    ruleset = load_policy_bundle(book.root / 'configs/execution_rules.toml', target_session=day,
        knowledge_cutoff=now, execution_policy=book.settings.raw['execution'], fee_policy=book.settings.raw['fees'])
    return {'account_snapshot': account, 'account_snapshot_id': account['state_id'],
        'account_snapshot_hash': account_input_hash(account), 'account_revision': account['account_revision'],
        'ruleset': ruleset, 'plan_id': plan_id or f'plan-{day}', 'account_id': 'LLM', 'strategy_id': 'LLM-Daily',
        'strategy_version': 'fixture', 'target_session': day, 'market_as_of': previous,
        'input_cutoff': now, 'generated_at': now, 'market_snapshot_ids': snapshots,
        'orders': orders, 'no_order_reason': 'HOLD' if not orders else None}


def execute_payload(book, plan, order, *, state='SIMULATED_FILL', price='10.00'):
    now = book._now().isoformat()
    if state == 'SIMULATED_FILL' and hasattr(book.market, 'quotes'):
        book.market.quotes[(order['code'], order['target_session'])] = price
    if state == 'NOT_FILLED' and hasattr(book.market, 'quotes'):
        # Prove a real limit miss; a free text refusal is no longer sufficient.
        book.market.quotes[(order['code'], order['target_session'])] = str(
            Decimal(order['limit_price']) + (Decimal('0.01') if order['side'] == 'BUY' else Decimal('-0.01')))
    context, queries = rule_context_from_market(book, plan, order['code'], order['target_session'], now)
    view = book.execution_view(plan['account_id'], plan['plan_id'])
    result = {'account_id': plan['account_id'], 'account_view_hash': view['view_hash'],
        'account_revision': view['account_revision'], 'account_view_evidence': view,
        'ruleset_hash': plan['ruleset_hash'], 'reason_details': {},
        'reason_code': 'OPEN_PRICE_MATCH' if state == 'SIMULATED_FILL' else
                       'LIMIT_NOT_MET' if state == 'NOT_FILLED' else 'WAITING_INPUTS',
        'reason_evidence': {'observation_ids': sorted({i for q in queries for i in q['result']['observation_ids']})},
        'data_evidence': queries, 'result_id': f'result:{order["order_id"]}', 'order_id': order['order_id'],
        'plan_id': plan['plan_id'], 'code': order['code'], 'side': order['side'],
        'target_session': order['target_session'], 'state': state, 'reason': 'fixture',
        'computed_at': now, 'knowledge_cutoff': now,
        'market_snapshot_ids': [book.market.snapshot(order['code'], order['target_session'], now,
                                      execution_context=context)],
        'execution_context': context,
        'execution_model': plan['rule_policy']['model'], 'fee_policy_hash': plan['fee_policy_hash'],
        'rule_policy_hash': plan['rule_policy_hash'], 'quantity': order['quantity'], 'price': price}
    result.update(fee_breakdown(price, order['quantity'], order['side'], plan['fee_policy'], order['target_session']))
    if state != 'SIMULATED_FILL':
        for key in ('quantity', 'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen', 'fees_fen', 'net_cash_fen'):
            result[key] = 0 if state == 'NOT_FILLED' else None
        result['price'] = None
    return result


def buy_and_close(book):
    saved = call(book, 'freeze_plan', plan_payload(book))
    assert saved['state'] == 'APPLIED', saved
    plan = book.get_plan('LLM', saved['plan_id'])
    book.test_now[0] = datetime.fromisoformat('2026-10-30T20:00:00+08:00')
    result = call(book, 'apply_execution_results', execute_payload(book, plan, plan['orders'][0]))
    assert result['state'] == 'APPLIED', result
    closed = call(book, 'close_session', {'session': DAY, 'knowledge_cutoff': book._now().isoformat()})
    assert closed['state'] == 'APPLIED', closed
    return plan


def test_reference_buy_sell_amounts_and_idempotence(book):
    buy_and_close(book)
    state = book.account_state('LLM', '2026-11-02')
    assert state['cash_fen'] == 3_999_490
    assert state['positions'][0]['sellable_shares'] == 1000
    assert book.positions('LLM', DAY)[0]['sellable_shares'] == 0
    sell = plan_payload(book, '2026-11-02', [{'code': CODE, 'side': 'SELL', 'quantity': 1000,
        'limit_price': '10.30', 'reason': 'fixture'}])
    assert call(book, 'freeze_plan', sell)['state'] == 'APPLIED'
    plan = book.get_plan('LLM', sell['plan_id'])
    assert book.account_state('LLM', '2026-11-02')['positions'][0]['sellable_shares'] == 0
    assert book.account_state('LLM', '2026-11-02', own_order_id=plan['order_ids'][0])['positions'][0]['sellable_shares'] == 1000
    book.test_now[0] = datetime.fromisoformat('2026-11-02T20:00:00+08:00')
    result = execute_payload(book, plan, plan['orders'][0], price='10.30')
    applied = call(book, 'apply_execution_results', result, op='sell-once')
    assert applied['state'] == 'APPLIED', applied
    before = book.settings.database.read_bytes()
    again = call(book, 'apply_execution_results', result, op='sell-once', expected=0)
    assert again['state'] == 'ALREADY_APPLIED'
    assert book.settings.database.read_bytes() == before
    assert book.account_state('LLM', '2026-11-03')['cash_fen'] == 5_028_465
    assert sum(f['fees_fen'] for f in book.fills('LLM')['rows']) == 1535
    assert not book.positions('LLM', '2026-11-02')


@pytest.mark.parametrize('change,expected', [
    (lambda p: p.update(market_as_of=DAY), 'INVALID_INPUT'),
    (lambda p: p['orders'][0].update(quantity=101), 'INVALID_INPUT'),
    (lambda p: p['orders'][0].update(quantity=100000), 'INVALID_INPUT'),
    (lambda p: p['orders'][0].update(limit_price='10.001'), 'INVALID_INPUT'),
    (lambda p: p['orders'].append(dict(p['orders'][0], side='SELL')), 'INVALID_INPUT'),
    (lambda p: p.update(market_snapshot_ids=[]), 'WAITING_DATA'),
])
def test_plan_atomic_rejections(book, change, expected):
    payload = plan_payload(book)
    change(payload)
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', payload)
    assert result['state'] == expected, result
    assert book.settings.database.read_bytes() == before
    assert book.orders('LLM')['total'] == 0


def test_freeze_clock_cutoff_revision_and_generated_time(book):
    payload = plan_payload(book)
    assert call(book, 'freeze_plan', payload, expected=0)['state'] == 'STALE_ACCOUNT'
    book.test_now[0] = datetime.fromisoformat('2026-10-30T09:00:00+08:00')
    payload['frozen_at'] = '2026-10-29T20:00:00+08:00'
    assert call(book, 'freeze_plan', payload)['state'] == 'INVALID_INPUT'


def test_waiting_does_not_release_charge_or_consume_terminal(book):
    payload = plan_payload(book)
    assert call(book, 'freeze_plan', payload)['state'] == 'APPLIED'
    plan = book.get_plan('LLM', payload['plan_id'])
    book.test_now[0] = datetime.fromisoformat('2026-10-30T20:00:00+08:00')
    waiting = execute_payload(book, plan, plan['orders'][0], state='WAITING_DATA')
    assert call(book, 'apply_execution_results', waiting)['state'] == 'WAITING_DATA'
    state = book.account_state('LLM', '2026-11-02')
    assert state['cash_fen'] == 5_000_000 and state['reserved_cash_fen'] == 1_000_510
    assert book.fills('LLM')['total'] == 0
    result = execute_payload(book, plan, plan['orders'][0], price='9.90')
    assert call(book, 'apply_execution_results', result)['state'] == 'APPLIED'
    state = book.account_state('LLM', '2026-11-02')
    assert state['reserved_cash_fen'] == 0
    assert state['cash_fen'] == 5_000_000 + result['net_cash_fen']


def test_unfilled_requires_zero_and_releases_reservation(book):
    payload = plan_payload(book)
    call(book, 'freeze_plan', payload)
    plan = book.get_plan('LLM', payload['plan_id'])
    book.test_now[0] = datetime.fromisoformat('2026-10-30T20:00:00+08:00')
    result = execute_payload(book, plan, plan['orders'][0], state='NOT_FILLED')
    assert call(book, 'apply_execution_results', result)['state'] == 'APPLIED'
    assert book.account_state('LLM', '2026-11-02')['cash_fen'] == 5_000_000
    assert book.fills('LLM')['total'] == 0


def test_fill_fee_or_other_symbol_evidence_rejected(book):
    payload = plan_payload(book)
    call(book, 'freeze_plan', payload)
    plan = book.get_plan('LLM', payload['plan_id'])
    book.test_now[0] = datetime.fromisoformat('2026-10-30T20:00:00+08:00')
    result = execute_payload(book, plan, plan['orders'][0])
    result['commission_fen'] += 1
    assert call(book, 'apply_execution_results', result)['state'] == 'INVALID_INPUT'
    result['commission_fen'] -= 1
    result['market_snapshot_ids'] = [book.market.snapshot('SZ000001', DAY, book._now().isoformat())]
    assert call(book, 'apply_execution_results', result)['state'] == 'WAITING_DATA'


def test_empty_plan_distinct_from_no_plan(book):
    payload = plan_payload(book, orders=[])
    assert call(book, 'freeze_plan', payload)['state'] == 'APPLIED'
    assert book.get_plan('LLM', payload['plan_id'])['status'] == 'EMPTY'
    assert call(book, 'record_no_plan', {'target_session': DAY, 'reason': 'MODEL_FAILED'})['state'] == 'CONFLICT'


def test_model_claim_persistent_limit_and_response_recovery(book):
    first = call(book, 'claim_model_attempt', {'target_session': DAY, 'request': {'prompt': 'fixture'}})
    assert first['state'] == 'APPLIED'
    assert revision(book) == 1
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['state'] == 'CONFLICT'
    assert call(book, 'finish_model_attempt', {'target_session': DAY, 'attempt_id': first['attempt_id'],
        'status': 'INTERRUPTED', 'reason': 'process exited'})['state'] == 'APPLIED'
    second = call(book, 'claim_model_attempt', {'target_session': DAY})
    assert second['attempt_number'] == 2
    assert call(book, 'finish_model_attempt', {'target_session': DAY, 'attempt_id': second['attempt_id'],
        'status': 'RECEIVED', 'response': {'orders': []}})['state'] == 'APPLIED'
    assert call(book, 'finish_model_attempt', {'target_session': DAY, 'attempt_id': second['attempt_id'],
        'status': 'SUCCEEDED'})['state'] == 'APPLIED'
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['state'] == 'CONFLICT'
    assert revision(book) == 1


def test_report_ack_does_not_create_another_publish_revision(book):
    failed = call(book, 'record_step_result', {'effective_date': DAY, 'step': 'report',
        'status': 'FAILED', 'reason': 'disk busy'})
    assert failed['state'] == 'APPLIED'
    current = failed['report_revision']
    ok = call(book, 'record_step_result', {'effective_date': DAY, 'step': 'report',
        'status': 'SUCCEEDED', 'rendered_revision': current})
    assert ok['report_revision'] == current and ok['last_rendered_revision'] == current
    assert revision(book) == 1


def test_rebalance_five_sessions_and_frozen_target(book):
    task = {'task_id': 'task1', 'start_session': DAY, 'end_session': '2026-11-05',
        'targets': [{'code': CODE, 'side': 'BUY', 'quantity': 1000}]}
    assert call(book, 'save_rebalance_task', task)['state'] == 'APPLIED'
    plan = plan_payload(book)
    plan['task_id'] = 'task1'
    plan['orders'][0]['quantity'] = 1100
    assert call(book, 'freeze_plan', plan)['state'] == 'INVALID_INPUT'
    too_long = dict(task, task_id='task2', end_session='2026-11-06')
    assert call(book, 'save_rebalance_task', too_long)['state'] == 'INVALID_INPUT'


def test_float_rejected_and_test_clock_not_allowed_in_production(settings):
    with pytest.raises(ValueError, match='测试时钟'):
        LedgerService(settings, clock=lambda: datetime.now().astimezone())
    assert fee_breakdown(Decimal('10'), 1000, 'BUY', settings.raw['fees'], DAY)['fees_fen'] == 510
