"""SL01/SL02 acceptance: original account identities and future policy transactions."""

from datetime import datetime

import pytest
from test_interface_accounts import contents
from test_ledger_trading import DAY, book, call, plan_payload, revision

from china_stock_sim.common.strategy_definitions import get_definition, normalize_parameters
from china_stock_sim.ledger.kernel import LedgerError, ledger_lock

assert book


def opening(account='NEW', *, strategy='S1', principal=123_456, **extra):
    return {'account_id': account, 'display_name': '独立账户 ' + account,
            'strategy_id': strategy, 'initial_cash_fen': principal,
            'strategy_parameters': {}, **extra}


def policy_change(book, target=DAY, *, account='LLM', **extra):
    return call(book, 'schedule_account_policy', {
        'effective_session': target, 'reason': 'isolated policy revision', **extra,
    }, account=account)


def test_builtin_definitions_normalize_full_inherited_parameters(settings):
    params = {identity: normalize_parameters(get_definition(identity, settings))
              for identity in ['S1', 'S1-LT', 'LLM-Daily']}
    for key in ['lookback_sessions', 'volatility_sessions', 'momentum_skip_sessions',
                'momentum_weight', 'low_volatility_weight', 'target_count']:
        assert params['S1-LT'][key] == params['S1'][key]
    assert params['S1-LT']['enter_rank'] == 10
    assert params['S1-LT']['retain_rank'] == 20
    assert params['S1']['buy_limit_offset'] == params['S1']['sell_limit_offset'] == '0.01'
    assert params['LLM-Daily']['model_ref'] == 'daily_planner'
    assert all('enabled' not in p for p in params.values())


@pytest.mark.parametrize('strategy,parameters', [
    ('S1', {'target_count': True}), ('S1', {'target_count': 0}),
    ('S1', {'target_count': 101}), ('S1', {'unknown': 1}),
    ('S1', {'target_weight': 0.1}), ('S1', {'target_weight': 'NaN'}),
    ('S1', {'lookback_sessions': 20}), ('S1', {'momentum_weight': '0.7'}),
    ('S1', {'buy_limit_offset': '0.11'}), ('S1', {'rebalance': 'weekly'}),
    ('S1-LT', {'enter_rank': 21, 'retain_rank': 20}),
    ('S1-LT', {'trim_target': '0.2', 'trim_trigger': '0.12'}),
    ('LLM-Daily', {'allow_tools': True}), ('LLM-Daily', {'max_attempts_per_target_session': 3}),
    ('LLM-Daily', {'max_target_weight': '0.9', 'max_total_target_weight': '0.8'}),
])
def test_invalid_account_parameters_never_create_partial_account(book, strategy, parameters):
    before = book.settings.database.read_bytes()
    result = book.create_account(opening(strategy=strategy, strategy_parameters=parameters), operation_id='invalid')
    assert result['state'] == 'INVALID_INPUT'
    assert book.settings.database.read_bytes() == before
    assert 'NEW' not in {r['account_id'] for r in book.list_accounts()}


@pytest.mark.parametrize('principal', [True, 0, -1, 1.5, '5000000', 2**63])
def test_account_principal_type_and_range_reject_without_financial_write(book, principal):
    before = book.settings.database.read_bytes()
    result = book.create_account(opening(principal=principal), operation_id='invalid-money')
    assert result['state'] == 'INVALID_INPUT'
    assert book.settings.database.read_bytes() == before


def test_distinct_accounts_preserve_original_three_and_have_unique_base_and_funding(book):
    original = book.list_accounts()
    for identity, principal in [('A', 1), ('B', 9_876_543)]:
        result = book.create_account(opening(identity, principal=principal), operation_id='open:' + identity)
        assert result['state'] == 'APPLIED'
        account = book.get_account_profile(identity)
        assert account['cash_fen'] == account['initial_cash_fen'] == principal
        assert account['opening_entries'] == 1 and account['simulation_start_session'] is None
        policies = book.account_policies(identity)
        assert len(policies) == 1 and policies[0]['effective_session'] is None
        assert policies[0]['policy']['account_id'] == identity
    assert [r for r in book.list_accounts() if r['account_id'] not in {'A', 'B'}] == original


def test_opening_replays_remain_original_after_future_policy_and_display_change(book):
    payload = opening()
    assert book.create_account(payload, operation_id='open')['state'] == 'APPLIED'
    assert policy_change(book, account='NEW', display_name='later label',
        strategy_parameters={'target_count': 8})['state'] == 'APPLIED'
    for operation in ['open', 'repeat-new-operation']:
        assert book.create_account(payload, operation_id=operation)['state'] == 'ALREADY_APPLIED'
    for change in [{'initial_cash_fen': 1}, {'strategy_id': 'S1-LT'},
                   {'strategy_parameters': {'target_count': 7}}]:
        assert book.create_account({**payload, **change}, operation_id='different')['state'] == 'CONFLICT'
    assert book.get_account_profile('NEW')['opening_entries'] == 1


def test_opening_replay_does_not_resolve_old_request_against_changed_current_defaults(book):
    payload = opening()
    assert book.create_account(payload, operation_id='open')['state'] == 'APPLIED'
    book.settings.raw['strategies']['s1']['target_count'] = 8
    for operation in ['open', 'another-operation']:
        result = book.create_account(payload, operation_id=operation)
        assert result['state'] == 'ALREADY_APPLIED', result
    assert book.get_account_policy('NEW', DAY)['policy']['parameters']['target_count'] == 10
    assert book.get_account_profile('NEW')['opening_entries'] == 1


def test_opening_failure_at_final_validation_rolls_back_account_money_and_policy(book, monkeypatch):
    import china_stock_sim.ledger.account_policies as module

    original = module.validate_accounts
    calls = []

    def fail_after_write(db, settings):
        calls.append(None)
        if len(calls) == 2:
            raise ValueError('injected after account, funding, policy and receipt')
        return original(db, settings)

    before = book.settings.database.read_bytes()
    monkeypatch.setattr(module, 'validate_accounts', fail_after_write)
    result = book.create_account(opening(), operation_id='partial')
    assert result['state'] == 'INVALID_INPUT'
    assert book.settings.database.read_bytes() == before
    assert 'NEW' not in {r['account_id'] for r in book.list_accounts()}


def test_policy_selection_immutable_projection_and_current_config_independence(book):
    before = book.get_account_policy('LLM', DAY)
    baseline_revision = revision(book)
    one = policy_change(book, strategy_parameters={'max_candidates': 10})
    two = policy_change(book, '2026-11-02', strategy_parameters={'max_holdings': 3})
    assert one['state'] == two['state'] == 'APPLIED'
    assert revision(book) == baseline_revision
    assert book.get_account_policy('LLM', '2026-10-29')['policy_hash'] == before['policy_hash']
    assert book.get_account_policy('LLM', DAY)['policy_id'] == one['policy_id']
    later = book.get_account_policy('LLM', '2026-11-03')
    assert later['policy_id'] == two['policy_id']
    assert later['policy']['parameters']['max_candidates'] == 10
    assert later['policy']['parameters']['max_holdings'] == 3
    assert set(later['policy']) == {'policy_id', 'account_id', 'strategy_id', 'strategy_version',
        'effective_from', 'effective_to', 'known_at', 'parameters'}
    assert all(r['policy']['effective_to'] is None for r in book.account_policies('LLM'))
    book.settings.raw['strategies']['llm']['max_candidates'] = 1
    assert book.get_account_policy('LLM', '2026-11-03') == later


def test_account_profile_display_name_changes_only_when_policy_becomes_effective(book):
    assert book.create_account(opening(), operation_id='open-display')['state'] == 'APPLIED'
    original = book.get_account_profile('NEW')['display_name']
    assert policy_change(book, account='NEW', display_name='生效后的名称')['state'] == 'APPLIED'
    assert book.get_account_profile('NEW')['display_name'] == original
    book.test_now[0] = datetime.fromisoformat(DAY + 'T08:00:00+08:00')
    assert book.get_account_profile('NEW')['display_name'] == '生效后的名称'
    assert book.get_account_policy('NEW', '2026-10-29')['display_name'] == original


@pytest.mark.parametrize('target', ['2026-10-29', '2026-10-31', '2040-01-01'])
def test_policy_unknown_non_session_or_expired_target_does_not_write(book, target):
    before = contents(book.root)
    result = policy_change(book, target)
    assert result['state'] in {'INVALID_INPUT', 'NOT_DUE', 'WAITING_DATA'}
    assert contents(book.root) == before


def test_policy_nine_oclock_equal_boundary_is_closed(book):
    book.test_now[0] = datetime.fromisoformat(DAY + 'T09:00:00+08:00')
    assert policy_change(book)['state'] in {'INVALID_INPUT', 'NOT_DUE'}


def test_policy_mutation_cannot_race_an_existing_ledger_writer(book):
    before = book.settings.database.read_bytes()
    with ledger_lock(book.root):
        assert policy_change(book)['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before


def test_future_attempt_blocks_policy_at_or_before_its_target_but_not_later(book):
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['state'] == 'APPLIED'
    assert policy_change(book)['state'] == 'CONFLICT'
    assert policy_change(book, '2026-11-02')['state'] == 'APPLIED'


def test_future_attempt_blocks_policy_earlier_than_occupied_target(book):
    assert book.create_account(opening('FUTURE', strategy='LLM-Daily'),
                               operation_id='future-account')['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': '2026-11-02'},
                account='FUTURE')['state'] == 'APPLIED'
    assert call(book, 'claim_model_attempt', {'target_session': '2026-11-02'},
                account='FUTURE')['state'] == 'APPLIED'
    assert policy_change(book, DAY, account='FUTURE')['state'] == 'CONFLICT'
    assert len(book.account_policies('FUTURE')) == 1


def test_any_unsettled_order_blocks_even_later_policy_changes(book):
    assert call(book, 'freeze_plan', plan_payload(book))['state'] == 'APPLIED'
    assert policy_change(book, '2026-11-02')['state'] == 'CONFLICT'


def test_active_parent_blocks_policy_and_expiration_without_orders_releases_it(book):
    result = call(book, 'save_rebalance_task', {'task_id': 'parent', 'start_session': DAY,
        'end_session': DAY, 'targets': [{'code': 'SH600000', 'side': 'BUY', 'quantity': 100}]})
    assert result['state'] == 'APPLIED'
    assert policy_change(book, '2026-11-02')['state'] == 'CONFLICT'
    book.test_now[0] = datetime.fromisoformat(DAY + 'T20:00:00+08:00')
    assert policy_change(book, '2026-11-02')['state'] == 'APPLIED'


def test_policy_original_damage_is_rejected_without_silent_reconstruction(book):
    policy = book.get_account_policy('LLM', DAY)
    (book.root / policy['document']['path']).write_text('{}')
    with pytest.raises(LedgerError) as failure:
        book.get_account_policy('LLM', DAY)
    assert failure.value.state == 'INTEGRITY_ERROR'
    assert policy_change(book)['state'] == 'INTEGRITY_ERROR'
