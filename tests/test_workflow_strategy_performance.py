"""WP03/04: isolated commit receipts and freeze-only static projections."""

import json
import os
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from execution_fixtures import FIRST, advance, execution_book
from test_strategy_acceptance import ControlledModel, response, strategy_world

from china_stock_sim.application import strategy
from china_stock_sim.common.wire import to_wire
from china_stock_sim.ledger import decision_evidence, strategy_decisions
from china_stock_sim.ledger.decision_evidence import verify_plan_decision
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest

assert execution_book and strategy_world


def proposed(book, app, account='S1'):
    result = app.plan(account, FIRST)
    assert result['state'] == 'PROPOSED', result
    row = book.get_strategy_decision(account, target_session=FIRST)
    validation = app.execution.validate(row['proposal'])
    assert validation['status'] == 'VALID', validation
    return row, to_wire(validation['plan_envelope'])


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_new_summary_uses_this_proposed_commit_without_full_read(strategy_world, monkeypatch, account):
    book, make_app, model = strategy_world
    app = make_app()
    save = book.save_strategy_decision
    to_wire = strategy.to_wire

    def used_wire(value):
        if isinstance(value, dict) and 'plan_envelope' in value:
            pytest.fail('summary serialized an unused validation result')
        return to_wire(value)

    monkeypatch.setattr(strategy, 'to_wire', used_wire)

    def committed(*args, **kwargs):
        result = save(*args, **kwargs)
        assert result['state'] == 'APPLIED' and result['decision_state'] == 'PROPOSED'
        monkeypatch.setattr(app, '_decision', lambda *a: pytest.fail('PROPOSED/full result reread'))
        return result

    monkeypatch.setattr(book, 'save_strategy_decision', committed)
    result = app.publish(account, FIRST, summary=True)
    assert result['state'] == 'FROZEN', result
    assert result['audit_scope'] == 'FROZEN_COMMIT_RECEIPT'
    assert book.get_strategy_decision(account, target_session=FIRST)['state'] == 'FROZEN'
    assert len(model.calls) == int(account == 'LLM')
    if account == 'LLM':
        assert [a['status'] for a in book.report_snapshot(account, FIRST)['model_attempts']] == ['SUCCEEDED']


def test_empty_summary_uses_committed_receipt(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    model = ControlledModel([response(intents=[])])
    app = make_app(client=model)
    save = book.save_strategy_decision

    def committed(*args, **kwargs):
        result = save(*args, **kwargs)
        monkeypatch.setattr(app, '_decision', lambda *a: pytest.fail('empty PROPOSED reread'))
        return result

    monkeypatch.setattr(book, 'save_strategy_decision', committed)
    assert app.publish('LLM', FIRST, summary=True)['state'] == 'FROZEN'
    plan = book.get_plan('LLM', target_session=FIRST)
    assert plan['status'] == 'EMPTY' and plan['orders'] == [] and len(model.calls) == 1


@pytest.mark.parametrize('change', [
    {'state': 'ALREADY_APPLIED'}, {'decision_id': 'another'}, {'sequence': 100},
    {'commit_id': None}, {'payload_hash': None}, {'operation_id': 'another'},
    {'account_revision': -1}, {'decision_state': None},
])
def test_incomplete_replay_or_mismatched_receipt_falls_back_to_authority(
        strategy_world, monkeypatch, change):
    book, make_app, _ = strategy_world
    app, saved, reads = make_app(), [], []
    save, read = book.save_strategy_decision, app._decision

    def committed(*args, **kwargs):
        result = save(*args, **kwargs)
        saved.append(True)
        return {**result, **change}

    def current(*args):
        if saved:
            reads.append(True)
        return read(*args)

    monkeypatch.setattr(book, 'save_strategy_decision', committed)
    monkeypatch.setattr(app, '_decision', current)
    assert app.publish('S1', FIRST, summary=True)['state'] == 'FROZEN'
    assert reads


@pytest.mark.parametrize('changed', ['cutoff', 'revision', 'definition'])
def test_actual_save_downgrade_is_not_a_proposed_receipt(strategy_world, monkeypatch, changed):
    book, make_app, model = strategy_world
    app = make_app()
    validate = app.execution.validate

    def change_after_validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        if changed == 'cutoff':
            advance(book, FIRST, '09:00:00')
        elif changed == 'definition':
            definition = strategy_decisions.get_definition
            monkeypatch.setattr(strategy_decisions, 'get_definition', lambda *a:
                {**definition(*a), 'implementation_hash': 'f' * 64})
        else:
            from test_strategy_application_rules import financial_revision

            financial_revision(book, 'S1')
        return result

    monkeypatch.setattr(app.execution, 'validate', change_after_validation)
    result = app.publish('S1', FIRST, summary=True)
    expected = {'cutoff': 'MISSED_CUTOFF', 'definition': 'CONFIG_CHANGED', 'revision': 'STALE_INPUT'}[changed]
    assert result['state'] == expected, result
    assert result['decision']['state'] == expected
    assert book.get_plan('S1', target_session=FIRST) is None and not model.calls


@pytest.mark.parametrize('account', ['S1', 'LLM'])
def test_crash_after_proposed_recovers_committed_original_without_recalculation(
        strategy_world, monkeypatch, account):
    book, make_app, model = strategy_world
    app = make_app()
    monkeypatch.setattr(app.execution, 'freeze', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('isolated crash after PROPOSED')))
    assert app.publish(account, FIRST, summary=True)['state'] == 'INVALID_INPUT'
    original = book.get_strategy_decision(account, target_session=FIRST)
    assert original['state'] == 'PROPOSED'
    recovered = make_app()
    monkeypatch.setattr(recovered, '_rules', lambda *a, **k: pytest.fail('reranked saved proposal'))
    monkeypatch.setattr(recovered, '_llm', lambda *a, **k: pytest.fail('recalled saved response'))
    assert recovered.recover(account, FIRST, summary=True)['state'] == 'FROZEN'
    frozen = book.get_strategy_decision(account, target_session=FIRST)
    assert frozen['proposal'] == original['proposal']
    assert len(model.calls) == int(account == 'LLM')


def test_freeze_projection_does_not_change_default_complete_core_contract(strategy_world):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    narrow = verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    assert 'core' not in narrow and 'source_proposal' not in narrow
    assert set(narrow['freeze_binding']) == {
        'schema_version', 'account_policy', 'parent_task', 'source_proposal_hash'}
    assert narrow['freeze_binding']['source_proposal_hash'] == digest(row['proposal'])
    for materialize in (False, True):
        full = verify_plan_decision(book.root, payload, market=book.market, materialize=materialize)
        assert {'input_bundle', 'calculation', 'proposal', 'account_policy'} <= full['core'].keys()
        assert full['source_proposal'] == row['proposal']


def test_warm_freeze_projection_avoids_core_and_index_expansion_keeps_authority_and_market(
        strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    first = verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    book._read('S1', lambda tx: strategy_decisions.verify_decision_record(
        book.root, tx.get('strategy_decisions', row['decision_id']), materialize=False))
    manifest, calls = book.market.evidence_manifest, []
    authority = book._verify_registered_decision_tx

    def public_manifest(*args, **kwargs):
        calls.append('market')
        return manifest(*args, **kwargs)

    def registered(*args):
        calls.append('authority')
        return authority(*args)

    monkeypatch.setattr(book.market, 'evidence_manifest', public_manifest)
    monkeypatch.setattr(book, '_verify_registered_decision_tx', registered)
    monkeypatch.setattr(decision_evidence, 'read_decision_core',
                        lambda *a: pytest.fail('warm core expansion'))
    monkeypatch.setattr(strategy_decisions, 'resolve_decision_index',
                        lambda *a: pytest.fail('warm proposal expansion'))
    second = verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    assert second['freeze_binding'] == first['freeze_binding']
    # Caller mutation must not change the cached authority input.
    second['freeze_binding']['account_policy'].clear()
    result = book.freeze_plan('S1', payload, operation_id='wp-static-hot',
                             expected_revision=row['account_revision'])
    assert result['state'] == 'APPLIED', result
    assert calls.count('authority') == 2 and calls.count('market') >= 3


@pytest.mark.parametrize('proof', ['missing', 'old_details', 'unknown_projection', 'disabled'])
def test_missing_or_unavailable_projection_uses_complete_validation(
        strategy_world, monkeypatch, proof):
    book, make_app, _ = strategy_world
    _, payload = proposed(book, make_app())
    verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    key = decision_evidence._validation_key(book.root, 'plan_decision', digest(payload))
    if proof == 'missing':
        decision_evidence._VALIDATIONS.pop(key)
    elif proof == 'old_details':
        decision_evidence._VALIDATIONS[key]['details'].pop('freeze_binding')
    elif proof == 'unknown_projection':
        decision_evidence._VALIDATIONS[key]['details']['freeze_binding']['schema_version'] = 99
    else:
        monkeypatch.setattr(decision_evidence, 'cached_validation', lambda *a: False)
        monkeypatch.setattr(decision_evidence, 'remember_validation', lambda *a, **k: None)
    real, reads = decision_evidence.document_closure, []

    def full(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(decision_evidence, 'document_closure', full)
    result = verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    assert result['freeze_binding']['schema_version'] == 1 and reads


@pytest.mark.parametrize('damage', ['payload', 'core_same_size_mtime', 'input_missing',
                                    'source', 'observation', 'decision_history', 'compact_index'])
def test_hot_freeze_rejects_changed_payload_and_full_dependency_closure(
        strategy_world, damage):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    checked = verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    if damage == 'payload':
        payload = deepcopy(payload)
        payload['validation_report']['source_proposal']['ordered_intents'][0]['reason'] = 'changed'
    elif damage == 'core_same_size_mtime':
        path = book.root / row['decision_core']['path']
        stamp, raw = path.stat(), bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 1
        path.write_bytes(raw)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    elif damage == 'input_missing':
        (book.root / row['input_document']['path']).unlink()
    elif damage == 'decision_history':
        (book.root / row['history'][0]['document']['path']).unlink()
    elif damage == 'compact_index':
        with sqlite3.connect(book.settings.database) as db:
            db.execute("UPDATE strategy_decisions SET data=json_set(data,'$.proposal.content_hash',?) WHERE id=?",
                       ('f' * 64, row['decision_id']))
    else:
        closure = checked['closure']
        if damage == 'observation':
            with sqlite3.connect(book.market.repository.path) as db:
                db.execute('DELETE FROM observations WHERE id=?', (next(iter(closure['observations'])),))
        else:
            manifest = book.market.evidence_manifest(closure['snapshots'],
                observation_ids=closure['observations'])
            source = next(item for item in manifest['files'] if item['kind'] == 'source_object')
            (book.root / source['path']).write_bytes(b'{}')
    result = book.freeze_plan('S1', payload, operation_id='wp-tampered',
                             expected_revision=row['account_revision'])
    assert result['state'] in {'INTEGRITY_ERROR', 'CONFLICT'}, result
    assert book.get_plan_header('S1', target_session=FIRST) is None


@pytest.mark.parametrize('changed', ['cutoff', 'core', 'revision', 'policy', 'cash', 'parent_progress'])
def test_transaction_rechecks_static_and_dynamic_guards_after_precheck(
        strategy_world, monkeypatch, changed):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    real, calls = book._verify_registered_decision_tx, []

    def registered(tx, frozen, decision):
        calls.append(1)
        if len(calls) == 2:
            if changed == 'revision':
                tx.state['account_revision'] += 1
            elif changed == 'policy':
                original = book._registered_policy_tx
                monkeypatch.setattr(book, '_registered_policy_tx', lambda *a:
                    {**original(*a), 'policy_hash': 'f' * 64})
            elif changed == 'cash':
                tx.cash('wp-consumed-cash', 'TEST', -tx.balance(), FIRST)
            elif changed == 'parent_progress':
                task = tx.get('rebalance_tasks', frozen['task_id'])
                task['completed_quantities'] = {f"{g['code']}:{g['side']}": g['quantity']
                                                for g in task['targets']}
                tx.save('rebalance_tasks', task['task_id'], task)
        checked = real(tx, frozen, decision)
        if len(calls) == 1:
            if changed == 'cutoff':
                advance(book, FIRST, '09:00:00')
            elif changed == 'core':
                (book.root / row['decision_core']['path']).write_bytes(b'{}')
        return checked

    monkeypatch.setattr(book, '_verify_registered_decision_tx', registered)
    result = book.freeze_plan('S1', payload, operation_id='wp-dynamic-change',
                             expected_revision=row['account_revision'])
    assert result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}, result
    assert book.get_plan_header('S1', target_session=FIRST) is None


def test_legacy_full_proposal_index_keeps_authority_binding(strategy_world):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    with sqlite3.connect(book.settings.database) as db:
        stored = json.loads(db.execute('SELECT data FROM strategy_decisions WHERE id=?',
                                      (row['decision_id'],)).fetchone()[0])
        stored['proposal'] = row['proposal']
        db.execute('UPDATE strategy_decisions SET data=? WHERE id=?',
                   (canonical(stored), row['decision_id']))
    result = book.freeze_plan('S1', payload, operation_id='wp-legacy-index',
                             expected_revision=row['account_revision'])
    assert result['state'] == 'APPLIED', result


def test_replay_still_checks_broken_static_original(strategy_world):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    arguments = {'operation_id': 'wp-idempotent-integrity', 'expected_revision': row['account_revision']}
    assert book.freeze_plan('S1', payload, **arguments)['state'] == 'APPLIED'
    assert book.freeze_plan('S1', payload, **arguments)['state'] == 'ALREADY_APPLIED'
    (book.root / row['decision_core']['path']).unlink()
    result = book.freeze_plan('S1', payload, **arguments)
    assert result['state'] == 'INTEGRITY_ERROR', result


def test_validator_change_misses_projection(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    _, payload = proposed(book, make_app())
    verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    monkeypatch.setattr(decision_evidence, 'verification_identity', lambda: 'b' * 64)
    monkeypatch.setattr(decision_evidence, 'document_closure', lambda *a, **k: (_ for _ in ()).throw(
        LedgerError('INTEGRITY_ERROR', 'cold path reached')))
    with pytest.raises(LedgerError, match='cold path reached'):
        verify_plan_decision(book.root, payload, market=book.market, projection='freeze')


def test_missing_public_market_verifier_rejects_hot_projection(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    _, payload = proposed(book, make_app())
    verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    monkeypatch.setattr(book.market, 'evidence_manifest', None)
    with pytest.raises(LedgerError) as caught:
        verify_plan_decision(book.root, payload, market=book.market, projection='freeze')
    assert caught.value.state == 'INTEGRITY_ERROR'


def test_disabled_proof_cache_does_not_prevent_fully_verified_freeze(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    row, payload = proposed(book, make_app())
    for module in (decision_evidence, strategy_decisions):
        monkeypatch.setattr(module, 'cached_validation', lambda *a: False)
        monkeypatch.setattr(module, 'remember_validation', lambda *a, **k: None)
    result = book.freeze_plan('S1', payload, operation_id='wp-no-cache',
                             expected_revision=row['account_revision'])
    assert result['state'] == 'APPLIED', result


@pytest.mark.parametrize('name', ['market/repository.py', 'market/raw_validation.py', 'market/storage.py',
                                 'ledger/trading.py', 'ledger/evidence.py'])
def test_verification_identity_binds_static_validation_dependencies(monkeypatch, name):
    original = decision_evidence.verification_identity()
    read = Path.read_bytes
    source = Path(decision_evidence.__file__).resolve().parents[1] / name
    visited = []

    def changed(path):
        value = read(path)
        if path == source:
            visited.append(True)
            return value + b'\n# isolated identity probe\n'
        return value

    monkeypatch.setattr(Path, 'read_bytes', changed)
    monkeypatch.setattr(decision_evidence, '_RULE_CACHE', None)
    assert decision_evidence.verification_identity() != original and visited
