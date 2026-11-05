"""SL03 and explicit account/policy CLI contracts on isolated real ledgers."""

import json
from copy import deepcopy

import pytest
from test_interface_accounts import contents
from test_ledger_trading import DAY, book, call, plan_payload, revision

from china_stock_sim.cli import CAPABILITIES, main
from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.evidence import document

assert book


def register_model(monkeypatch, identity='FutureModel', *, uses_model=True, maximum=2):
    definition = {
        'strategy_version': 'fixture-model-v1', 'uses_model': uses_model,
        'defaults': {'model_ref': 'fixture_model', 'max_attempts_per_target_session': maximum},
        'implementation_hash': 'a' * 64,
    }
    monkeypatch.setitem(STRATEGY_DEFINITIONS, identity, definition)
    return definition


def create_model_account(book, monkeypatch, account='MODEL-X', *, uses_model=True, maximum=2):
    register_model(monkeypatch, uses_model=uses_model, maximum=maximum)
    result = book.create_account({
        'account_id': account, 'display_name': account, 'initial_cash_fen': 123_456,
        'strategy_id': 'FutureModel', 'strategy_parameters': {},
    }, operation_id='create:' + account)
    assert result['state'] == 'APPLIED', result
    assert call(book, 'activate_simulation', {'target_session': DAY}, account=account)['state'] == 'APPLIED'
    return account


def rows(book, account='LLM'):
    return book.report_snapshot(account)['model_attempts']


def finish(book, attempt, status, *, account='LLM', **extra):
    return call(book, 'finish_model_attempt', {
        'target_session': DAY, 'attempt_id': attempt['attempt_id'], 'status': status, **extra,
    }, account=account)


def prepare_model_decision(book, account):
    payload = {
        'target_session': DAY,
        'market_as_of': book.market.calendar_operation('previous_session', DAY).data,
        'input_cutoff': book._now().isoformat(), 'expected_sequence': 0,
        'input_bundle': {'market_snapshot_ids': [], 'data_evidence': [],
                         'account_revision': revision(book, account)},
    }
    result = call(book, 'prepare_strategy_decision', payload, account=account)
    assert result['state'] == 'APPLIED', result
    return result, payload


def use_cli_book(book, monkeypatch):
    monkeypatch.setattr('china_stock_sim.ledger.cli.LedgerService', lambda root: book)
    monkeypatch.setattr('china_stock_sim.cli.model_status',
                        lambda *a, **k: pytest.fail('ledger commands must not read model settings'))
    monkeypatch.setattr('china_stock_sim.cli.create_model_client',
                        lambda *a, **k: pytest.fail('ledger commands must not create model clients'))


def cli(book, capsys, arguments):
    code = main(['--root', str(book.root), *arguments])
    captured = capsys.readouterr()
    return code, json.loads(captured.out or captured.err)


def test_non_literal_model_strategy_uses_frozen_policy_and_survives_registry_change(book, monkeypatch):
    account = create_model_account(book, monkeypatch)
    policy = book.get_account_policy(account, DAY)
    original = deepcopy(STRATEGY_DEFINITIONS['FutureModel'])
    monkeypatch.setitem(STRATEGY_DEFINITIONS, 'FutureModel', {**original, 'uses_model': False})
    result = call(book, 'claim_model_attempt', {'target_session': DAY}, account=account)
    assert result['state'] == 'APPLIED', result
    attempt = rows(book, account)[0]
    assert attempt['strategy_id'] == 'FutureModel'
    assert attempt['account_policy_hash'] == policy['policy_hash']
    assert attempt['account_policy'] == policy['policy']
    assert attempt['strategy_definition']['uses_model'] is True
    assert attempt['model_ref'] == 'fixture_model'
    assert attempt['account_revision_at_claim'] == revision(book, account)


@pytest.mark.parametrize('override', [
    {'uses_model': True}, {'uses_model': False}, {'definition': {'uses_model': True}},
    {'model_ref': 'other_model'}, {'strategy_id': 'S1'},
    {'account_policy_hash': '0' * 64}, {'account_policy_id': 'invented'},
])
def test_model_claim_rejects_caller_capability_and_policy_override(book, override):
    result = call(book, 'claim_model_attempt', {'target_session': DAY, **override})
    assert result['state'] == 'INVALID_INPUT'
    assert rows(book) == []


def test_rule_capability_cannot_be_turned_into_model_by_payload(book, monkeypatch):
    account = create_model_account(book, monkeypatch, uses_model=False)
    assert call(book, 'claim_model_attempt', {
        'target_session': DAY, 'uses_model': True,
    }, account=account)['state'] == 'INVALID_INPUT'
    assert rows(book, account) == []


def test_model_attempts_remain_account_target_scoped_and_restart_durable(book, monkeypatch):
    account = create_model_account(book, monkeypatch)
    first = call(book, 'claim_model_attempt', {'target_session': DAY}, account=account)
    assert first['attempt_number'] == 1
    assert finish(book, first, 'TIMEOUT', account=account)['state'] == 'APPLIED'
    restarted = LedgerService(book.settings, market=book.market, clock=lambda: book.test_now[0], test_mode=True)
    second = call(restarted, 'claim_model_attempt', {'target_session': DAY}, account=account)
    assert second['attempt_number'] == 2
    assert finish(restarted, second, 'INTERRUPTED', account=account)['state'] == 'APPLIED'
    assert call(restarted, 'claim_model_attempt', {'target_session': DAY}, account=account)['state'] == 'CONFLICT'
    assert call(restarted, 'claim_model_attempt', {'target_session': DAY})['attempt_number'] == 1
    assert len(rows(restarted, account)) == 2


def test_lower_persisted_attempt_limit_is_respected(book, monkeypatch):
    account = create_model_account(book, monkeypatch, maximum=1)
    first = call(book, 'claim_model_attempt', {'target_session': DAY}, account=account)
    assert finish(book, first, 'FAILED', account=account)['state'] == 'APPLIED'
    assert call(book, 'claim_model_attempt', {'target_session': DAY}, account=account)['state'] == 'CONFLICT'


def test_legacy_unbound_attempt_can_finish_but_cannot_gain_current_policy_for_retry(book):
    # An existing schema2 attempt has no policy/input binding. Migration preserves it.
    def insert_legacy(tx):
        tx.append('model_attempts', 'legacy:1', {
            'attempt_id': 'legacy:1', 'target_session': DAY, 'attempt_number': 1,
            'status': 'CLAIMED', 'claimed_at': tx.now, 'model_ref': 'daily_planner',
            'request_document': document(book.root, 'model-requests', {}),
            'response_document': None,
        })
        return {}

    assert book._mutate('LLM', 'legacy-fixture', {}, revision(book), DAY,
                        insert_legacy, financial=False)['state'] == 'APPLIED'
    attempt = {'attempt_id': 'legacy:1'}
    assert finish(book, attempt, 'RECEIVED', response={'text': 'legacy response'})['state'] == 'APPLIED'
    assert finish(book, attempt, 'INVALID')['state'] == 'APPLIED'
    rejected = call(book, 'claim_model_attempt', {'target_session': DAY})
    assert rejected['state'] == 'CONFLICT' and '政策' in rejected['reason']
    assert len(rows(book)) == 1 and 'account_policy_hash' not in rows(book)[0]


@pytest.mark.parametrize('status', ['SUCCEEDED', 'INVALID'])
def test_response_must_be_received_before_semantic_outcome(book, status):
    attempt = call(book, 'claim_model_attempt', {'target_session': DAY})
    assert finish(book, attempt, status, response={'orders': []})['state'] == 'CONFLICT'
    assert rows(book)[0]['status'] == 'CLAIMED'
    assert rows(book)[0]['response_document'] is None


def test_received_response_is_immutable_and_success_never_reasks(book):
    attempt = call(book, 'claim_model_attempt', {'target_session': DAY})
    response = {'text': '{"ordered_intents": []}', 'finish_reason': 'stop'}
    assert finish(book, attempt, 'RECEIVED', response=response)['state'] == 'APPLIED'
    stored = rows(book)[0]['response_document']
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['state'] == 'CONFLICT'
    assert finish(book, attempt, 'SUCCEEDED', response={'text': 'changed'})['state'] == 'CONFLICT'
    assert finish(book, attempt, 'SUCCEEDED')['state'] == 'APPLIED'
    assert rows(book)[0]['response_document'] == stored
    assert finish(book, attempt, 'SUCCEEDED')['state'] == 'ALREADY_APPLIED'
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['state'] == 'CONFLICT'


def test_response_can_be_saved_after_financial_revision_changes_without_changing_input(book):
    attempt = call(book, 'claim_model_attempt', {'target_session': DAY})
    claimed_revision = revision(book)
    assert call(book, 'freeze_plan', plan_payload(book, orders=[]))['state'] == 'APPLIED'
    assert revision(book) > claimed_revision
    payload = {'target_session': DAY, 'attempt_id': attempt['attempt_id'],
               'status': 'RECEIVED', 'response': {'text': 'late network response'}}
    assert call(book, 'finish_model_attempt', payload, expected=claimed_revision)['state'] == 'STALE_ACCOUNT'
    assert call(book, 'finish_model_attempt', payload)['state'] == 'APPLIED'
    assert rows(book)[0]['account_revision_at_claim'] == claimed_revision
    assert finish(book, attempt, 'SUCCEEDED', account_revision_at_claim=revision(book))['state'] == 'CONFLICT'


def test_prepared_decision_requires_matching_identity_and_frozen_input_for_every_attempt(book, monkeypatch):
    account = create_model_account(book, monkeypatch)
    prepared, _ = prepare_model_decision(book, account)
    identity = {'target_session': DAY, 'decision_id': prepared['decision_id'],
                'input_hash': prepared['input_hash']}
    assert call(book, 'claim_model_attempt', {'target_session': DAY}, account=account)['state'] == 'CONFLICT'
    assert call(book, 'claim_model_attempt', {**identity, 'input_hash': '0' * 64},
                account=account)['state'] == 'CONFLICT'
    first = call(book, 'claim_model_attempt', identity, account=account)
    assert first['state'] == 'APPLIED'
    assert finish(book, first, 'RECEIVED', account=account, response={'text': 'invalid'})['state'] == 'APPLIED'
    assert finish(book, first, 'INVALID', account=account)['state'] == 'APPLIED'
    second = call(book, 'claim_model_attempt', identity, account=account)
    assert second['state'] == 'APPLIED' and second['attempt_number'] == 2
    assert all(row['decision_id'] == prepared['decision_id']
               and row['input_hash'] == prepared['input_hash'] for row in rows(book, account))
    assert call(book, 'schedule_account_policy', {
        'effective_session': DAY, 'strategy_parameters': {'model_ref': 'changed'},
        'reason': 'must not rebind an occupied target',
    }, account=account)['state'] == 'CONFLICT'


def test_unbound_attempt_cannot_be_adopted_by_later_prepared_decision(book, monkeypatch):
    account = create_model_account(book, monkeypatch)
    first = call(book, 'claim_model_attempt', {'target_session': DAY}, account=account)
    assert first['state'] == 'APPLIED'
    assert finish(book, first, 'TIMEOUT', account=account)['state'] == 'APPLIED'
    result = call(book, 'prepare_strategy_decision', {
        'target_session': DAY,
        'market_as_of': book.market.calendar_operation('previous_session', DAY).data,
        'input_cutoff': book._now().isoformat(), 'expected_sequence': 0,
        'input_bundle': {'market_snapshot_ids': [], 'data_evidence': [],
                         'account_revision': revision(book, account)},
    }, account=account)
    assert result['state'] == 'CONFLICT'


def test_cli_account_creation_explicit_and_idempotent_without_starting_observation(book, monkeypatch, capsys):
    use_cli_book(book, monkeypatch)
    path = book.root / 'create-account.json'
    path.write_text(json.dumps({'account_id': 'NEW', 'display_name': '新账户',
        'initial_cash_fen': 765_432, 'strategy_id': 'S1', 'strategy_parameters': {}}))
    arguments = ['account-create', '--input', str(path), '--operation-id', 'open-new']
    code, result = cli(book, capsys, arguments)
    assert code == 0 and result['state'] == 'APPLIED'
    profile = book.get_account_profile('NEW')
    assert profile['initial_cash_fen'] == 765_432
    assert profile['simulation_start_session'] is None
    assert profile['opening_entries'] == 1
    code, result = cli(book, capsys, arguments)
    assert code == 0 and result['state'] == 'ALREADY_APPLIED'
    assert book.get_account_profile('NEW')['cash_fen'] == 765_432


def test_cli_policy_and_decision_queries_are_read_only(book, monkeypatch, capsys):
    use_cli_book(book, monkeypatch)
    before = contents(book.root)
    code, result = cli(book, capsys, ['account-policy', '--account', 'LLM', '--session', DAY])
    assert code == 0
    assert result['account_policy']['policy']['strategy_id'] == 'LLM-Daily'
    assert result['account_selection']['account_id'] == 'LLM'
    code, result = cli(book, capsys, ['strategy-decision', '--account', 'LLM', '--session', DAY])
    assert code == 2 and result['state'] == 'NOT_FOUND' and result['decision'] is None
    assert contents(book.root) == before


def test_cli_policy_write_requires_explicit_effective_session(book, monkeypatch, capsys):
    use_cli_book(book, monkeypatch)
    path = book.root / 'policy-input.json'
    path.write_text(json.dumps({'strategy_parameters': {'max_candidates': 10}}))
    before = contents(book.root)
    code, result = cli(book, capsys, ['account-policy-set', '--account', 'LLM', '--input', str(path),
        '--operation-id', 'policy-write', '--expected-revision', str(revision(book))])
    assert code == 2 and 'effective_session' in result['reason']
    assert contents(book.root) == before


def test_cli_policy_write_and_read_keep_financial_revision_and_previous_policy(book, monkeypatch, capsys):
    use_cli_book(book, monkeypatch)
    before = book.get_account_policy('LLM', DAY)
    old_revision = revision(book)
    path = book.root / 'future-policy.json'
    path.write_text(json.dumps({'effective_session': DAY, 'reason': 'fewer candidates',
                               'strategy_parameters': {'max_candidates': 10}}))
    args = ['account-policy-set', '--account', 'LLM', '--input', str(path),
            '--operation-id', 'policy-new', '--expected-revision', str(old_revision)]
    code, result = cli(book, capsys, args)
    assert code == 0 and result['state'] == 'APPLIED'
    assert revision(book) == old_revision
    code, result = cli(book, capsys, args)
    assert code == 0 and result['state'] == 'ALREADY_APPLIED'
    assert book.get_account_policy('LLM', '2026-10-29')['policy_hash'] == before['policy_hash']
    code, result = cli(book, capsys, ['account-policy', '--account', 'LLM', '--session', DAY])
    assert code == 0 and result['account_policy']['policy']['parameters']['max_candidates'] == 10


def test_cli_ledger_apply_prepares_decision_and_both_read_filters_find_same_record(book, monkeypatch, capsys):
    account = create_model_account(book, monkeypatch)
    use_cli_book(book, monkeypatch)
    path = book.root / 'prepare-decision.json'
    path.write_text(json.dumps({
        'target_session': DAY,
        'market_as_of': book.market.calendar_operation('previous_session', DAY).data,
        'input_cutoff': book._now().isoformat(), 'expected_sequence': 0,
        'input_bundle': {'market_snapshot_ids': [], 'data_evidence': [],
                         'account_revision': revision(book, account)},
    }))
    code, result = cli(book, capsys, ['ledger-apply', '--action', 'prepare_strategy_decision',
        '--account', account, '--input', str(path), '--operation-id', 'prepare-cli',
        '--expected-revision', str(revision(book, account))])
    assert code == 0 and result['decision_state'] == 'PREPARED'
    before = contents(book.root)
    code, by_id = cli(book, capsys, ['strategy-decision', '--account', account,
                                    '--decision-id', result['decision_id']])
    assert code == 0 and by_id['state'] == 'FOUND'
    code, by_day = cli(book, capsys, ['strategy-decision', '--account', account, '--session', DAY])
    assert code == 0 and by_day == by_id
    assert contents(book.root) == before


def test_cli_rejects_write_input_outside_project(book, monkeypatch, capsys, tmp_path):
    use_cli_book(book, monkeypatch)
    outside = tmp_path / 'outside.json'
    outside.write_text('{}')
    before = contents(book.root)
    code, result = cli(book, capsys, ['account-create', '--input', str(outside), '--operation-id', 'escape'])
    assert code == 2 and '本项目内' in result['reason']
    assert contents(book.root) == before


@pytest.mark.parametrize('arguments', [
    ['account-create'], ['account-policy-set', '--account', 'LLM'],
    ['account-policy', '--account', 'LLM'], ['strategy-decision', '--account', 'LLM'],
])
def test_cli_new_commands_require_explicit_write_or_query_arguments(arguments):
    with pytest.raises(SystemExit) as result:
        main(arguments)
    assert result.value.code == 2


def test_cli_capabilities_show_implemented_strategies_and_scheduler():
    assert CAPABILITIES['dynamic_account_creation'] == 'IMPLEMENTED'
    assert CAPABILITIES['persistent_account_policies'] == 'IMPLEMENTED'
    assert CAPABILITIES['strategy_decision_audit'] == 'IMPLEMENTED'
    assert CAPABILITIES['s1_s1lt_execution'] == 'IMPLEMENTED'
    assert CAPABILITIES['llm_strategy'] == 'IMPLEMENTED'
    assert CAPABILITIES['background_scheduler'] == 'IMPLEMENTED'
