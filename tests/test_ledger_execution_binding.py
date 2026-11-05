"""LX02/LX03: real ledger transactions bind DTOs, policies and refusal evidence."""

from copy import deepcopy
from dataclasses import fields
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from test_ledger_trading import (
    CODE,
    DAY,
    Facts,
    buy_and_close,
    call,
    execute_payload,
    plan_payload,
)
from test_ledger_trading import book as book

from china_stock_sim.common.fees import fee_breakdown
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.domain import ExecutionResult, FrozenOrder, FrozenPlan, PlanEnvelope
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.execution_evidence import account_input_hash
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.signals import execution_facts


class BindingFacts(Facts):
    """Small, explicit changes in public source facts, never ledger result overrides."""

    def __init__(self):
        super().__init__()
        start = date(2026, 1, 1)
        self.calendar_rows = [{'date': (start + timedelta(days=i)).isoformat(),
            'is_trading_day': str(int((start + timedelta(days=i)).weekday() < 5))}
            for i in range(365)]
        self.target_bar = {}
        self.identity = {}
        self.event_rows = []

    def prices(self, code, start, end, **kwargs):
        result = super().prices(code, start, end, **kwargs)
        for row in result.data:
            if row['date'] == DAY:
                row.update(self.target_bar)
        return result

    def security(self, code, **kwargs):
        result = super().security(code, **kwargs)
        result.data[0].update(self.identity)
        return result

    def events(self, code, **kwargs):
        return DataResult('READY', deepcopy(self.event_rows), (), ('complex-event-proof',))

    def readiness(self, code, as_of, purpose, *, knowledge_cutoff=None, rule_context=None):
        if purpose == 'valuation':
            return self.prices(code, as_of, as_of)
        bar = deepcopy(self.prices(code, as_of, as_of).data[0])
        previous = self.calendar_operation('previous_session', as_of).data
        bar['previous_close'] = self.quotes.get((code, previous), '10.00')
        facts = execution_facts(bar, self.security(code).data[0], self.event_rows,
                                knowledge_cutoff, events_complete=True, rule_context=rule_context)
        return DataResult(facts['state'], facts, tuple(facts['issues']),
                          (f'{code}:{as_of}', f'{code}:{previous}', 'security-proof', 'coverage-proof'))


def frozen(book, orders=None):
    request = plan_payload(book, orders=orders)
    result = call(book, 'freeze_plan', request)
    assert result['state'] == 'APPLIED', result
    book.test_now[0] = datetime.fromisoformat(f'{DAY}T20:00:00+08:00')
    return book.get_plan('LLM', request['plan_id'])


def financial_state(book):
    account = book.account_state('LLM', DAY)
    return {'cash': account['cash_fen'], 'reserved': account['reserved_cash_fen'],
            'positions': book.positions('LLM', DAY), 'lots': book.lots('LLM'),
            'fills': book.fills('LLM'), 'orders': book.orders('LLM')}


def assert_rejected_unchanged(book, payload, allowed):
    before, database = financial_state(book), book.settings.database.read_bytes()
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] in allowed, result
    assert financial_state(book) == before
    assert book.settings.database.read_bytes() == database
    assert before['reserved'] > 0
    return result


def refusal(book, plan, reason, **details):
    order = plan['orders'][0]
    price = book.market.quotes.get((order['code'], order['target_session']), '10.00')
    result = execute_payload(book, plan, order, price=price)
    result.update(state='NOT_FILLED', reason_code=reason, reason_details=details, price=None)
    for key in ('quantity', 'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen',
                'fees_fen', 'net_cash_fen'):
        result[key] = 0
    return result


def add_query(book, payload, method, **kwargs):
    kwargs['knowledge_cutoff'] = payload['knowledge_cutoff']
    result = getattr(book.market, method)(**kwargs).as_dict()
    payload['data_evidence'].append({'method': method, 'kwargs': kwargs, 'result': result})
    payload['reason_evidence']['observation_ids'] = sorted(set(
        payload['reason_evidence']['observation_ids']) | set(result['observation_ids']))


def test_nonempty_plan_and_execution_dtos_cross_the_public_ledger_boundary(book):
    source = plan_payload(book)
    source.update(fee_policy=book.settings.raw['fees'], rule_policy=book.settings.raw['execution'])
    source['orders'] = (FrozenOrder(order_id='dto-order', code=CODE, side='BUY', quantity=1000,
        limit_price=Decimal('10.00'), reason='DTO integration', reserved_cash_fen=1000510),)
    source['target_session'] = date.fromisoformat(source['target_session'])
    source['market_as_of'] = date.fromisoformat(source['market_as_of'])
    source['input_cutoff'] = datetime.fromisoformat(source['input_cutoff'])
    source['generated_at'] = datetime.fromisoformat(source['generated_at'])
    source['market_snapshot_ids'] = tuple(source['market_snapshot_ids'])
    envelope = PlanEnvelope(**source)
    saved = call(book, 'freeze_plan', envelope)
    assert saved['state'] == 'APPLIED', saved
    plan: FrozenPlan = book.get_plan('LLM', envelope.plan_id)
    assert plan['orders'][0]['order_id'] == 'dto-order'
    assert plan['buy_budget_fen'] == 1000510
    assert plan['account_snapshot_hash'] == envelope.account_snapshot_hash
    assert plan['ruleset_hash'] == envelope.ruleset['ruleset_hash']
    assert plan['frozen_at'] and plan['plan_hash'] and plan['document']
    assert book.account_state('LLM', DAY)['cash_fen'] == 5000000
    book.test_now[0] = datetime.fromisoformat(f'{DAY}T20:00:00+08:00')
    wire = execute_payload(book, plan, plan['orders'][0])
    wire['target_session'] = date.fromisoformat(wire['target_session'])
    wire['computed_at'] = datetime.fromisoformat(wire['computed_at'])
    wire['knowledge_cutoff'] = datetime.fromisoformat(wire['knowledge_cutoff'])
    wire['price'] = Decimal(wire['price'])
    result_dto = ExecutionResult(**{f.name: wire[f.name] for f in fields(ExecutionResult)})
    applied = call(book, 'apply_execution_results', result_dto, op='dto-buy-once')
    assert applied['state'] == 'APPLIED', applied
    account = book.account_state('LLM', DAY)
    assert account['cash_fen'] == 3999490 and account['reserved_cash_fen'] == 0
    assert book.positions('LLM', DAY)[0]['total_shares'] == 1000
    assert book.positions('LLM', DAY)[0]['sellable_shares'] == 0
    assert book.positions('LLM', '2026-11-02')[0]['sellable_shares'] == 1000
    assert book.fills('LLM')['rows'][0]['fees_fen'] == 510
    before = financial_state(book)
    assert call(book, 'apply_execution_results', result_dto, op='dto-buy-once',
                expected=0)['state'] == 'ALREADY_APPLIED'
    assert financial_state(book) == before


@pytest.mark.parametrize('reseal_old_input', [False, True])
def test_old_account_snapshot_is_not_repaired_by_new_expected_revision(settings, reseal_old_input):
    clock = datetime.fromisoformat('2026-10-29T20:00:00+08:00')
    service = LedgerService(settings, market=Facts(), clock=lambda: clock, test_mode=True)
    service.initialize()
    old = plan_payload(service)
    activated = call(service, 'activate_simulation', {'target_session': DAY})
    assert activated['state'] == 'APPLIED'
    current = service.account_state('LLM', DAY)
    assert current['account_revision'] > old['account_revision']
    if reseal_old_input:
        # Even rewriting revision/identity and rehashing cannot refresh the old facts.
        old.update(account_revision=current['account_revision'], account_snapshot_id=current['state_id'])
        old['account_snapshot'].update(account_revision=current['account_revision'], state_id=current['state_id'])
        old['account_snapshot_hash'] = account_input_hash(old['account_snapshot'])
    database = settings.database.read_bytes()
    # The public optimistic revision is current; the financial planning input is stale.
    result = service.freeze_plan('LLM', old, operation_id='stale-planner',
                                  expected_revision=current['account_revision'])
    assert result['state'] == 'STALE_ACCOUNT', result
    assert settings.database.read_bytes() == database
    assert service.orders('LLM')['total'] == 0
    assert service.account_state('LLM', DAY)['reserved_cash_fen'] == 0


@pytest.mark.parametrize('reseal', [False, True])
def test_forged_rule_registry_cannot_be_frozen_even_with_a_self_consistent_hash(book, reseal):
    proposal = plan_payload(book)
    proposal['ruleset']['rules'][0]['limit_rate'] = '0.20'
    if reseal:
        body = {k: v for k, v in proposal['ruleset'].items() if k != 'ruleset_hash'}
        proposal['ruleset']['ruleset_hash'] = policy_digest(body)
    database = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', proposal)
    assert result['state'] == 'INVALID_INPUT', result
    assert book.settings.database.read_bytes() == database
    assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 0


@pytest.mark.parametrize('state', ['SIMULATED_FILL', 'NOT_FILLED'])
@pytest.mark.parametrize('field', ['fee_policy_hash', 'rule_policy_hash', 'ruleset_hash', 'execution_model'])
def test_every_terminal_binds_every_frozen_policy_before_any_economic_effect(book, state, field):
    plan = frozen(book)
    payload = execute_payload(book, plan, plan['orders'][0], state=state)
    payload[field] = 'unrelated-policy'
    assert_rejected_unchanged(book, payload, {'INVALID_INPUT'})


@pytest.mark.parametrize('state', ['SIMULATED_FILL', 'NOT_FILLED'])
def test_submitted_context_cannot_choose_a_different_rule_than_the_frozen_package(book, state):
    plan = frozen(book)
    payload = execute_payload(book, plan, plan['orders'][0], state=state)
    payload['execution_context']['rule_id'] = 'OTHER_RULE'
    assert_rejected_unchanged(book, payload, {'INVALID_INPUT'})


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'fake_id', 'wrong_reason', 'unrelated_calendar'])
def test_refusal_requires_proof_of_the_claim_and_cannot_release_reserved_cash(book, mutation):
    plan = frozen(book)
    payload = execute_payload(book, plan, plan['orders'][0], state='NOT_FILLED')
    if mutation == 'missing':
        payload.pop('reason_evidence')
    elif mutation == 'empty':
        payload['reason_evidence']['observation_ids'] = []
    elif mutation == 'fake_id':
        payload['reason_evidence']['observation_ids'] = ['not-in-source-evidence']
    elif mutation == 'wrong_reason':
        payload['reason_code'] = 'NO_TRADES'  # Source proves positive volume.
    else:
        payload['reason_evidence']['observation_ids'] = ['calendar-proof']
    assert_rejected_unchanged(book, payload, {'INVALID_INPUT', 'INTEGRITY_ERROR', 'WAITING_DATA'})


@pytest.mark.parametrize('scenario,reason,qualification', [
    ('limit', 'LIMIT_NOT_MET', None), ('zero', 'NO_TRADES', None),
    ('small_volume', 'INSUFFICIENT_DAILY_VOLUME', None),
    ('ipo', 'MODEL_UNSUPPORTED_SESSION', None), ('relisting', 'MODEL_UNSUPPORTED_SESSION', None),
    ('delisting_first', 'MODEL_UNSUPPORTED_SESSION', None),
    ('st', 'BUY_INELIGIBLE', 'ST'), ('recent_listing', 'BUY_INELIGIBLE', 'NEW_LISTING'),
    ('complex', 'BUY_INELIGIBLE', 'KNOWN_COMPLEX_ACTION'),
])
def test_true_refusal_reasons_release_only_the_original_reservation(book, scenario, reason, qualification):
    market = BindingFacts()
    book.market = market
    if scenario == 'ipo':
        market.identity['listed'] = '2026-10-26'
    elif scenario == 'recent_listing':
        market.identity['listed'] = '2026-10-20'  # Ninth session, beyond IPO's five-day exception.
    elif scenario == 'relisting':
        market.identity['relisting_date'] = DAY
    elif scenario == 'delisting_first':
        market.identity['delisting_start_date'] = DAY
    elif scenario == 'complex':
        market.event_rows = [{'event_id': 'known-future-complex', 'code': CODE, 'kind': 'COMPLEX',
            'status': 'ANNOUNCED', 'ex_date': '2026-11-02', 'available_at': '2026-10-28T20:00:00+08:00'}]
    plan = frozen(book)
    if scenario == 'limit':
        market.quotes[(CODE, DAY)] = '10.01'
    elif scenario == 'zero':
        market.target_bar.update(volume=0, amount='0')
    elif scenario == 'small_volume':
        market.target_bar.update(volume=999, amount='9990')
    elif scenario == 'st':
        market.target_bar['isST'] = 1
    payload = refusal(book, plan, reason, **({'qualification': qualification} if qualification else {}))
    if scenario == 'recent_listing':
        add_query(book, payload, 'calendar_operation', operation='window', day=plan['market_as_of'], size=121)
    if scenario == 'complex':
        add_query(book, payload, 'events', code=CODE)
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == 'APPLIED', result
    after = book.account_state('LLM', DAY)
    assert after['cash_fen'] == 5000000 and after['reserved_cash_fen'] == 0
    assert book.fills('LLM')['total'] == 0
    assert book.positions('LLM', DAY) == []
    assert book.orders('LLM')['rows'][0]['status'] == 'NOT_FILLED'
    assert book.orders('LLM')['rows'][0]['result']['fees_fen'] == 0


def test_explicit_suspension_needs_only_same_day_status_not_ohlc_or_rule_context(book, monkeypatch):
    plan = frozen(book)
    payload = refusal(book, plan, 'SUSPENDED')
    def only_status(code, start, end, **kwargs):
        return DataResult('WAITING_DATA', [{'code': code, 'date': start, 'tradestatus': 0}],
                          ('NO_OHLC',), ('suspension-proof',))
    monkeypatch.setattr(book.market, 'prices', only_status)
    payload.update(market_snapshot_ids=[], data_evidence=[], execution_context=None,
                   reason_evidence={'observation_ids': []})
    add_query(book, payload, 'prices', code=CODE, start=DAY, end=DAY)
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == 'APPLIED', result
    assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 0
    assert book.account_state('LLM', DAY)['cash_fen'] == 5000000
    assert book.fills('LLM')['total'] == 0


def test_later_future_complex_announcement_cannot_cancel_the_original_buy(book):
    market = BindingFacts()
    book.market = market
    plan = frozen(book)
    market.event_rows = [{'event_id': 'late-future-complex', 'code': CODE, 'kind': 'COMPLEX',
        'status': 'ANNOUNCED', 'ex_date': '2026-11-03', 'available_at': '2026-10-30T17:00:00+08:00'}]
    payload = refusal(book, plan, 'BUY_INELIGIBLE', qualification='KNOWN_COMPLEX_ACTION')
    add_query(book, payload, 'events', code=CODE)
    assert_rejected_unchanged(book, payload, {'WAITING_DATA'})


@pytest.mark.parametrize('scenario', ['wrong_open', 'zero_volume', 'too_little_volume', 'no_limit'])
def test_fill_claim_cannot_override_the_frozen_model_or_verified_open(book, scenario):
    market = BindingFacts()
    book.market = market
    if scenario == 'no_limit':
        market.identity['listed'] = '2026-10-26'
    plan = frozen(book)
    if scenario == 'zero_volume':
        market.target_bar.update(volume=0, amount='0')
    elif scenario == 'too_little_volume':
        market.target_bar.update(volume=999, amount='9990')
    payload = execute_payload(book, plan, plan['orders'][0])
    if scenario == 'wrong_open':
        # Price and all fees agree internally and fit the limit, but are not the public open.
        payload['price'] = '9.99'
        payload.update(fee_breakdown('9.99', 1000, 'BUY', plan['fee_policy'], DAY))
    assert_rejected_unchanged(book, payload, {'INVALID_INPUT'})


@pytest.mark.parametrize('mutation', ['missing_proof', 'fake_proof', 'wrong_policy'])
def test_sell_refusal_releases_shares_only_after_a_valid_proof(book, mutation):
    buy_and_close(book)
    sell_day = '2026-11-02'
    proposal = plan_payload(book, day=sell_day, orders=[{
        'code': CODE, 'side': 'SELL', 'quantity': 1000, 'limit_price': '10.30', 'reason': 'exit'}])
    assert call(book, 'freeze_plan', proposal)['state'] == 'APPLIED'
    plan = book.get_plan('LLM', proposal['plan_id'])
    book.test_now[0] = datetime.fromisoformat(f'{sell_day}T20:00:00+08:00')
    payload = execute_payload(book, plan, plan['orders'][0], state='NOT_FILLED')
    good = deepcopy(payload)
    if mutation == 'missing_proof':
        payload.pop('reason_evidence')
    elif mutation == 'fake_proof':
        payload['reason_evidence']['observation_ids'] = ['invented-sale-proof']
    else:
        payload['fee_policy_hash'] = 'another-fee-policy'
    before, database = financial_state(book), book.settings.database.read_bytes()
    held = book.positions('LLM', sell_day)[0]
    assert held['reserved_shares'] == 1000 and held['sellable_shares'] == 0
    rejected = call(book, 'apply_execution_results', payload)
    assert rejected['state'] in {'INVALID_INPUT', 'INTEGRITY_ERROR'}, rejected
    assert financial_state(book) == before
    assert book.settings.database.read_bytes() == database
    assert book.positions('LLM', sell_day)[0] == held
    applied = call(book, 'apply_execution_results', good)
    assert applied['state'] == 'APPLIED', applied
    released = book.positions('LLM', sell_day)[0]
    assert released['total_shares'] == 1000 and released['sellable_shares'] == 1000
    assert released['reserved_shares'] == 0 and released['cost_fen'] == held['cost_fen']
    assert book.account_state('LLM', sell_day)['cash_fen'] == before['cash']
    assert book.fills('LLM') == before['fills']
