"""Real frozen books feed the pure matcher; returned DTOs go through actual commits."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta

import pytest
from test_ledger_trading import CODE, DAY, book, call, plan_payload

from china_stock_sim.common.fees import ExecutionSupportError
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.execution.matching import DailyOpenMatcher
from china_stock_sim.ledger.execution_evidence import rule_context_from_market

assert book


def rehash(value, key):
    value[key] = policy_digest({k: v for k, v in value.items() if k != key})
    return value


def inputs(book, orders=None):
    saved = call(book, 'freeze_plan', plan_payload(book, orders=orders))
    assert saved['state'] == 'APPLIED', saved
    plan = book.get_plan('LLM', saved['plan_id'])
    book.test_now[0] = datetime.fromisoformat(DAY + 'T20:00:00+08:00')
    return plan, book.execution_view('LLM', plan['plan_id'])


def bundle(book, plan):
    cutoff = book._now().isoformat()
    target = plan['target_session']
    queries, contexts = {}, {}
    for order in plan['orders']:
        code = order['code']
        context, data = rule_context_from_market(book, plan, code, target, cutoff)
        contexts[code] = context
        for method, kwargs in [
            ('readiness', {'code': code, 'as_of': target, 'purpose': 'execution', 'rule_context': context}),
            ('event_coverage', {'code': code, 'start': target, 'end': target}),
            ('events', {'code': code}),
            ('calendar_operation', {'operation': 'window', 'day': plan['market_as_of'], 'size': 121}),
        ]:
            kwargs['knowledge_cutoff'] = cutoff
            data.append({'method': method, 'kwargs': kwargs,
                         'result': getattr(book.market, method)(**kwargs).as_dict()})
        queries[code] = data
    return rehash({'target_session': target, 'knowledge_cutoff': cutoff, 'by_code': queries,
        'execution_contexts': contexts, 'market_snapshot_ids': [],
        'observation_ids': sorted({i for qs in queries.values() for q in qs
                                  for i in q['result']['observation_ids']})}, 'bundle_hash')


def matched(book, plan, view=None, data=None):
    return DailyOpenMatcher(clock=book._now).match(plan, view or book.execution_view('LLM', plan['plan_id']),
                                                  data or bundle(book, plan))


def query(data, method, code=CODE):
    return next(q for q in data['by_code'][code] if q['method'] == method)


def changed_quote(book, value):
    book.market.quotes[(CODE, DAY)] = value


def test_full_calculator_result_commits_and_reuses_terminal_without_recomputing(book):
    plan, view = inputs(book)
    before = book.settings.database.read_bytes()
    outcome = matched(book, plan, view)[0]
    assert outcome.state == 'SIMULATED_FILL'
    assert outcome.reason_code == 'OPEN_PRICE_MATCH'
    assert outcome.quantity == 1000 and outcome.amount_fen == 1000000
    assert outcome.net_cash_fen == -1000510
    assert book.settings.database.read_bytes() == before
    receipt = call(book, 'apply_execution_results', asdict(outcome), op='match-commit')
    assert receipt['state'] == 'APPLIED', receipt
    assert book.account_state('LLM', DAY)['cash_fen'] == 3999490
    assert matched(book, plan) == ()
    repeat = call(book, 'apply_execution_results', asdict(outcome), op='match-commit', expected=0)
    assert repeat['state'] == 'ALREADY_APPLIED'


@pytest.mark.parametrize('price,expected,reason', [
    ('9.80', 'SIMULATED_FILL', 'OPEN_PRICE_MATCH'),
    ('10.00', 'SIMULATED_FILL', 'OPEN_PRICE_MATCH'),
    ('10.01', 'NOT_FILLED', 'LIMIT_NOT_MET'),
])
def test_open_only_limit_comparison_and_real_submission(book, price, expected, reason):
    plan, _ = inputs(book)
    changed_quote(book, price)
    outcome = matched(book, plan)[0]
    assert (outcome.state, outcome.reason_code) == (expected, reason)
    receipt = call(book, 'apply_execution_results', asdict(outcome))
    assert receipt['state'] == 'APPLIED', receipt
    if expected == 'NOT_FILLED':
        assert outcome.quantity == 0 and outcome.price is None and outcome.fees_fen == 0
        assert book.account_state('LLM', DAY)['cash_fen'] == 5000000


def test_high_and_low_cannot_substitute_for_open(book):
    plan, _ = inputs(book)
    changed_quote(book, '10.01')
    data = bundle(book, plan)
    query(data, 'prices')['result']['data'][0].update(low='9', high='11')
    rehash(data, 'bundle_hash')
    assert matched(book, plan, data=data)[0].reason_code == 'LIMIT_NOT_MET'


@pytest.mark.parametrize('hour,minute,expected', [(18, 9, 'NOT_DUE'), (18, 10, None)])
def test_daily_terminal_time_boundary(book, hour, minute, expected):
    plan, _ = inputs(book)
    book.test_now[0] = datetime.fromisoformat(DAY + f'T{hour:02}:{minute:02}:00+08:00')
    if expected:
        with pytest.raises(ExecutionSupportError, match='18:10') as exc:
            matched(book, plan)
        assert exc.value.code == expected
    else:
        assert matched(book, plan)[0].state == 'SIMULATED_FILL'


@pytest.mark.parametrize('field', ['plan', 'account', 'market', 'order', 'context', 'cutoff', 'future_view'])
def test_reject_tampered_protocol_without_any_write(book, field):
    plan, view = inputs(book)
    data = bundle(book, plan)
    if field == 'plan':
        plan = deepcopy(plan)
        plan['orders'][0]['quantity'] = 200
    elif field == 'account':
        view['commit_guards']['cash_fen'] += 1
    elif field == 'market':
        data['observation_ids'].append('fake')
    elif field == 'order':
        view['orders'][0]['order']['limit_price'] = '12'
        view['view_hash'] = policy_digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
    elif field == 'context':
        data['execution_contexts'][CODE]['limit_rate'] = '0.5'
        rehash(data, 'bundle_hash')
    elif field == 'cutoff':
        data['knowledge_cutoff'] = DAY + 'T17:00:00+08:00'
        rehash(data, 'bundle_hash')
    else:
        view['snapshot_at'] = (book._now()+timedelta(days=1)).isoformat()
    before = book.settings.database.read_bytes()
    with pytest.raises(ExecutionSupportError):
        matched(book, plan, view, data)
    assert book.settings.database.read_bytes() == before


def test_empty_frozen_plan_is_not_an_order(book):
    plan, view = inputs(book, [])
    assert matched(book, plan, view) == ()


def test_missing_volume_waits_without_zero_economic_values(book):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    query(data, 'prices')['result']['data'][0].pop('volume')
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    assert result.state == 'WAITING_DATA'
    assert result.quantity is None and result.price is None and result.fees_fen is None


@pytest.mark.parametrize('volume,reason', [(0, 'NO_TRADES'), (999, 'INSUFFICIENT_DAILY_VOLUME')])
def test_volume_is_full_order_upper_bound(book, volume, reason):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    query(data, 'prices')['result']['data'][0]['volume'] = volume
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    assert result.state == 'NOT_FILLED' and result.reason_code == reason
    assert result.quantity == 0


@pytest.mark.parametrize('missing', ['volume', 'isST', 'preclose'])
def test_independent_limit_miss_ignores_irrelevant_unknowns(book, missing):
    plan, _ = inputs(book)
    changed_quote(book, '10.01')
    data = bundle(book, plan)
    query(data, 'prices')['result']['data'][0].pop(missing)
    data['execution_contexts'] = {}
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    assert result.reason_code == 'LIMIT_NOT_MET' and result.execution_context is None
    assert {q['method'] for q in result.data_evidence} == {'prices'}


@pytest.mark.parametrize('status', ['MISSING', 'WAITING_DATA', 'CONFLICT', 'CORRUPT'])
def test_price_source_unknown_never_becomes_terminal(book, status):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    query(data, 'prices')['result'].update(state=status, data=[], issues=['SOURCE_FAILURE'])
    data['execution_contexts'] = {}
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    assert result.state == 'WAITING_DATA'
    assert result.reason_code == ('INTEGRITY_ERROR' if status == 'CORRUPT' else 'WAITING_INPUTS')


def test_suspension_minimal_proof_without_ohlc(book):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    price = query(data, 'prices')
    price['result'].update(state='PARTIAL', data=[{'code': CODE, 'date': DAY, 'tradestatus': 0}])
    data['by_code'][CODE] = [price]
    data['execution_contexts'] = {}
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    assert result.reason_code == 'SUSPENDED' and result.state == 'NOT_FILLED'


def test_confirmed_delisting_needs_no_bar(book):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    sec = query(data, 'security')
    sec['result']['data'][0]['delisted'] = DAY
    data['by_code'][CODE] = [sec]
    data['execution_contexts'] = {}
    rehash(data, 'bundle_hash')
    assert matched(book, plan, data=data)[0].reason_code == 'SECURITY_NOT_TRADABLE'


def test_corrupt_coverage_is_preserved_but_does_not_block_price_rejection(book):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    query(data, 'event_coverage')['result']['state'] = 'CORRUPT'
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    assert result.reason_code == 'INTEGRITY_ERROR'
    changed_quote(book, '10.01')
    data = bundle(book, plan)
    query(data, 'event_coverage')['result']['state'] = 'CORRUPT'
    rehash(data, 'bundle_hash')
    assert matched(book, plan, data=data)[0].reason_code == 'LIMIT_NOT_MET'


def test_per_order_guard_not_aggregate_waiting(book):
    plan, view = inputs(book)
    view['state'] = 'WAITING_DATA'
    view['issues'] = [{'kind': 'UNRELATED_DIAGNOSTIC'}]
    view['view_hash'] = policy_digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
    assert matched(book, plan, view)[0].state == 'SIMULATED_FILL'
    view['orders'][0]['issues'] = [{'kind': 'UNKNOWN_TAX_LIABILITY'}]
    view['view_hash'] = policy_digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
    result = matched(book, plan, view)[0]
    assert result.reason_code == 'ACCOUNT_RESOURCE_REVIEW'


def test_resource_shortfall_preserves_order_and_does_not_shrink(book):
    plan, view = inputs(book)
    view['orders'][0]['max_payable_fen'] = 1000000
    view['view_hash'] = policy_digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
    result = matched(book, plan, view)[0]
    assert result.state == 'WAITING_DATA' and result.quantity is None
    assert plan['orders'][0]['quantity'] == 1000


def test_read_timestamp_does_not_change_result_identity(book):
    plan, view = inputs(book)
    data = bundle(book, plan)
    one = matched(book, plan, view, data)[0]
    book.test_now[0] += timedelta(seconds=1)
    view['snapshot_at'] = book._now().isoformat()
    two = matched(book, plan, view, data)[0]
    assert one.result_id == two.result_id
    assert one.computed_at != two.computed_at


def test_source_corruption_isolated_to_one_order(book):
    plan, _ = inputs(book, [{'code': c, 'side': 'BUY', 'quantity': 100, 'limit_price': '10',
                            'reason': 'independent'} for c in [CODE, 'SH600001']])
    data = bundle(book, plan)
    query(data, 'prices')['result'].update(state='CORRUPT', data=[])
    data['execution_contexts'].pop(CODE)
    rehash(data, 'bundle_hash')
    results = matched(book, plan, data=data)
    assert [r.state for r in results] == ['WAITING_DATA', 'SIMULATED_FILL']


def policy_block(plan, **changes):
    return {'block_id': 'review-main-board-rule',
            'rule_ids': [r['rule_id'] for r in plan['ruleset']['rules']
                         if r['exchange'] == 'SSE'],
            'effective_from': DAY, 'effective_to': DAY,
            'known_at': DAY + 'T19:00:00+08:00', 'source': 'isolated-official-review',
            'reason': '冻结政策适用范围需核验', 'codes': [CODE], **changes}


def test_policy_conflict_preserves_original_order_and_resources_until_resolved(book):
    plan, view = inputs(book)
    data = bundle(book, plan)
    block = policy_block(plan)
    data['policy_conflicts'] = {CODE: [block]}
    rehash(data, 'bundle_hash')
    result = matched(book, plan, view, data)[0]
    assert (result.state, result.reason_code) == ('WAITING_DATA', 'RULE_POLICY_CONFLICT')
    assert result.reason_details == {'policy_conflicts': [block]}
    assert result.quantity is None and result.price is None and result.fees_fen is None
    receipt = call(book, 'apply_execution_results', asdict(result), op='policy-review')
    assert receipt['state'] == 'WAITING_DATA', receipt
    state = book.account_state('LLM', DAY)
    assert state['cash_fen'] == 5000000 and state['reserved_cash_fen'] == 1000510
    assert book.get_plan('LLM', plan['plan_id']) == plan
    resumed = matched(book, plan)[0]
    assert resumed.state == 'SIMULATED_FILL'
    assert call(book, 'apply_execution_results', asdict(resumed), op='policy-resolved')['state'] == 'APPLIED'


def test_policy_conflict_isolated_and_precedes_other_terminal_evidence(book):
    plan, _ = inputs(book, [{'code': c, 'side': 'BUY', 'quantity': 100, 'limit_price': '10',
                            'reason': 'independent'} for c in [CODE, 'SH600001']])
    data = bundle(book, plan)
    query(data, 'prices')['result']['data'][0]['tradestatus'] = 0
    data['policy_conflicts'] = {CODE: [policy_block(plan)]}
    rehash(data, 'bundle_hash')
    outcomes = matched(book, plan, data=data)
    assert [r.state for r in outcomes] == ['WAITING_DATA', 'SIMULATED_FILL']
    assert outcomes[0].reason_code == 'RULE_POLICY_CONFLICT'


@pytest.mark.parametrize('broken', [
    None, ['not-an-object'], [{'block_id': 'incomplete'}],
    {'codes': ['SZ000001']}, {'rule_ids': ['not-in-original-rules']},
    {'known_at': '2030-01-01T20:00:00+08:00'}, {'effective_from': '2030-01-01'},
    {'effective_to': '2026-01-01'}, {'source': {}},
])
def test_policy_conflict_protocol_rejects_unassociated_or_malformed_blocks(book, broken):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    value = [policy_block(plan, **broken)] if isinstance(broken, dict) else broken
    data['policy_conflicts'] = {CODE: value}
    rehash(data, 'bundle_hash')
    with pytest.raises(ExecutionSupportError) as exc:
        matched(book, plan, data=data)
    assert exc.value.code == 'INVALID_INPUT'


@pytest.mark.parametrize('price,limit,expected', [
    ('11.00', '11.00', 'LIMIT_UP_BUY_BLOCKED'),
    ('9.00', '10.00', 'OPEN_PRICE_MATCH'),
    ('10.00', '12.00', 'ORDER_PRICE_OUT_OF_RANGE'),
])
def test_buy_limit_boundary_and_order_range(book, price, limit, expected):
    plan, _ = inputs(book, [{'code': CODE, 'side': 'BUY', 'quantity': 100,
                            'limit_price': limit, 'reason': 'boundary'}])
    changed_quote(book, price)
    result = matched(book, plan)[0]
    assert result.reason_code == expected
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


@pytest.mark.parametrize('price,limit,expected', [
    ('9.00', '9.00', 'LIMIT_DOWN_SELL_BLOCKED'),
    ('11.00', '10.00', 'OPEN_PRICE_MATCH'),
    ('10.00', '10.01', 'LIMIT_NOT_MET'),
])
def test_existing_holding_sell_directions_and_t_plus_one(book, price, limit, expected):
    plan, _ = inputs(book)
    buy = matched(book, plan)[0]
    assert call(book, 'apply_execution_results', asdict(buy))['state'] == 'APPLIED'
    assert call(book, 'close_session', {'session': DAY, 'knowledge_cutoff': book._now().isoformat()})['state'] == 'APPLIED'
    sell_day = '2026-11-02'
    request = plan_payload(book, day=sell_day, orders=[{'code': CODE, 'side': 'SELL',
        'quantity': 1000, 'limit_price': limit, 'reason': 'exit'}])
    frozen = call(book, 'freeze_plan', request)
    assert frozen['state'] == 'APPLIED', frozen
    plan = book.get_plan('LLM', frozen['plan_id'])
    book.test_now[0] = datetime.fromisoformat(sell_day + 'T20:00:00+08:00')
    book.market.quotes[(CODE, sell_day)] = price
    result = matched(book, plan)[0]
    assert result.reason_code == expected
    posted = call(book, 'apply_execution_results', asdict(result))
    assert posted['state'] == 'APPLIED', posted
    if expected == 'OPEN_PRICE_MATCH':
        assert result.stamp_fen == 550
        assert book.account_state('LLM', sell_day)['positions'] == []


def test_st_buy_refusal_is_accepted_without_full_price_readiness(book, monkeypatch):
    plan, _ = inputs(book)
    original = book.market.prices
    def st(*args, **kwargs):
        response = original(*args, **kwargs)
        for row in response.data:
            if row['date'] == DAY:
                row['isST'] = 1
        return response
    monkeypatch.setattr(book.market, 'prices', st)
    result = matched(book, plan)[0]
    assert result.reason_code == 'BUY_INELIGIBLE'
    assert result.reason_details['qualification'] == 'ST'
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


def test_known_no_limit_session_is_terminal_not_waiting(book, monkeypatch):
    plan, _ = inputs(book)
    original = book.market.security
    def relisting(*args, **kwargs):
        response = original(*args, **kwargs)
        response.data[0]['relisting_date'] = DAY
        return response
    monkeypatch.setattr(book.market, 'security', relisting)
    result = matched(book, plan)[0]
    assert result.reason_code == 'MODEL_UNSUPPORTED_SESSION'
    assert result.state == 'NOT_FILLED'
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


def test_frozen_known_future_complex_action_blocks_buy_without_rewriting_knowledge(book, monkeypatch):
    from china_stock_sim.market.contracts import DataResult
    event = {'code': CODE, 'event_id': 'known', 'kind': 'COMPLEX', 'ex_date': '2026-11-05',
             'status': 'IMPLEMENTED', 'available_at': book._now().isoformat()}
    monkeypatch.setattr(book.market, 'events', lambda *a, **k:
                        DataResult('READY', [deepcopy(event)], (), ('event-proof',)))
    plan, _ = inputs(book)
    event['available_at'] = book._now().isoformat()
    result = matched(book, plan)[0]
    assert result.reason_details == {'qualification': 'KNOWN_COMPLEX_ACTION'}
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


def test_later_known_future_event_does_not_cancel_original_buy(book, monkeypatch):
    from china_stock_sim.market.contracts import DataResult
    plan, _ = inputs(book)
    event = {'code': CODE, 'event_id': 'later', 'kind': 'COMPLEX', 'ex_date': '2026-11-05',
             'status': 'IMPLEMENTED', 'available_at': book._now().isoformat()}
    monkeypatch.setattr(book.market, 'events', lambda *a, **k:
                        DataResult('READY', [event], (), ('later-proof',)))
    result = matched(book, plan)[0]
    assert result.reason_code == 'OPEN_PRICE_MATCH'
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


@pytest.mark.parametrize('later_name', ['*ST当前风险名称', '当前更名证券'],
                         ids=['later_st_name', 'later_renamed_security'])
def test_later_security_name_cannot_rewrite_target_session_eligibility(book, monkeypatch,
                                                                     later_name):
    original = book.market.security
    current_name = ['冻结时普通证券']

    def security(*args, **kwargs):
        response = original(*args, **kwargs)
        response.data[0]['name'] = current_name[0]
        return response

    monkeypatch.setattr(book.market, 'security', security)
    plan, _ = inputs(book)
    frozen_identity = deepcopy(plan['security_facts'])
    assert frozen_identity[CODE]['result']['data'][0]['name'] == '冻结时普通证券'
    book.test_now[0] += timedelta(days=5)
    current_name[0] = later_name
    data = bundle(book, plan)
    assert query(data, 'security')['result']['data'][0]['name'] == later_name
    assert query(data, 'prices')['result']['data'][0]['date'] == DAY
    assert query(data, 'prices')['result']['data'][0]['isST'] == 0

    result = matched(book, plan, data=data)[0]
    assert (result.state, result.reason_code) == ('SIMULATED_FILL', 'OPEN_PRICE_MATCH')
    assert result.target_session == DAY and result.price == '10.00'
    assert result.execution_context['security_class'] == 'ORDINARY'
    assert result.execution_context['status']['is_st'] == 0
    assert plan['security_facts'] == frozen_identity
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'
    assert book.get_plan('LLM', plan['plan_id'])['security_facts'] == frozen_identity
    assert book.fills('LLM')['total'] == 1


@pytest.mark.parametrize('field,value', [('open', 'NaN'), ('open', 'Infinity'), ('volume', True),
                                         ('volume', -1), ('tradestatus', None)])
def test_malformed_required_facts_never_fill(book, field, value):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    query(data, 'prices')['result']['data'][0][field] = value
    rehash(data, 'bundle_hash')
    assert matched(book, plan, data=data)[0].state == 'WAITING_DATA'


def test_frozen_policy_survives_runtime_config_change(book):
    plan, _ = inputs(book)
    book.settings.raw['fees']['commission_rate'] = '0.01'
    result = matched(book, plan)[0]
    assert result.commission_fen == 500
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


def test_many_days_late_still_uses_original_target_open(book):
    plan, _ = inputs(book)
    book.test_now[0] += timedelta(days=5)
    book.market.quotes[(CODE, book._now().date().isoformat())] = '100'
    result = matched(book, plan)[0]
    assert result.target_session == DAY
    assert result.price == '10.00'
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'


def test_fractional_tick_price_cannot_be_filled(book):
    plan, _ = inputs(book)
    changed_quote(book, '9.999')
    assert matched(book, plan)[0].state == 'WAITING_DATA'


def test_pending_market_failure_dto_can_be_persisted_without_corrupt_evidence(book):
    plan, _ = inputs(book)
    data = bundle(book, plan)
    query(data, 'event_coverage')['result'].update(state='CORRUPT', issues=['HASH_MISMATCH'])
    rehash(data, 'bundle_hash')
    result = matched(book, plan, data=data)[0]
    receipt = call(book, 'apply_execution_results', asdict(result), op='failed-input')
    assert receipt['state'] == 'WAITING_DATA', receipt
    assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 1000510
    assert book.fills('LLM')['total'] == 0
    restored = matched(book, plan)[0]
    assert restored.state == 'SIMULATED_FILL'
    assert call(book, 'apply_execution_results', asdict(restored), op='restored-input')['state'] == 'APPLIED'


def test_unknown_special_session_is_waiting_and_not_unsupported(book, monkeypatch):
    plan, _ = inputs(book)
    original = book.market.security
    def unknown(*args, **kwargs):
        response = original(*args, **kwargs)
        response.data[0]['special_status_unknown'] = True
        return response
    monkeypatch.setattr(book.market, 'security', unknown)
    result = matched(book, plan)[0]
    assert result.state == 'WAITING_DATA'
    assert result.reason_code == 'WAITING_INPUTS'


def test_reading_current_immutable_frozen_orders_cannot_hide_terminals(book):
    plan, _ = inputs(book)
    result = matched(book, plan)[0]
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'
    assert plan['orders'][0]['status'] == 'PENDING'
    view = book.execution_view('LLM', plan['plan_id'])
    assert view['orders'][0]['status'] == 'FILLED'
    assert matched(book, plan, view) == ()
