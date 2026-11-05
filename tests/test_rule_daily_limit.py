"""NL01-05: current scope, historical frozen bytes and actual execution gates."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

import pytest
from test_execution_matching import bundle
from test_execution_validation import inputs, run
from test_ledger_trading import DAY, book, call, plan_payload
from test_strategy_rules import context

from china_stock_sim.common.fees import ExecutionSupportError
from china_stock_sim.common.policies import (
    daily_symbol_limit,
    policy_digest,
    validate_execution_policy,
)
from china_stock_sim.common.strategy_definitions import get_definition, normalize_parameters
from china_stock_sim.execution.matching import DailyOpenMatcher
from china_stock_sim.strategies.rules import plan

assert book and inputs


@pytest.mark.parametrize('strategy', ['S1', 'S1-LT'])
def test_current_rule_parameters_zero_only_and_historical_defaults_keep_one_to_five(settings, strategy):
    definition = get_definition(strategy, settings)
    assert normalize_parameters(definition)['max_daily_symbols'] == 0
    for value in (1, 5, -1, True, '0', None):
        with pytest.raises(ValueError):
            normalize_parameters(definition, {'max_daily_symbols': value})
    old = deepcopy(definition)
    old['defaults']['max_daily_symbols'] = 5
    old['strategy_version'] = strategy + '-implementation-1'
    for value in (1, 5):
        assert normalize_parameters(old, {'max_daily_symbols': value})['max_daily_symbols'] == value
    with pytest.raises(ValueError):
        normalize_parameters(old, {'max_daily_symbols': 0})
    outcome = plan(context(strategy=strategy))
    assert len(outcome.ordered_intents) == len(outcome.targets) == 10
    assert not any(item['reason'] == 'DAILY_SYMBOL_LIMIT' for item in outcome.calculation['skipped'])


def test_llm_limit_and_policy_scope_are_strict_and_do_not_add_historical_fields(settings):
    definition = get_definition('LLM-Daily', settings)
    for value in (0, 6, True, '5'):
        with pytest.raises(ValueError):
            normalize_parameters(definition, {'max_daily_symbols': value})
    current = deepcopy(settings.raw['execution'])
    old = {k: v for k, v in current.items() if k != 'daily_symbol_limit_scope'}
    before = deepcopy(old)
    assert validate_execution_policy(old) == before
    assert 'daily_symbol_limit_scope' not in old
    for strategy in ('S1', 'S1-LT', 'LLM-Daily'):
        assert daily_symbol_limit(old, strategy) == 5
        assert daily_symbol_limit(current, strategy) == (5 if strategy == 'LLM-Daily' else None)
    for scope in ('OTHER', None, [], 0):
        with pytest.raises(ExecutionSupportError):
            daily_symbol_limit({**current, 'daily_symbol_limit_scope': scope}, 'S1')


def six_intents():
    return [{'code': f'SH{600000+i}', 'side': 'BUY', 'order_index': i,
             'reason': 'NL actual public execution', 'limit_price': '10.00', 'quantity': 100}
            for i in range(6)]


@pytest.mark.parametrize('account', ['S1', 'S1-LT'])
def test_six_rule_codes_validate_freeze_match_and_commit_with_real_reservations(inputs, account):
    book = inputs['book']
    assert call(book, 'activate_simulation', {'target_session': DAY}, account=account)['state'] == 'APPLIED'
    state = book.account_state(account, DAY)
    inputs['account'] = state
    inputs['p'].update(account_id=account, strategy_id=account,
        account_revision=state['account_revision'], account_snapshot_id=state['state_id'],
        ordered_intents=six_intents())
    checked = run(inputs)
    assert checked['status'] == 'VALID', checked
    assert len(checked['accepted_orders']) == 6
    saved = call(book, 'freeze_plan', checked['plan_envelope'], account=account, op='six-freeze')
    assert saved['state'] == 'APPLIED', saved
    frozen = book.get_plan(account, saved['plan_id'])
    assert len(frozen['orders']) == 6 and frozen['rule_policy']['daily_symbol_limit_scope'] == 'LLM_ONLY'
    assert book.account_state(account, DAY)['reserved_cash_fen'] > 600_000
    book.test_now[0] = datetime.fromisoformat(DAY + 'T20:00:00+08:00')
    matcher = DailyOpenMatcher(clock=book._now)
    results = matcher.match(frozen, book.execution_view(account, saved['plan_id']), bundle(book, frozen))
    assert len(results) == 6 and all(result.state == 'SIMULATED_FILL' for result in results)
    for index, result in enumerate(results):
        # Each commit verifies the current account view; recompute the remaining
        # pure result after the prior real financial mutation.
        pending = matcher.match(frozen, book.execution_view(account, saved['plan_id']), bundle(book, frozen))
        actual = next(item for item in pending if item.order_id == result.order_id)
        applied = call(book, 'apply_execution_results', asdict(actual), account=account, op=f'six:{index}')
        assert applied['state'] == 'APPLIED', applied
        assert call(book, 'apply_execution_results', asdict(actual), account=account,
                    op=f'six:{index}', expected=0)['state'] == 'ALREADY_APPLIED'
    assert book.fills(account)['total'] == 6
    assert book.account_state(account, DAY)['reserved_cash_fen'] == 0


def test_llm_six_codes_rejected_by_validator_and_freeze_and_identity_cannot_be_spoofed(inputs):
    inputs['p']['ordered_intents'] = six_intents()
    assert run(inputs)['status'] == 'INVALID_PLAN'
    inputs['p']['strategy_id'] = 'S1'
    assert run(inputs)['status'] == 'INVALID_INPUT'
    book = inputs['book']
    before = book.settings.database.read_bytes()
    raw = plan_payload(book, orders=six_intents())
    assert call(book, 'freeze_plan', raw)['state'] == 'INVALID_INPUT'
    raw['strategy_id'] = 'S1'
    assert call(book, 'freeze_plan', raw)['state'] == 'INVALID_INPUT'
    assert book.settings.database.read_bytes() == before


def test_matcher_independently_rejects_six_llm_orders(book):
    raw = plan_payload(book, orders=six_intents()[:5])
    saved = call(book, 'freeze_plan', raw)
    frozen = book.get_plan('LLM', saved['plan_id'])
    book.test_now[0] = datetime.fromisoformat(DAY+'T20:00:00+08:00')
    market = bundle(book, frozen)
    view = book.execution_view('LLM', saved['plan_id'])
    # Self-consistent pure input, never saved: cap must be checked even before
    # later per-order/source checks. A ledger commit also verifies the original.
    frozen['orders'].append({**frozen['orders'][0], 'code': 'SH600005', 'order_id': 'sixth'})
    frozen['document']['content_hash'] = policy_digest({k: v for k, v in frozen.items()
                                                       if k not in {'document', 'id'}})
    view['frozen_plan'] = deepcopy(frozen)
    view['view_hash'] = policy_digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
    with pytest.raises(ExecutionSupportError, match='冻结订单集合非法'):
        DailyOpenMatcher(clock=book._now).match(frozen, view, market)


def test_old_frozen_execution_policy_is_not_rewritten_when_current_scope_changes(book):
    book.settings.raw['execution'].pop('daily_symbol_limit_scope')
    raw = plan_payload(book)
    saved = call(book, 'freeze_plan', raw)
    assert saved['state'] == 'APPLIED'
    frozen = book.get_plan('LLM', saved['plan_id'])
    original = deepcopy(frozen)
    original_document = (book.root / frozen['document']['path']).read_bytes()
    book.settings.raw['execution']['daily_symbol_limit_scope'] = 'LLM_ONLY'
    book.test_now[0] = datetime.fromisoformat(DAY + 'T20:00:00+08:00')
    result = DailyOpenMatcher(clock=book._now).match(
        frozen, book.execution_view('LLM', saved['plan_id']), bundle(book, frozen))[0]
    assert call(book, 'apply_execution_results', asdict(result))['state'] == 'APPLIED'
    assert frozen == original and 'daily_symbol_limit_scope' not in frozen['rule_policy']
    assert (book.root / frozen['document']['path']).read_bytes() == original_document
