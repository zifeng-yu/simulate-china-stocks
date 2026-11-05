"""Pure LLM contract tests: actual resources, strict semantics and input provenance."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from china_stock_sim.common.strategy_definitions import get_definition, normalize_parameters
from china_stock_sim.common.wire import to_wire
from china_stock_sim.strategies.base import PlanningContext
from china_stock_sim.strategies.llm import build_messages, build_request, parse_response
from china_stock_sim.strategies.llm_prompt import PROMPT_REVISION, PROMPT_SHA256, response_schema


def _security(code='SH600000', *, amount='1000', eligibility='READY', in_pool=True,
              adjusted=None, close='10'):
    start = date(2026, 10, 10)
    bars = [{'code': code, 'date': (start + timedelta(days=i)).isoformat(),
             'close': close, 'volume': '100', 'amount': amount} for i in range(21)]
    return {'code': code, 'name': '证券' + code, 'in_pool': in_pool,
            'eligibility': eligibility, 'issues': [], 'close': close,
            'adjusted_close': adjusted or ['10'] * 21, 'raw_bars': bars}


def _context(securities=None, positions=(), **changes):
    p = normalize_parameters(get_definition('LLM-Daily'))
    ctx = PlanningContext(account_id='LLM', strategy_id='LLM-Daily',
        target_session='2026-11-02', market_as_of='2026-10-30',
        input_cutoff='2026-11-01T18:20:00+08:00', generated_at='2026-11-01T18:20:00+08:00',
        account={'available_cash_fen': 5_000_000, 'positions': list(positions)},
        policy={'parameters': p}, net_assets_fen=5_000_000,
        securities=securities if securities is not None else {'SH600000': _security()},
        fee_policy={'version': 'fixture', 'commission_rate': '0.0003'})
    return replace(ctx, **changes)


def _position(code='SH600000', quantity=100, sellable=None):
    return {'code': code, 'total_shares': quantity,
            'sellable_shares': quantity if sellable is None else sellable,
            'cost_fen': quantity * 1000}


def _intent(code='SH600000', side='BUY', index=0, **changes):
    item = {'code': code, 'side': side, 'reason': '依据所给指标保留流动性。',
            'basis_fields': ['ret_20d'], 'order_index': index}
    if side != 'HOLD':
        item['limit_price'] = '10.00'
        item['budget_fen' if side == 'BUY' else 'sell_fraction'] = 100_000 if side == 'BUY' else '0.5'
    item.update(changes)
    return item


def _response(intents=None, reason=None, **changes):
    result = {'target_session': '2026-11-02',
              'ordered_intents': [_intent()] if intents is None else intents, 'no_order_reason': reason}
    result.update(changes)
    return json.dumps(result, ensure_ascii=False)


def _codes(decision):
    return {issue['code'] for issue in decision.reasons}


def test_llm_features_use_twenty_returns_sample_denominator_and_last_twenty_amounts():
    adjusted = ['100' if i % 2 == 0 else '110' for i in range(21)]
    security = _security(adjusted=adjusted)
    security['raw_bars'][0]['amount'] = '99999999'
    for i, bar in enumerate(security['raw_bars'][1:], 1):
        bar['amount'] = str(i)
    row = build_request(_context({'SH600000': security}))['securities'][0]
    with localcontext() as ctx:
        ctx.prec = 34
        expected_volatility = (Decimal(20) / 19).sqrt() * Decimal(21) / 220
        assert abs(Decimal(row['volatility_20d']) - expected_volatility) < Decimal('1e-32')
        assert Decimal(row['ret_5d']) == Decimal(100) / 110 - 1
    assert row['ret_20d'] == '0'
    assert row['avg_amount_20d'] == '10.5'
    assert row['close'] == '10'  # adjusted signal price never becomes sizing/valuation price
    assert row['amount'] == '20'


def test_llm_twenty_candidates_are_liquidity_ranked_then_all_holdings_are_added():
    securities = {f'SH{600000+i}': _security(f'SH{600000+i}') for i in range(22)}
    securities['SZ000001'] = _security('SZ000001', amount='1', in_pool=False, eligibility='EXCLUDED')
    request = build_request(_context(securities, [_position('SH600021'), _position('SZ000001')]))
    assert request['buy_candidates'] == [f'SH{600000+i}' for i in range(20)]
    assert len(request['securities']) == 22
    rows = {row['code']: row for row in request['securities']}
    assert rows['SH600021']['allowed_actions'] == ['SELL', 'HOLD']
    assert rows['SZ000001']['allowed_actions'] == ['SELL', 'HOLD']
    assert request['holdings'] == ['SH600021', 'SZ000001']


def test_llm_candidate_liquidity_does_not_use_rounded_display_order():
    securities = {'SH600000': _security(amount='100.000000000000000000000001'),
                  'SH600001': _security('SH600001', amount='100.000000000000000000000002')}
    request = build_request(_context(securities))
    assert request['buy_candidates'] == ['SH600001', 'SH600000']


@pytest.mark.parametrize('mutate', [
    lambda s: s.update(adjusted_close=['10'] * 20),
    lambda s: s['raw_bars'].pop(),
    lambda s: s['raw_bars'][-1].update(date='2026-10-29'),
    lambda s: s['raw_bars'][0].update(date='2026-11-03'),
    lambda s: s['raw_bars'][0].update(code='SH600001'),
    lambda s: s['raw_bars'][0].update(amount=None),
    lambda s: s['raw_bars'][0].update(volume=True),
    lambda s: s.update(close='11'),
    lambda s: s.update(eligibility='UNAVAILABLE'),
])
def test_llm_unknown_holding_inputs_wait_without_selling_or_filling_zero(mutate):
    security = _security()
    mutate(security)
    request = build_request(_context({'SH600000': security}, [_position()]))
    assert request['state'] == 'WAITING_INPUT'
    assert request['reasons']
    with pytest.raises(ValueError):
        build_messages(request)


def test_llm_candidate_missing_inputs_preserves_issue_and_does_not_block_valid_other_candidate():
    bad = _security('SH600001')
    bad['adjusted_close'] = []
    bad['issues'] = [{'code': 'CORRUPT_COVERAGE', 'source': 'evidence-id'}]
    request = build_request(_context({'SH600000': _security(), 'SH600001': bad}))
    assert request['state'] == 'READY'
    assert request['buy_candidates'] == ['SH600000']
    assert request['excluded'][0]['issues'][0] == bad['issues'][0]
    bad['issues'][0]['source'] = 'changed'
    assert request['excluded'][0]['issues'][0]['source'] == 'evidence-id'


def test_llm_all_unknown_candidates_wait_but_confirmed_exclusions_allow_no_order():
    unknown = build_request(_context({'SH600000': _security(eligibility='UNAVAILABLE')}))
    assert unknown['state'] == 'WAITING_INPUT'
    excluded = build_request(_context({'SH600000': _security(eligibility='EXCLUDED')}))
    assert excluded['state'] == 'READY'
    assert parse_response(_response([], reason='全部候选已被明确排除。'), excluded).state == 'READY'


def test_llm_real_holdings_cash_and_weight_bound_new_buys_without_sale_netting():
    securities = {f'SH{600000+i}': _security(f'SH{600000+i}') for i in range(6)}
    positions = [_position(f'SH{600000+i}', quantity=1000) for i in range(5)]
    request = build_request(_context(securities, positions))
    assert request['resources']['stock_market_value_fen'] == 5_000_000
    assert request['resources']['remaining_new_slots'] == 0
    assert request['resources']['max_total_buy_budget_fen'] == 0
    assert all('BUY' not in row['allowed_actions'] for row in request['securities'])
    result = parse_response(_response([_intent(side='SELL'), _intent('SH600005', index=1)]), request)
    assert {'ACTION_NOT_ALLOWED', 'NEW_HOLDING_SLOTS_EXCEEDED', 'TOTAL_BUY_BUDGET_EXCEEDED'} <= _codes(result)


def test_llm_existing_holding_in_top_twenty_can_add_without_consuming_new_slot():
    ctx = _context(positions=[_position()])
    ctx.policy['parameters']['max_holdings'] = 1
    request = build_request(ctx)
    assert request['resources']['remaining_new_slots'] == 0
    assert request['securities'][0]['max_buy_budget_fen'] == 900_000
    assert parse_response(_response(), request).state == 'READY'


@pytest.mark.parametrize('equity', [0, -100])
def test_llm_nonpositive_equity_permits_existing_sell_or_hold_but_no_added_exposure(equity):
    request = build_request(_context(positions=[_position()], net_assets_fen=equity))
    row = request['securities'][0]
    assert row['current_weight'] is None
    assert 'current_weight' not in row['basis_fields']
    assert row['allowed_actions'] == ['SELL', 'HOLD']
    assert parse_response(_response([_intent(side='SELL')]), request).state == 'READY'


def test_llm_valid_intents_map_to_public_contract_and_basis_facts_are_separate():
    request = build_request(_context())
    before = deepcopy(request)
    result = parse_response(_response(), request)
    assert result.state == 'READY'
    assert result.ordered_intents == ({'code': 'SH600000', 'side': 'BUY',
        'reason': '依据所给指标保留流动性。', 'order_index': 0, 'limit_price': '10.00', 'budget_fen': 100_000},)
    assert 'basis_fields' not in result.ordered_intents[0]
    assert result.calculation['model_basis'][0]['basis_values'] == {'ret_20d': '0'}
    assert result.calculation['response_validation']['full_execution_validation_required'] is True
    assert request == before
    to_wire(result)


@pytest.mark.parametrize(('response', 'code'), [
    ('{}', 'RESPONSE_FIELDS'), ('[]', 'RESPONSE_FIELDS'), ('null', 'RESPONSE_FIELDS'),
    ('```json\n{}\n```', 'INVALID_JSON'), ('{} {}', 'INVALID_JSON'),
    ('{"target_session":"2026-11-02","target_session":"2026-11-02"}', 'DUPLICATE_KEY'),
    ('{"nested":{"x":1,"x":2}}', 'DUPLICATE_KEY'),
    ('{"x":1.0}', 'NON_INTEGER_JSON_NUMBER'), ('{"x":1e2}', 'NON_INTEGER_JSON_NUMBER'),
    ('{"x":NaN}', 'NON_INTEGER_JSON_NUMBER'), ('{"x":Infinity}', 'NON_INTEGER_JSON_NUMBER'),
    ('', 'EMPTY_RESPONSE'), ('\ud800', 'INVALID_JSON'),
    ('{"reason":"\\ud800"}', 'INVALID_JSON'),
])
def test_llm_json_rejects_duplicates_numbers_nonobjects_fences_and_trailing_text(response, code):
    result = parse_response(response, build_request(_context()))
    assert result.state == 'INVALID'
    assert code in _codes(result)


@pytest.mark.parametrize(('change', 'code'), [
    ({'code': 'sh600000'}, 'UNKNOWN_SECURITY'),
    ({'code': 'SH600001'}, 'UNKNOWN_SECURITY'),
    ({'quantity': 100}, 'INTENT_FIELDS'),
    ({'account_id': 'other'}, 'INTENT_FIELDS'),
    ({'order_index': True}, 'ORDER_INDEX_MISMATCH'),
    ({'order_index': 1}, 'ORDER_INDEX_MISMATCH'),
    ({'reason': ' '}, 'INVALID_REASON'),
    ({'reason': []}, 'INVALID_REASON'),
    ({'basis_fields': []}, 'INVALID_BASIS'),
    ({'basis_fields': ['news']}, 'INVALID_BASIS'),
    ({'basis_fields': ['ret_20d', 'ret_20d']}, 'INVALID_BASIS'),
    ({'basis_fields': [{}]}, 'INVALID_BASIS'),
    ({'basis_fields': ['ret_20d', 'ret_5d', 'close', 'volume']}, 'INVALID_BASIS'),
    ({'limit_price': '10.001'}, 'INVALID_TICK_PRICE'),
    ({'limit_price': 'NaN'}, 'INVALID_TICK_PRICE'),
    ({'limit_price': '-10'}, 'INVALID_TICK_PRICE'),
    ({'limit_price': '0'}, 'INVALID_TICK_PRICE'),
    ({'limit_price': 10}, 'INVALID_TICK_PRICE'),
    ({'limit_price': '1e999999999'}, 'INVALID_TICK_PRICE'),
    ({'limit_price': '+10.00'}, 'INVALID_TICK_PRICE'),
    ({'limit_price': '１０.００'}, 'INVALID_TICK_PRICE'),
    ({'budget_fen': 0}, 'INVALID_BUDGET'),
    ({'budget_fen': True}, 'INVALID_BUDGET'),
    ({'budget_fen': '100'}, 'INVALID_BUDGET'),
    ({'budget_fen': 1_000_001}, 'SYMBOL_BUDGET_EXCEEDED'),
])
def test_llm_buy_semantic_failures_are_structured_and_never_silently_fixed(change, code):
    result = parse_response(_response([_intent(**change)]), build_request(_context()))
    assert result.state == 'INVALID'
    assert result.ordered_intents == ()
    assert code in _codes(result)


@pytest.mark.parametrize('fraction', ['0', '-0.1', '1.01', 'NaN', 1, True,
                                     '1e-999999999', '+0.5', '０.５'])
def test_llm_invalid_sell_fraction(fraction):
    request = build_request(_context(positions=[_position()]))
    result = parse_response(_response([_intent(side='SELL', sell_fraction=fraction)]), request)
    assert 'INVALID_SELL_FRACTION' in _codes(result)


def test_llm_hold_requires_no_economic_fields_and_no_order_reason():
    request = build_request(_context())
    hold = _intent(side='HOLD', basis_fields=[])
    assert parse_response(_response([hold]), request).state == 'INVALID'
    valid = parse_response(_response([hold], '暂不交易。'), request)
    assert valid.state == 'READY'
    assert valid.ordered_intents[0]['limit_price'] is None
    assert parse_response(_response([dict(hold, budget_fen=1)], '暂不交易。'), request).state == 'INVALID'


def test_llm_hold_does_not_count_towards_five_trade_codes_but_is_in_sequence():
    securities = {f'SH{600000+i}': _security(f'SH{600000+i}') for i in range(7)}
    request = build_request(_context(securities))
    intents = [_intent(f'SH{600000+i}', index=i) for i in range(5)]
    intents += [_intent('SH600005', side='HOLD', index=5)]
    assert parse_response(_response(intents), request).state == 'READY'
    intents.append(_intent('SH600006', index=6))
    assert 'TOO_MANY_TRADE_CODES' in _codes(parse_response(_response(intents), request))


def test_llm_multi_order_cash_and_slots_checked_before_any_sale_release():
    securities = {f'SH{600000+i}': _security(f'SH{600000+i}') for i in range(4)}
    ctx = _context(securities, account={'positions': [], 'available_cash_fen': 150_000})
    request = build_request(ctx)
    intents = [_intent('SH600000'), _intent('SH600001', index=1)]
    assert 'TOTAL_BUY_BUDGET_EXCEEDED' in _codes(parse_response(_response(intents), request))
    ctx.policy['parameters']['max_holdings'] = 1
    request = build_request(replace(ctx, account={'positions': [], 'available_cash_fen': 5_000_000}))
    assert 'NEW_HOLDING_SLOTS_EXCEEDED' in _codes(parse_response(_response(intents), request))


def test_llm_duplicate_security_or_wrong_target_or_model_identity_is_rejected():
    request = build_request(_context(positions=[_position()]))
    intents = [_intent(), _intent(side='SELL', index=1)]
    assert 'DUPLICATE_SECURITY' in _codes(parse_response(_response(intents), request))
    assert 'WRONG_TARGET_SESSION' in _codes(parse_response(_response(target_session='2026-10-30'), request))
    assert 'RESPONSE_FIELDS' in _codes(parse_response(_response(strategy_id='S1'), request))


@pytest.mark.parametrize('finish_reason', ['length', 'tool_calls', 'unknown', '', None])
def test_llm_only_complete_stop_response_is_eligible_for_semantic_acceptance(finish_reason):
    result = parse_response(_response(), build_request(_context()), finish_reason=finish_reason)
    assert _codes(result) == {'INCOMPLETE_RESPONSE'}


def test_llm_fixed_prompt_and_single_correction_keep_the_exact_original_request():
    request = build_request(_context())
    before = deepcopy(request)
    first = build_messages(request)
    invalid = parse_response('{}', request)
    correction = build_messages(request, prior_response='{}', errors=invalid.reasons)
    assert len(first) == 2 and len(correction) == 4
    assert first == correction[:2]
    assert correction[2].role == 'assistant' and correction[2].content == '{}'
    feedback = json.loads(correction[3].content)
    assert feedback['errors'] == list(invalid.reasons)
    assert json.loads(first[1].content) == request
    assert request['prompt_revision'] == PROMPT_REVISION
    assert request['prompt_sha256'] == PROMPT_SHA256
    assert '不调用工具' in first[0].content
    assert request == before
    schema = response_schema()
    schema['properties'].clear()
    assert response_schema()['properties']
    with pytest.raises(ValueError):
        build_messages(request, prior_response='{}')
