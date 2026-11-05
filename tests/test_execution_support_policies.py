"""LX02/LX05: pure fee and frozen policy support, without a matching engine."""

import tomllib
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from china_stock_sim.common.fees import ExecutionSupportError, fee_breakdown, integer, money, number
from china_stock_sim.common.policies import (
    build_policy_bundle,
    derive_execution_context,
    load_policy_bundle,
    policy_digest,
    validate_execution_context,
    validate_execution_policy,
    validate_policy_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = '2026-10-30'
CUTOFF = '2026-10-29T20:00:00+08:00'


@pytest.fixture
def inputs():
    with (ROOT / 'configs/default.toml').open('rb') as stream:
        settings = tomllib.load(stream)
    with (ROOT / 'configs/execution_rules.toml').open('rb') as stream:
        registry = tomllib.load(stream)
    return registry, settings['execution'], settings['fees']


def freeze(inputs, **kwargs):
    return build_policy_bundle(*inputs, target_session=kwargs.get('target', TARGET),
                               knowledge_cutoff=kwargs.get('cutoff', CUTOFF))


def normal_status(**kwargs):
    return {'board': 'SSE_MAIN_A', 'is_st': 0, 'listing_session_number': 121,
            'listing_session_lower_bound': True, **kwargs}


def context(bundle, status=None, evidence=()):
    return derive_execution_context(bundle, code='SH600000', target_session=TARGET,
                                    status=normal_status() if status is None else status,
                                    status_evidence=evidence)


def test_fee_example_and_per_component_rounding(inputs):
    fees = inputs[2]
    buy = fee_breakdown('10', 1000, 'BUY', fees, date(2026, 10, 30))
    sell = fee_breakdown('10.30', 1000, 'SELL', fees, '2026-11-02')
    assert buy == {'amount_fen': 1000000, 'commission_fen': 500, 'stamp_fen': 0,
                   'transfer_fen': 10, 'fees_fen': 510, 'net_cash_fen': -1000510}
    assert sell == {'amount_fen': 1030000, 'commission_fen': 500, 'stamp_fen': 515,
                    'transfer_fen': 10, 'fees_fen': 1025, 'net_cash_fen': 1028975}
    assert 5000000 + buy['net_cash_fen'] == 3999490
    assert 5000000 + buy['net_cash_fen'] + sell['net_cash_fen'] == 5028465
    half_cent = fee_breakdown('10', 50, 'SELL', fees, TARGET)
    assert half_cent['transfer_fen'] == 1
    assert half_cent['stamp_fen'] == 25
    assert money(Decimal('-1.005')) == -101


@pytest.mark.parametrize('value', [True, 1.1, 'NaN', 'Infinity', '-1'])
def test_bad_numbers_are_public_errors(value):
    with pytest.raises(ExecutionSupportError) as error:
        number(value)
    assert error.value.code == 'INVALID_INPUT'
    assert isinstance(error.value, ValueError)


@pytest.mark.parametrize('value', [True, 1.0, '100', -1])
def test_share_and_fen_integers_reject_coercion(value):
    with pytest.raises(ExecutionSupportError):
        integer(value)


@pytest.mark.parametrize('side', [None, 'HOLD', 'buy'])
def test_fee_direction_cannot_be_omitted(inputs, side):
    with pytest.raises(ExecutionSupportError):
        fee_breakdown('10', 100, side, inputs[2], TARGET)


def test_fee_effective_boundaries_and_metadata(inputs):
    fees = deepcopy(inputs[2])
    assert fee_breakdown('10', 100, 'BUY', fees, '2023-08-27')['stamp_fen'] == 0
    with pytest.raises(ExecutionSupportError) as error:
        fee_breakdown('10', 100, 'SELL', fees, '2023-08-27')
    assert error.value.code == 'WAITING_DATA'
    assert fee_breakdown('10', 100, 'SELL', fees, '2023-08-28')['stamp_fen'] == 50
    fees['effective_to'] = TARGET
    fee_breakdown('10', 100, 'BUY', fees, TARGET)
    with pytest.raises(ExecutionSupportError):
        fee_breakdown('10', 100, 'BUY', fees, '2026-11-02')
    fees['commission_includes_exchange_fees'] = False
    with pytest.raises(ExecutionSupportError):
        fee_breakdown('10', 100, 'BUY', fees, TARGET)


@pytest.mark.parametrize('key,value', [
    ('max_daily_symbols_per_account', 6), ('buy_lot_size', 200),
    ('price_tick_yuan', '0.001'), ('partial_fills', True), ('t_plus_one', False),
    ('one_direction_per_symbol_per_day', False), ('use_same_open_sale_proceeds', True),
    ('reject_limit_up_buy', False), ('reject_limit_down_sell', False),
    ('slippage_bps', 1), ('slippage_bps', False), ('order_validity', 'forever'),
    ('model', 'INTRADAY'), ('max_order_shares', 1000001), ('unknown_option', True),
])
def test_execution_configuration_cannot_claim_unsupported_rules(inputs, key, value):
    policy = {**inputs[1], key: value}
    with pytest.raises(ExecutionSupportError) as error:
        validate_execution_policy(policy)
    assert error.value.code == 'INVALID_INPUT'


def test_bundle_is_deep_frozen_and_hashes_original_fee_dictionary(inputs):
    bundle = freeze(inputs)
    saved = deepcopy(bundle)
    inputs[0]['rules'][0]['source_clauses'].append('later change')
    inputs[1]['buy_lot_size'] = 200
    inputs[2]['commission_rate'] = '0.009'
    assert bundle == saved
    assert bundle['fee_policy_hash'] == policy_digest(bundle['fee_policy'])
    assert bundle['rule_policy_hash'] == policy_digest(bundle['execution_policy'])
    assert validate_policy_bundle(bundle) == bundle


def test_public_loader_matches_pure_builder(inputs):
    loaded = load_policy_bundle(ROOT / 'configs/execution_rules.toml', target_session=TARGET,
                                knowledge_cutoff=CUTOFF, execution_policy=inputs[1],
                                fee_policy=inputs[2])
    assert loaded == freeze(inputs)


def test_date_knowledge_and_overlapping_rule_selection(inputs):
    with pytest.raises(ExecutionSupportError) as error:
        freeze(inputs, target='2026-07-05')
    assert error.value.code == 'RULE_NOT_COVERED'
    inputs[0]['fee_policy_metadata']['known_at'] = '2026-01-01T00:00:00+08:00'
    assert freeze(inputs, target='2026-07-06', cutoff='2026-07-03T20:00:00+08:00')
    for rule in inputs[0]['rules']:
        rule['known_at'] = '2026-10-29T20:00:01+08:00'
    with pytest.raises(ExecutionSupportError) as error:
        freeze(inputs)
    assert error.value.code == 'RULE_NOT_COVERED'
    for rule in inputs[0]['rules']:
        rule['known_at'] = CUTOFF
    assert freeze(inputs)
    inputs[0]['rules'].append({**deepcopy(inputs[0]['rules'][0]), 'rule_id': 'overlap'})
    with pytest.raises(ExecutionSupportError) as error:
        freeze(inputs)
    assert error.value.code == 'CONFLICT'


def test_fee_knowledge_metadata_and_fee_period_are_not_assumed(inputs):
    inputs[0]['fee_policy_metadata']['known_at'] = '2026-10-29T20:00:01+08:00'
    with pytest.raises(ExecutionSupportError) as error:
        freeze(inputs)
    assert error.value.code == 'WAITING_RULE'
    inputs[0]['fee_policy_metadata']['known_at'] = CUTOFF
    inputs[2]['transfer_effective_from'] = '2026-11-02'
    with pytest.raises(ExecutionSupportError) as error:
        freeze(inputs)
    assert error.value.code == 'WAITING_RULE'


@pytest.mark.parametrize('code,board,risk', [
    ('SH600000', 'SSE_MAIN_A', 0), ('SH600000', 'SSE_MAIN_A', 1),
    ('SZ000001', 'SZSE_MAIN_A', 0), ('SZ000001', 'SZSE_MAIN_A', 1),
])
def test_current_main_board_and_st_are_ten_percent_without_invented_coverage(inputs, code, board, risk):
    status = normal_status(board=board, is_st=risk)
    result = derive_execution_context(freeze(inputs), code=code, target_session=TARGET, status=status)
    assert result['limit_rate'] == '0.10'
    assert result['special_status_unknown'] is False
    assert result['security_class'] == ('RISK_WARNING' if risk else 'ORDINARY')
    assert result['coverage_mode'] == 'KNOWN_ONLY_COVERAGE'
    assert 'relisting_first_session' not in result['status']


@pytest.mark.parametrize('changes,reason', [
    ({'listing_session_number': 1, 'listing_session_lower_bound': False}, 'IPO_FIRST_FIVE_SESSIONS'),
    ({'listing_session_number': 5, 'listing_session_lower_bound': False}, 'IPO_FIRST_FIVE_SESSIONS'),
    ({'relisting_first_session': True}, 'RELISTING_FIRST_SESSION'),
    ({'delisting_first_session': True}, 'DELISTING_FIRST_SESSION'),
])
def test_positive_special_day_facts_require_bound_evidence(inputs, changes, reason):
    bundle, status = freeze(inputs), normal_status(**changes)
    with pytest.raises(ExecutionSupportError):
        context(bundle, status)
    result = context(bundle, status, [{'observation_id': 'proven-status', 'date': TARGET}])
    assert result['no_price_limit'] is True
    assert result['no_price_limit_reasons'] == [reason]
    assert 'limit_rate' not in result


def test_sixth_listing_day_and_ambiguous_lower_bound(inputs):
    bundle = freeze(inputs)
    assert context(bundle, normal_status(listing_session_number=6))['limit_rate'] == '0.10'
    unknown = context(bundle, normal_status(listing_session_number=5))
    assert unknown['special_status_unknown'] is True
    assert 'LISTING_SESSION_BOUND_INCONCLUSIVE' in unknown['issues']
    assert 'limit_rate' not in unknown and 'no_price_limit' not in unknown


@pytest.mark.parametrize('changes', [{'special_status_unknown': True}, {'is_st': None},
                                    {'board': None}, {'listing_session_number': None}])
def test_unknown_status_never_means_normal_ten_percent(inputs, changes):
    result = context(freeze(inputs), normal_status(**changes))
    assert result['special_status_unknown'] is True
    assert 'limit_rate' not in result


def test_context_cannot_switch_rate_rule_bundle_status_or_evidence(inputs):
    bundle, status = freeze(inputs), normal_status()
    evidence = [{'observation_id': 'status-proof', 'date': TARGET}]
    original = context(bundle, status, evidence)
    assert validate_execution_context(original, bundle, code='SH600000', target_session=TARGET,
                                      status=status, status_evidence=evidence) == original
    for key, value in [('limit_rate', '0.20'), ('rule_id', 'other'), ('ruleset_hash', 'forged'),
                       ('status_evidence', []), ('security_class', 'RISK_WARNING')]:
        invalid = {**original, key: value}
        with pytest.raises(ExecutionSupportError) as error:
            validate_execution_context(invalid, bundle, code='SH600000', target_session=TARGET,
                                       status=status, status_evidence=evidence)
        assert error.value.code == 'INTEGRITY_ERROR'
    invalid_bundle = deepcopy(bundle)
    invalid_bundle['rules'][0]['source'] += '/tampered'
    with pytest.raises(ExecutionSupportError) as error:
        context(invalid_bundle)
    assert error.value.code == 'INTEGRITY_ERROR'


def test_cross_market_and_cross_session_are_rejected(inputs):
    bundle = freeze(inputs)
    with pytest.raises(ExecutionSupportError):
        context(bundle, normal_status(board='SZSE_MAIN_A'))
    with pytest.raises(ExecutionSupportError):
        derive_execution_context(bundle, code='SH600000', target_session='2026-11-02',
                                  status=normal_status())
