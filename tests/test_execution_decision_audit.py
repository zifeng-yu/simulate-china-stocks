"""SX01: explicit strategy provenance through real isolated execution services.

Policy and decision setup now uses the current SL02 public registry.
These fixtures still do not implement a trading strategy. No real model/network is used.
"""

import argparse
import json
from copy import deepcopy

import pytest
from execution_fixtures import CODES, FIRST, PREVIOUS, advance, execution_book, proposal, source
from test_ledger_trading import call

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.common.policies import load_policy_bundle, policy_digest
from china_stock_sim.config import load_settings
from china_stock_sim.domain import ExecutionIntent, StrategyProposal
from china_stock_sim.execution import cli
from china_stock_sim.execution.evidence import collect_planning_market
from china_stock_sim.execution.utils import to_wire
from china_stock_sim.execution.validation import DefaultPlanValidator, normalize_proposal
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import canonical
from china_stock_sim.market.repository import MarketRepository

assert execution_book


def application(book):
    return ExecutionApplication(book.settings, market=book.market, ledger=book,
                                clock=lambda: book.test_now[0], test_mode=True)


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def publish(book, request, core):
    """Persist an immutable fixture core and return its explicit strategy reference."""
    request = deepcopy(request)
    request['decision_ref'] = {
        'decision_id': core['decision_id'],
        'account_policy_id': core['account_policy']['policy_id'],
        'account_policy_hash': policy_digest(core['account_policy']),
        'strategy_implementation_hash': core['strategy_implementation_hash'],
        'input_hash': policy_digest(core['input_bundle']),
        'decision_core': document(book.root, 'strategy', core),
    }
    return request


def strategy_request(book, *, empty=False, nested=False, input_extra=None):
    from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
    definition = deepcopy(STRATEGY_DEFINITIONS['LLM-Daily'])
    definition['strategy_version'] = 'isolated-current'
    definition['defaults'].update(lookback=121, max_symbols=5)
    book.test_monkeypatch.setitem(STRATEGY_DEFINITIONS, 'LLM-Daily', definition)
    scheduled = call(book, 'schedule_account_policy', {'effective_session': FIRST,
        'strategy_parameters': {'lookback': 121, 'max_symbols': 5}, 'reason': 'isolated fixture policy'})
    assert scheduled['state'] == 'APPLIED', scheduled
    policy = book.get_account_policy('LLM', FIRST)
    base = proposal(book, intents=[] if empty else None,
                    no_order_reason='explicit fixture HOLD' if empty else None)
    p = to_wire(normalize_proposal(base))
    p['provenance_mode'] = 'STRATEGY'
    cutoff = p['input_cutoff']
    kwargs = {'code': CODES[1], 'as_of': PREVIOUS, 'size': 121, 'knowledge_cutoff': cutoff}
    result = book.market.signal_prices(**kwargs).as_dict()
    assert result['state'] == 'READY', result
    # Evidence for a candidate that will not become an order must still survive.
    p['data_evidence'] = [{'method': 'signal_prices', 'kwargs': kwargs, 'result': result}]
    snapshot = book.market.freeze([CODES[1]], PREVIOUS, knowledge_cutoff=cutoff)
    p['market_snapshot_ids'] = [snapshot['snapshot_id']]
    core_proposal = deepcopy(p)
    core_proposal.pop('decision_ref', None)
    core = {
        'schema_version': 1,
        'decision_id': __import__('china_stock_sim.ledger.strategy_decisions',
                                  fromlist=['decision_identity']).decision_identity('LLM', FIRST),
        'account_policy': policy['policy'],
        'strategy_implementation_hash': policy['definition']['implementation_hash'],
        'input_bundle': {'market_snapshot_ids': deepcopy(p['market_snapshot_ids']),
                         'data_evidence': deepcopy(p['data_evidence']),
                         'account_state_id': p['account_snapshot_id'],
                         'account_revision': p['account_revision']},
        'proposal': core_proposal,
        'calculation': {'ranking': [{'code': CODES[1], 'score': '0.5',
                                    'excluded_reason': 'fixture ranking loser'}]},
    }
    if input_extra is not None:
        core['input_bundle']['extra_audit'] = deepcopy(input_extra)
    if nested:
        # This raw fact appears only inside the nested leaf, never in the plan queries.
        leaf_rows = [{'code': 'SH600999', 'date': PREVIOUS, 'name': 'excluded audit candidate'}]
        leaf_ids = MarketRepository(book.root).put('security', leaf_rows,
            source(leaf_rows, PREVIOUS + 'T18:10:00+08:00'))
        leaf = document(book.root, 'strategy-input', {'explanation': 'safe fixture model reply',
            'observation_ids': leaf_ids})
        middle = document(book.root, 'strategy-input', {'model_reply': leaf, 'repeat': leaf})
        core['calculation']['audit_attachment'] = middle
    prepare_registered(book, p, core)
    return publish(book, p, core), core


def prepare_registered(book, p, core):
    result = call(book, 'prepare_strategy_decision', {
        'target_session': p['target_session'], 'market_as_of': p['market_as_of'],
        'input_cutoff': p['input_cutoff'], 'input_bundle': core['input_bundle'],
        'expected_sequence': 0})
    assert result['state'] == 'APPLIED', result
    claim = call(book, 'claim_model_attempt', {'target_session': p['target_session'],
        'decision_id': result['decision_id'], 'input_hash': result['input_hash'],
        'request': {'prompt': 'isolated strategy fixture'}})
    assert claim['state'] == 'APPLIED', claim
    received = {'target_session': p['target_session'], 'attempt_id': claim['attempt_id'],
                'status': 'RECEIVED', 'response': {'text': 'isolated stored response'}}
    assert call(book, 'finish_model_attempt', received)['state'] == 'APPLIED'


def register_proposed(book, p):
    if not isinstance(p, dict) or p.get('provenance_mode') != 'STRATEGY':
        return
    core = read_business_document(book.root, p['decision_ref']['decision_core'])[0]
    row = book.get_strategy_decision(p['account_id'], target_session=p['target_session'])
    if row['state'] == 'PROPOSED':
        return
    result = call(book, 'save_strategy_decision', {
        'target_session': p['target_session'], 'decision_id': row['decision_id'],
        'expected_sequence': row['sequence'], 'state': 'PROPOSED',
        'proposal': p, 'decision_core': core})
    assert result['state'] == 'APPLIED' and result['decision_state'] == 'PROPOSED', result


def pure_inputs(book, p):
    p = normalize_proposal(p)
    return (book.account_state(p['account_id'], p['target_session']),
            collect_planning_market(book.market, p),
            load_policy_bundle(book.root / 'configs/execution_rules.toml',
                target_session=p['target_session'], knowledge_cutoff=p['input_cutoff'],
                execution_policy=book.settings.raw['execution'], fee_policy=book.settings.raw['fees']))


def pure_validate(book, p, core=None):
    account, evidence, bundle = pure_inputs(book, p)
    return DefaultPlanValidator().validate_plan(p, account, evidence, bundle,
                                               now=book._now(), decision_core=core)


def freeze(book, p):
    register_proposed(book, p)
    result = application(book).freeze(p, operation_id='sx01:freeze')
    assert result['state'] == 'APPLIED', result
    return result['plan']


def test_explicit_manual_mode_does_not_claim_strategy_provenance(execution_book):
    book = execution_book
    p = normalize_proposal(proposal(book))
    assert p['provenance_mode'] == 'MANUAL'
    assert p['decision_ref'] is None
    result = application(book).validate(p)
    assert result['status'] == 'VALID', result
    assert to_wire(result['plan_envelope'])['provenance_mode'] == 'MANUAL'
    assert to_wire(result['plan_envelope'])['decision_ref'] is None


@pytest.mark.parametrize('mutation', ['missing_ref', 'manual_ref', 'manual_evidence', 'unknown_mode',
                                     'empty_ref', 'extra_ref_field', 'bad_digest', 'path_escape',
                                     'wrong_core_filename'])
def test_strategy_reference_contract_rejects_invalid_or_downgraded_requests(execution_book, mutation):
    book = execution_book
    p, _ = strategy_request(book)
    if mutation == 'missing_ref':
        p['decision_ref'] = None
    elif mutation == 'manual_ref':
        p['provenance_mode'] = 'MANUAL'
    elif mutation == 'manual_evidence':
        p.update(provenance_mode='MANUAL', decision_ref=None)
    elif mutation == 'unknown_mode':
        p['provenance_mode'] = 'AUTO'
    elif mutation == 'empty_ref':
        p['decision_ref'] = {}
    elif mutation == 'extra_ref_field':
        p['decision_ref']['not_supported'] = 1
    elif mutation == 'bad_digest':
        p['decision_ref']['input_hash'] = 'A' * 64
    elif mutation == 'path_escape':
        p['decision_ref']['decision_core']['path'] = '../outside.json'
    else:
        p['decision_ref']['decision_core']['path'] = 'data/plans/strategy/' + '0' * 64 + '.json'
    before = tree(book.root)
    result = application(book).validate(p)
    assert result['status'] != 'VALID', result
    assert result['plan_envelope'] is None
    assert tree(book.root) == before
    assert book.get_plan('LLM', target_session=FIRST) is None


def test_pure_validator_requires_core_without_reading_files_or_losing_strategy_evidence(
    execution_book, monkeypatch,
):
    book = execution_book
    p, core = strategy_request(book)
    account, evidence, bundle = pure_inputs(book, p)
    before = tree(book.root)
    import pathlib
    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, 'read_text', lambda *a, **k: pytest.fail('pure validator did IO'))
        patch.setattr(pathlib.Path, 'read_bytes', lambda *a, **k: pytest.fail('pure validator did IO'))
        validator = DefaultPlanValidator()
        missing = validator.validate_plan(p, account, evidence, bundle, now=book._now())
        assert missing['status'] == 'WAITING_VALIDATION', missing
        assert missing['plan_envelope'] is None
        valid = validator.validate_plan(p, account, evidence, bundle,
                                        now=book._now(), decision_core=core)
    assert valid['status'] == 'VALID', valid
    envelope = to_wire(valid['plan_envelope'])
    assert envelope['decision_ref'] == p['decision_ref']
    assert envelope['strategy_market_snapshot_ids'] == p['market_snapshot_ids']
    assert envelope['strategy_data_evidence'] == p['data_evidence']
    assert any(q['method'] == 'signal_prices' and q['kwargs']['code'] == CODES[1]
               for q in envelope['strategy_data_evidence'])
    assert not any(q['method'] == 'signal_prices' for q in envelope['data_evidence'])
    assert envelope['validation_report']['source_proposal'] == to_wire(normalize_proposal(p))
    assert tree(book.root) == before
    envelope['strategy_data_evidence'][0]['kwargs']['size'] = 2
    assert p['data_evidence'][0]['kwargs']['size'] == 121


def test_strategy_dataclass_and_dictionary_normalize_to_same_identity(execution_book):
    book = execution_book
    p, core = strategy_request(book)
    value = deepcopy(p)
    value['ordered_intents'] = tuple(ExecutionIntent(**i) for i in value['ordered_intents'])
    value['data_evidence'] = tuple(value['data_evidence'])
    value['market_snapshot_ids'] = tuple(value['market_snapshot_ids'])
    dto = StrategyProposal(**value)
    assert policy_digest(normalize_proposal(dto)) == policy_digest(normalize_proposal(p))
    result = pure_validate(book, dto, core)
    assert result['status'] == 'VALID', result


@pytest.mark.parametrize('mutation', ['wrong_account', 'wrong_strategy', 'wrong_target',
                                     'future_policy', 'expired_policy', 'future_known_at',
                                     'changed_intent', 'changed_input', 'extra_core_field',
                                     'bad_schema', 'bad_calculation'])
def test_validly_hashed_but_inconsistent_core_cannot_freeze(execution_book, mutation):
    book = execution_book
    p, core = strategy_request(book)
    if mutation == 'wrong_account':
        core['account_policy']['account_id'] = 'S1'
    elif mutation == 'wrong_strategy':
        core['account_policy']['strategy_version'] = 'different'
    elif mutation == 'wrong_target':
        core['proposal']['target_session'] = '2026-11-02'
    elif mutation == 'future_policy':
        core['account_policy']['effective_from'] = '2026-11-02'
    elif mutation == 'expired_policy':
        core['account_policy']['effective_to'] = PREVIOUS
    elif mutation == 'future_known_at':
        core['account_policy']['known_at'] = FIRST + 'T08:00:00+08:00'
    elif mutation == 'changed_intent':
        core['proposal']['ordered_intents'][0]['quantity'] = 200
    elif mutation == 'changed_input':
        core['input_bundle']['data_evidence'] = []
    elif mutation == 'extra_core_field':
        core['validation_result'] = {}
    elif mutation == 'bad_schema':
        core['schema_version'] = True
    else:
        core['calculation'] = []
    p = publish(book, p, core)
    before = book.settings.database.read_bytes()
    result = application(book).freeze(p, operation_id='sx01:reject-core')
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before
    assert book.get_plan('LLM', target_session=FIRST) is None
    assert book.account_state('LLM', FIRST)['reserved_cash_fen'] == 0


@pytest.mark.parametrize('damage', ['missing', 'changed_body', 'duplicate_key', 'float', 'constant'])
def test_core_file_damage_is_rejected_before_any_booking(execution_book, damage):
    book = execution_book
    p, core = strategy_request(book)
    path = book.root / p['decision_ref']['decision_core']['path']
    if damage == 'missing':
        path.unlink()
    elif damage == 'changed_body':
        core['calculation']['ranking'][0]['score'] = '999'
        path.write_text(json.dumps(core))
    elif damage == 'duplicate_key':
        path.write_text('{"schema_version":1,' + canonical(core)[1:])
    elif damage == 'float':
        original = canonical(core)
        assert '"score":"0.5"' in original
        path.write_text(original.replace('"score":"0.5"', '"score":0.5'))
    else:
        original = canonical(core)
        assert '"score":"0.5"' in original
        path.write_text(original.replace('"score":"0.5"', '"score":NaN'))
    before = book.settings.database.read_bytes()
    result = application(book).freeze(p, operation_id='sx01:damaged')
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before


def test_ledger_rechecks_core_when_it_changes_after_pure_validation(execution_book):
    book = execution_book
    p, core = strategy_request(book)
    valid = application(book).validate(p)
    assert valid['status'] == 'VALID', valid
    (book.root / p['decision_ref']['decision_core']['path']).unlink()
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', valid['plan_envelope'])
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('mutation', ['price_forgery', 'future_cutoff'])
def test_strategy_query_evidence_is_rechecked_against_public_data(execution_book, mutation):
    book = execution_book
    p, core = strategy_request(book)
    query = p['data_evidence'][0]
    if mutation == 'price_forgery':
        query['result']['data'][0]['adjusted_close'] = '999.00'
    else:
        query['kwargs']['knowledge_cutoff'] = FIRST + 'T18:10:00+08:00'
    core['proposal']['data_evidence'] = deepcopy(p['data_evidence'])
    core['input_bundle']['data_evidence'] = deepcopy(p['data_evidence'])
    p = publish(book, p, core)
    before = book.settings.database.read_bytes()
    result = application(book).freeze(p, operation_id='sx01:false-query')
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('empty', [False, True])
def test_real_freeze_query_replay_and_settlement_keep_original_decision(execution_book, empty):
    book = execution_book
    p, core = strategy_request(book, empty=empty)
    plan = freeze(book, p)
    assert plan['provenance_mode'] == 'STRATEGY'
    assert plan['decision_ref'] == p['decision_ref']
    assert plan['strategy_data_evidence'] == p['data_evidence']
    assert plan['strategy_market_snapshot_ids'] == p['market_snapshot_ids']
    assert book.execution_view('LLM', plan['plan_id'])['frozen_plan']['decision_ref'] == p['decision_ref']
    assert book.report_snapshot('LLM', FIRST)['plans'][0]['decision_ref'] == p['decision_ref']
    before = tree(book.root)
    replay = application(book).freeze(p, operation_id='sx01:different-retry-id')
    assert replay['state'] == 'ALREADY_APPLIED', replay
    assert tree(book.root) == before
    book.settings.raw['strategies']['llm']['model_ref'] = 'unrelated-current-model'
    advance(book, FIRST)
    result = application(book).settle('LLM', plan['plan_id'], operation_id='sx01:settle')
    assert result['completion_status'] == 'COMPLETE', result
    assert book.fills('LLM')['total'] == (0 if empty else 1)
    before = book.settings.database.read_bytes()
    again = application(book).settle('LLM', plan['plan_id'], operation_id='sx01:settle-again')
    assert again['completion_status'] == 'COMPLETE', again
    assert book.settings.database.read_bytes() == before
    assert book.get_plan('LLM', plan['plan_id'])['decision_ref'] == p['decision_ref']
    assert read_business_document(book.root, p['decision_ref']['decision_core'])[0] == core


@pytest.mark.parametrize('damage', ['core', 'nested'])
def test_replay_refuses_missing_decision_originals_without_trading(execution_book, damage):
    book = execution_book
    p, core = strategy_request(book, nested=True)
    plan = freeze(book, p)
    ref = p['decision_ref']['decision_core'] if damage == 'core' else core['calculation']['audit_attachment']
    (book.root / ref['path']).unlink()
    before = book.settings.database.read_bytes()
    replay = application(book).freeze(p, operation_id='sx01:damaged-replay')
    assert replay['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, replay
    advance(book, FIRST)
    matched = application(book).match('LLM', plan['plan_id'])
    assert matched['completion_status'] == 'ERROR', matched
    assert book.settings.database.read_bytes() == before
    assert book.fills('LLM')['total'] == 0


def test_cli_validation_is_read_only_and_preserves_strategy_reference(execution_book, monkeypatch):
    book = execution_book
    p, _ = strategy_request(book)
    path = book.root / 'strategy-intent.json'
    path.write_text(json.dumps(p))
    parser = argparse.ArgumentParser()
    cli.register_commands(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(['execution-validate', '--input', str(path)])
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: application(book))
    import china_stock_sim.models.settings as model_settings
    monkeypatch.setattr(model_settings, 'load_model_settings', lambda *a, **k: pytest.fail('no model'))
    before = tree(book.root)
    result, code = cli.execute(args, book.root)
    assert code == 0 and result['status'] == 'VALID', result
    assert to_wire(result['plan_envelope'])['decision_ref'] == p['decision_ref']
    assert tree(book.root) == before


def test_backup_recursively_archives_decision_documents_and_restores_their_bytes(execution_book, tmp_path):
    book = execution_book
    p, core = strategy_request(book, nested=True)
    plan = freeze(book, p)
    middle = core['calculation']['audit_attachment']
    leaf = read_business_document(book.root, middle)[0]['model_reply']
    expected = {p['decision_ref']['decision_core']['path'], middle['path'], leaf['path'],
                plan['document']['path']}
    backup = book.backup()
    assert backup['state'] == 'COMPLETE', backup
    paths = [f['path'] for f in backup['evidence']['files']]
    assert expected <= set(paths)
    assert len(paths) == len(set(paths))
    leaf_ids = read_business_document(book.root, leaf)[0]['observation_ids']
    assert not set(leaf_ids).intersection(p['data_evidence'][0]['result']['observation_ids'])
    exported = book.market.evidence_manifest([], observation_ids=leaf_ids)
    assert {f['path'] for f in exported['files']} <= set(paths)
    target = tmp_path / 'restored-strategy-ledger'
    restored = book.restore_backup(backup['backup_id'], target)
    assert restored['state'] == 'RESTORED_AUDIT_PARTIAL', restored
    assert restored['market_archive_required'] is True
    for path in expected:
        assert (target / path).read_bytes() == (book.root / path).read_bytes()
    recovered = LedgerService(load_settings(target))
    assert recovered.get_plan('LLM', plan['plan_id'])['decision_ref'] == p['decision_ref']


def test_missing_nested_original_marks_audit_and_backup_incomplete(execution_book):
    book = execution_book
    p, core = strategy_request(book, nested=True)
    freeze(book, p)
    middle = core['calculation']['audit_attachment']
    leaf = read_business_document(book.root, middle)[0]['model_reply']
    (book.root / leaf['path']).unlink()
    audit = book.audit_status('LLM')
    assert audit['state'] != 'READY', audit
    backup = book.backup()
    assert backup['state'] == 'PARTIAL', backup
    assert any(issue.get('path') == leaf['path'] for issue in backup['evidence']['issues'])


@pytest.mark.parametrize('mutation', ['order_reason', 'missing_strategy_queries',
                                     'input_reference', 'report_proposal'])
def test_ledger_refuses_envelope_detached_from_validated_decision(execution_book, mutation):
    book = execution_book
    p, _ = strategy_request(book)
    valid = application(book).validate(p)
    assert valid['status'] == 'VALID', valid
    envelope = to_wire(valid['plan_envelope'])
    if mutation == 'order_reason':
        envelope['orders'][0]['reason'] = 'unvalidated replacement reason'
    elif mutation == 'missing_strategy_queries':
        envelope['strategy_data_evidence'] = []
    elif mutation == 'input_reference':
        envelope['decision_ref']['input_hash'] = '0' * 64
    else:
        envelope['validation_report']['source_proposal']['ordered_intents'][0]['reason'] = 'other'
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', envelope)
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('mode', ['quantity', 'budget'])
def test_coordinated_order_and_report_quantity_change_cannot_replace_core_intent(
    execution_book, mode,
):
    """A matching envelope/report is insufficient if sizing differs from the core."""
    from decimal import Decimal

    from china_stock_sim.ledger.evidence import fee_breakdown

    book = execution_book
    p, core = strategy_request(book)
    if mode == 'budget':
        p['ordered_intents'][0].update(quantity=None, budget_fen=1_000_510)
        core['proposal']['ordered_intents'] = deepcopy(p['ordered_intents'])
        p = publish(book, p, core)
    valid = application(book).validate(p)
    assert valid['status'] == 'VALID', valid
    envelope = to_wire(valid['plan_envelope'])
    assert envelope['orders'][0]['quantity'] == 1000
    expense = fee_breakdown(Decimal('10.00'), 200, 'BUY', book.settings.raw['fees'], FIRST)
    for order in [envelope['orders'][0], envelope['validation_report']['accepted_orders'][0]]:
        order['quantity'] = 200
        order['reserved_cash_fen'] = -expense['net_cash_fen']
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', envelope)
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before
    assert book.account_state('LLM', FIRST)['reserved_cash_fen'] == 0


def test_manual_downgrade_cannot_hide_retained_strategy_source_proposal(execution_book):
    book = execution_book
    p, _ = strategy_request(book)
    valid = application(book).validate(p)
    assert valid['status'] == 'VALID', valid
    envelope = to_wire(valid['plan_envelope'])
    for value in [envelope, envelope['validation_report']]:
        value.update(provenance_mode='MANUAL', decision_ref=None,
                     strategy_market_snapshot_ids=[], strategy_data_evidence=[])
    assert envelope['validation_report']['source_proposal']['provenance_mode'] == 'STRATEGY'
    assert envelope['validation_report']['source_proposal']['decision_ref'] == p['decision_ref']
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', envelope)
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before


def test_conflicting_nested_document_hashes_cannot_freeze(execution_book):
    book = execution_book
    p, core = strategy_request(book, nested=True)
    reference = deepcopy(core['calculation']['audit_attachment'])
    reference['content_hash'] = '0' * 64
    core['calculation']['conflicting_attachment'] = reference
    p = publish(book, p, core)
    before = book.settings.database.read_bytes()
    result = application(book).freeze(p, operation_id='sx01:conflicting-child-hash')
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before
    assert book.get_plan('LLM', target_session=FIRST) is None


@pytest.mark.parametrize('available,fraction,expected', [
    (1150, '1', 1150), (1150, '0.5', 550), (1150, '0.1', 100),
    (1150, '0.01', 0), (50, '1', 50), (50, '0.5', 0),
])
def test_shared_sell_fraction_sizing_keeps_odd_lot_boundary(settings, available, fraction, expected):
    from decimal import Decimal

    from china_stock_sim.common.sizing import intent_quantity

    quantity, evidence = intent_quantity(
        {'side': 'SELL', 'code': CODES[0], 'price': Decimal('10.00'),
         'quantity': None, 'budget_fen': None, 'sell_fraction': fraction},
        [{'code': CODES[0], 'sellable_shares': available}], settings.raw['fees'], FIRST,
    )
    assert quantity == expected
    assert evidence['quantity'] == expected
    assert evidence['method'] == 'sell_fraction'
    assert evidence['sellable_shares'] == available
    assert quantity <= available


def test_direct_execution_commit_rechecks_core_after_matching(execution_book):
    book = execution_book
    p, _ = strategy_request(book)
    plan = freeze(book, p)
    advance(book, FIRST, '18:10:00')
    calculated = application(book).match('LLM', plan['plan_id'])
    assert calculated['results'][0]['state'] == 'SIMULATED_FILL', calculated
    (book.root / p['decision_ref']['decision_core']['path']).unlink()
    before = book.settings.database.read_bytes()
    result = call(book, 'apply_execution_results', {'results': calculated['results']})
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before
    assert book.fills('LLM')['total'] == 0
    assert book.positions('LLM', FIRST) == []
    state = book.account_state('LLM', FIRST)
    assert state['cash_fen'] == 5_000_000
    assert state['reserved_cash_fen'] == plan['buy_budget_fen']


@pytest.mark.parametrize('bad_source', ['not-a-proposal', ['not', 'a', 'proposal']])
def test_malformed_report_source_proposal_is_a_controlled_error(execution_book, bad_source):
    book = execution_book
    p, _ = strategy_request(book)
    valid = application(book).validate(p)
    assert valid['status'] == 'VALID', valid
    envelope = to_wire(valid['plan_envelope'])
    envelope['validation_report']['source_proposal'] = bad_source
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', envelope)
    assert result['state'] in {'INVALID_INPUT', 'INTEGRITY_ERROR'}, result
    assert book.settings.database.read_bytes() == before


def test_erasing_orders_and_accepted_report_cannot_turn_buy_core_into_hold(execution_book):
    book = execution_book
    p, _ = strategy_request(book)
    valid = application(book).validate(p)
    assert valid['status'] == 'VALID', valid
    envelope = to_wire(valid['plan_envelope'])
    assert envelope['orders'] and envelope['validation_report']['accepted_orders']
    envelope['orders'] = []
    envelope['validation_report']['accepted_orders'] = []
    envelope['no_order_reason'] = 'unvalidated HOLD substituted for BUY'
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', envelope)
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.settings.database.read_bytes() == before
    assert book.get_plan('LLM', target_session=FIRST) is None
