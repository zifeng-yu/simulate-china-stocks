"""SP: bounded workflow receipts preserve immutable evidence and execution guards."""

import hashlib
import json
import multiprocessing
import os
import sqlite3

import pytest
from execution_fixtures import CODES, FIRST, PREVIOUS, advance, execution_book, proposal
from test_strategy_acceptance import ControlledModel, response, strategy_world

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.ledger import decision_evidence
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest
from china_stock_sim.runtime.recording import run_observed

assert execution_book and strategy_world


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def clear_memory():
    decision_evidence._VALIDATIONS.clear()
    decision_evidence._DOCUMENTS.clear()
    decision_evidence._DOCUMENT_BYTES = 0
    decision_evidence._DOCUMENT_STAMPS.clear()


def warm(book, app, account='S1'):
    with decision_evidence.verification_scope(book.root, persist=True):
        first = app.publish(account, FIRST, summary=True)
        assert first['state'] == 'FROZEN', first
        checked = app.recover(account, FIRST, summary=True)
        assert checked['state'] == 'FROZEN', checked
        assert checked['audit_scope'] == 'FROZEN_RECEIPT'
    return first


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_summary_freeze_avoids_post_commit_plan_and_decision_materialization(
        strategy_world, monkeypatch, account):
    book, make_app, model = strategy_world
    app = make_app()
    original = book.freeze_plan

    def freeze(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result['state'] == 'APPLIED'
        monkeypatch.setattr(book, 'get_plan', lambda *a, **k: pytest.fail('post-commit plan expansion'))
        monkeypatch.setattr(app, '_decision', lambda *a, **k: pytest.fail('post-commit decision expansion'))
        return result

    monkeypatch.setattr(book, 'freeze_plan', freeze)
    result = app.publish(account, FIRST, summary=True)
    assert result['state'] == 'FROZEN', result
    assert result['audit_scope'] == 'FROZEN_COMMIT_RECEIPT'
    assert {'plan', 'decision', 'publication'}.isdisjoint(result)
    assert len(model.calls) == int(account == 'LLM')


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_warm_receipt_no_business_expansion_no_financial_write_keeps_market_verification(
        strategy_world, monkeypatch, account):
    book, make_app, model = strategy_world
    app = make_app()
    original = warm(book, app, account)
    clear_memory()
    market_calls = []
    real = book.market.evidence_manifest

    def market(*args, **kwargs):
        market_calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(book.market, 'evidence_manifest', market)
    monkeypatch.setattr(book, 'get_plan', lambda *a, **k: pytest.fail('warm full plan read'))
    monkeypatch.setattr(app.execution, '_view', lambda *a: pytest.fail('warm execution view'))
    monkeypatch.setattr(app, '_decision', lambda *a: pytest.fail('warm decision expansion'))
    before = files(book.root)
    with decision_evidence.verification_scope(book.root, persist=True) as metrics:
        result = app.recover(account, FIRST, summary=True)
    assert result['state'] == 'FROZEN' and result['plan_id'] == original['plan_id']
    assert result['audit_scope'] == 'FROZEN_RECEIPT' and market_calls == [1]
    assert metrics['receipts_written'] == 0 and metrics['bytes_read'] < 50_000
    assert files(book.root) == before
    assert len(model.calls) == int(account == 'LLM')


def test_read_only_receipt_does_not_persist_missing_proofs_even_inside_writer_scope(strategy_world):
    book, make_app, _ = strategy_world
    assert make_app().publish('S1', FIRST, summary=True)['state'] == 'FROZEN'
    clear_memory()
    before = files(book.root)
    with decision_evidence.verification_scope(book.root, persist=True) as metrics:
        result = book.frozen_plan_receipt('S1', target_session=FIRST)
    assert result['state'] == 'UNAVAILABLE'
    assert metrics['receipts_written'] == 0 and files(book.root) == before


@pytest.mark.parametrize('damage', ['plan_missing', 'plan_cas', 'header_rehash',
    'decision_missing', 'decision_rebind', 'decision_history', 'order_missing', 'order_quantity',
    'source', 'market_snapshot', 'observation_binding'])
def test_frozen_receipt_fails_closed_for_damaged_originals_indexes_and_market(
        strategy_world, monkeypatch, damage):
    book, make_app, model = strategy_world
    app = make_app()
    if damage == 'market_snapshot':
        snapshot = book.market.freeze([CODES[0]], PREVIOUS,
            knowledge_cutoff=book._now().isoformat(), purposes=('rules',))
        assemble = app._assemble

        def with_snapshot(*args):
            value = assemble(*args)
            value['input_bundle']['market_snapshot_ids'] = [snapshot['snapshot_id']]
            return value

        monkeypatch.setattr(app, '_assemble', with_snapshot)
    first = warm(book, app)
    plan = book.get_plan('S1', target_session=FIRST)
    decision = book.get_strategy_decision('S1', target_session=FIRST)
    if damage in {'plan_missing', 'plan_cas'}:
        path = book.root / plan['document']['path']
        if damage == 'plan_missing':
            path.unlink()
        else:
            body = json.loads(path.read_bytes())
            if body.get('storage_format') == 'cas-json-v1':
                path = book.root / body['object_ref']['_evidence_ref']['path']
            data = bytearray(path.read_bytes())
            data[len(data) // 2] ^= 1
            path.chmod(0o600)
            path.write_bytes(data)
    elif damage == 'decision_history':
        (book.root / decision['history'][0]['document']['path']).unlink()
    elif damage in {'source', 'market_snapshot', 'observation_binding'}:
        proof = decision_evidence.validation_details(book.root, 'plan_decision', digest(plan))
        if damage == 'observation_binding':
            with sqlite3.connect(book.market.repository.path) as db:
                # The public manifest must reject a missing observation even if all source files remain.
                db.execute('DELETE FROM observations WHERE id=?', (proof['observations'][0],))
        else:
            manifest = book.market.evidence_manifest(proof['snapshots'], observation_ids=proof['observations'])
            kind = 'source_object' if damage == 'source' else 'market_snapshot'
            candidates = [r for r in manifest['files'] if r['kind'] == kind]
            assert candidates
            (book.root / candidates[0]['path']).write_bytes(b'{}')
    else:
        with sqlite3.connect(book.settings.database) as db:
            if damage == 'header_rehash':
                raw = json.loads(db.execute('SELECT data FROM plans WHERE id=?', (first['plan_id'],)).fetchone()[0])
                raw['orders'][0]['quantity'] += 100
                raw['header_hash'] = digest({k: v for k, v in raw.items()
                    if k not in {'id', 'original_record_ref', 'header_hash'}})
                db.execute('UPDATE plans SET data=? WHERE id=?', (canonical(raw), first['plan_id']))
            elif damage == 'decision_missing':
                db.execute('DELETE FROM strategy_decisions WHERE account_id=?', ('S1',))
            elif damage == 'decision_rebind':
                db.execute("UPDATE strategy_decisions SET data=json_set(data,'$.plan_id','wrong') WHERE account_id='S1'")
            elif damage == 'order_missing':
                db.execute('DELETE FROM orders WHERE id=?', (plan['order_ids'][0],))
            else:
                db.execute("UPDATE orders SET data=json_set(data,'$.quantity',999) WHERE id=?", (plan['order_ids'][0],))
    before = book.settings.database.read_bytes()
    result = app.recover('S1', FIRST, summary=True)
    assert result['state'] == 'INTEGRITY_ERROR', result
    assert book.settings.database.read_bytes() == before and not model.calls


def test_empty_and_post_cutoff_frozen_receipts_do_not_replan(strategy_world):
    book, make_app, _ = strategy_world
    model = ControlledModel([response(intents=[])])
    app = make_app(client=model)
    warm(book, app, 'LLM')
    advance(book, FIRST, '09:00:00')
    before = book.settings.database.read_bytes()
    result = app.recover('LLM', FIRST, summary=True)
    assert result['state'] == 'FROZEN' and result['order_count'] == 0
    assert len(model.calls) == 1 and book.settings.database.read_bytes() == before


def test_completed_plan_receipt_does_not_replace_execution_guards(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    plan = warm(book, app)
    advance(book, FIRST)
    execution = app.execution
    calls = []
    real = execution._view

    def view(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(execution, '_view', view)
    settled = execution.settle('S1', plan['plan_id'], operation_id='receipt-does-not-authorize-execution')
    assert settled['completion_status'] == 'COMPLETE' and calls
    assert app.recover('S1', FIRST, summary=True)['state'] == 'FROZEN'


def test_detailed_default_and_legacy_manual_summary_are_compatible(strategy_world, execution_book):
    book, make_app, _ = strategy_world
    detailed = make_app().publish('S1', FIRST)
    assert detailed['state'] == 'FROZEN' and {'decision', 'publication'} <= detailed.keys()
    replay = make_app().recover('S1', FIRST)
    assert {'plan', 'decision'} <= replay.keys()
    execution = ExecutionApplication(book.settings, ledger=book, market=book.market,
        clock=book._now, test_mode=True)
    manual = execution.freeze(proposal(book), operation_id='manual-receipt')
    assert manual['state'] == 'APPLIED'
    assert book.frozen_plan_receipt('LLM', target_session=FIRST)['reason'] == 'MANUAL_PLAN'
    assert make_app().recover('LLM', FIRST, summary=True)['audit_scope'] == 'FROZEN_FULL_VERIFIED'
    with sqlite3.connect(book.settings.database) as db:
        db.execute('UPDATE plans SET data=? WHERE id=?', (canonical(replay['plan']), replay['plan_id']))
    assert book.frozen_plan_receipt('S1', target_session=FIRST)['reason'] == 'LEGACY_FULL_PLAN'
    assert make_app().recover('S1', FIRST, summary=True)['state'] == 'FROZEN'


def _child_receipt(book, output):
    clear_memory()
    book.get_plan = lambda *a, **k: (_ for _ in ()).throw(AssertionError('expanded original'))
    output.put(book.frozen_plan_receipt('S1', target_session=FIRST))


def test_frozen_receipt_persistent_proof_survives_process_restart(strategy_world):
    book, make_app, _ = strategy_world
    warm(book, make_app())
    context = multiprocessing.get_context('fork')
    output = context.Queue()
    process = context.Process(target=_child_receipt, args=(book, output))
    process.start()
    process.join(20)
    assert process.exitcode == 0
    assert output.get(timeout=2)['state'] == 'READY'


def test_cold_fallback_on_validator_change_and_no_current_strategy_reinterpretation(
        strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    warm(book, app)
    monkeypatch.setattr(decision_evidence, 'verification_identity', lambda: 'f' * 64)
    before = book.settings.database.read_bytes()
    assert book.frozen_plan_receipt('S1', target_session=FIRST)['state'] == 'UNAVAILABLE'
    monkeypatch.setattr('china_stock_sim.application.strategy.get_definition',
                        lambda *a: pytest.fail('frozen plan consulted current strategy'))
    assert app.recover('S1', FIRST, summary=True)['state'] == 'FROZEN'
    assert book.settings.database.read_bytes() == before


def test_workflow_requests_summary_and_nested_observation_preserves_result(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    book.settings.raw['strategies']['s1']['enabled'] = True
    monkeypatch.setattr('china_stock_sim.runtime.recording._identity',
                        lambda root: {'state': 'UNAVAILABLE'})
    workflow = WorkflowApplication(book.settings, ledger=book, market=book.market,
        strategy=app, clock=lambda: book.test_now[0], test_mode=True)
    result, code = run_observed(book.root, 'workflow', lambda: (
        workflow._plan('S1', {'state': 'READY', 'planning_target': FIRST, 'plan_allowed': True}), 0))
    assert code == 0 and result['state'] == 'FROZEN'
    assert result['audit_scope'] == 'FROZEN_COMMIT_RECEIPT'
    events = [json.loads(line) for path in (book.root / 'logs/runtime').rglob('*.jsonl')
              for line in path.read_text().splitlines()]
    stages = {row.get('stage') for row in events if row['event'] == 'STAGE_END'}
    assert {'strategy.assemble', 'strategy.prepare_write', 'strategy.rules',
            'strategy.freeze', 'execution.freeze_commit', 'execution.plan_validate'} <= stages
    assert 'strategy.result_read' not in stages and 'execution.freeze_result' not in stages


def test_absent_receipt_is_scoped_to_account_and_target(strategy_world):
    book, make_app, _ = strategy_world
    warm(book, make_app())
    assert book.frozen_plan_receipt('LLM', target_session=FIRST)['state'] == 'ABSENT'
    assert book.frozen_plan_receipt('S1', target_session='2026-11-04')['state'] == 'ABSENT'
    with pytest.raises(LedgerError):
        book.frozen_plan_receipt('UNKNOWN', target_session=FIRST)


def test_full_verification_succeeds_when_derived_proofs_cannot_be_retained(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    assert app.publish('S1', FIRST, summary=True)['state'] == 'FROZEN'
    clear_memory()
    monkeypatch.setattr(decision_evidence, 'cached_validation', lambda *a, **k: False)
    before = book.settings.database.read_bytes()
    result = app.recover('S1', FIRST, summary=True)
    assert result['state'] == 'FROZEN' and result['audit_scope'] == 'FROZEN_FULL_VERIFIED'
    assert book.settings.database.read_bytes() == before


def test_unknown_dependency_timestamp_change_cold_verifies_identical_bytes(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    warm(book, app)
    plan = book.get_plan('S1', target_session=FIRST)
    os.utime(book.root / plan['document']['path'], None)
    assert book.frozen_plan_receipt('S1', target_session=FIRST)['state'] == 'UNAVAILABLE'
    called, real = [], app.execution._view

    def view(*args):
        called.append(1)
        return real(*args)

    monkeypatch.setattr(app.execution, '_view', view)
    before = book.settings.database.read_bytes()
    assert app.recover('S1', FIRST, summary=True)['state'] == 'FROZEN' and called == [1]
    assert book.settings.database.read_bytes() == before


def test_controlled_backup_links_rebind_receipt_proofs(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    warm(book, app)
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    assert backup['storage']['hardlinked_files'] > 0
    clear_memory()
    monkeypatch.setattr(app.execution, '_view', lambda *a: pytest.fail('controlled link invalidated proof'))
    before = files(book.root)
    assert app.recover('S1', FIRST, summary=True)['state'] == 'FROZEN'
    assert files(book.root) == before


@pytest.mark.parametrize('time', ['09:00:00', '12:00:00'])
def test_workflow_after_cutoff_existing_plan_uses_receipt_without_full_decision(
        strategy_world, monkeypatch, time):
    book, make_app, model = strategy_world
    app = make_app()
    warm(book, app)
    advance(book, FIRST, time)
    workflow = WorkflowApplication(book.settings, ledger=book, market=book.market,
        strategy=app, clock=lambda: book.test_now[0], test_mode=True)
    monkeypatch.setattr(book, 'get_strategy_decision', lambda *a, **k: pytest.fail('cutoff full decision'))
    monkeypatch.setattr(app.execution, '_view', lambda *a: pytest.fail('cutoff execution view'))
    before = book.settings.database.read_bytes()
    result = workflow._plan('S1', {'state': 'READY', 'planning_target': FIRST, 'plan_allowed': False})
    assert result['state'] == 'FROZEN' and result['audit_scope'] == 'FROZEN_RECEIPT'
    assert book.settings.database.read_bytes() == before and not model.calls


def test_workflow_after_cutoff_broken_frozen_plan_never_replans(strategy_world, monkeypatch):
    book, make_app, model = strategy_world
    app = make_app()
    warm(book, app)
    plan = book.get_plan('S1', target_session=FIRST)
    (book.root / plan['document']['path']).unlink()
    advance(book, FIRST, '09:00:00')
    monkeypatch.setattr(app, '_prepare', lambda *a: pytest.fail('damaged plan was replanned'))
    workflow = WorkflowApplication(book.settings, ledger=book, market=book.market,
        strategy=app, clock=lambda: book.test_now[0], test_mode=True)
    before = book.settings.database.read_bytes()
    result = workflow._plan('S1', {'state': 'READY', 'planning_target': FIRST, 'plan_allowed': False})
    assert result['state'] == 'INTEGRITY_ERROR'
    assert book.settings.database.read_bytes() == before and not model.calls


def test_large_valid_legacy_decision_index_falls_back_without_replanning(strategy_world, monkeypatch):
    """A genuinely verified >1MiB expanded proposal uses the compatible full path."""
    book, make_app, model = strategy_world
    app = make_app()
    assemble = app._assemble

    def retained_repeated_evidence(*args, **kwargs):
        result = assemble(*args, **kwargs)
        bundle = result['input_bundle']
        # Repeated valid query evidence is legal and leaves the two securities'
        # economics unchanged. It models a verbose legacy index, not fake padding.
        query = next(row for row in bundle['data_evidence'] if row['method'] == 'prices')
        copies = 1_100_000 // len(canonical(query).encode()) + 1
        bundle['data_evidence'].extend([query] * copies)
        result['input_hash'] = digest(bundle)
        return result

    monkeypatch.setattr(app, '_assemble', retained_repeated_evidence)
    first = app.publish('S1', FIRST, summary=True)
    assert first['state'] == 'FROZEN', first
    decision = book.get_strategy_decision('S1', target_session=FIRST)
    expanded = canonical(decision)
    assert len(expanded.encode()) > 1024 * 1024
    with sqlite3.connect(book.settings.database) as db:
        db.execute('UPDATE strategy_decisions SET data=? WHERE account_id=? AND id=?',
                   (expanded, 'S1', decision['decision_id']))
    clear_memory()
    # Establish that the expanded index is valid before exercising its size fallback.
    verified = book.get_strategy_decision('S1', target_session=FIRST)
    assert verified == decision
    assert book.frozen_plan_receipt('S1', target_session=FIRST) == {
        'state': 'UNAVAILABLE', 'reason': 'LEGACY_LARGE_INDEX'}
    viewed, view = [], app.execution._view

    def full_view(*args):
        viewed.append(args)
        return view(*args)

    monkeypatch.setattr(app.execution, '_view', full_view)
    monkeypatch.setattr(app, '_prepare', lambda *a: pytest.fail('large valid index was replanned'))
    before = book.settings.database.read_bytes()
    result = app.recover('S1', FIRST, summary=True)
    assert result['state'] == 'FROZEN' and result['audit_scope'] == 'FROZEN_FULL_VERIFIED'
    assert result['plan_id'] == first['plan_id'] and viewed == [('S1', first['plan_id'])]
    assert book.settings.database.read_bytes() == before and not model.calls


@pytest.mark.parametrize('table', ['plans', 'strategy_decisions'])
def test_large_damaged_index_fallback_fails_closed_without_replanning(strategy_world, monkeypatch, table):
    book, make_app, model = strategy_world
    app = make_app()
    first = warm(book, app)
    with sqlite3.connect(book.settings.database) as db:
        identity, encoded = db.execute(f'SELECT id,data FROM {table} WHERE account_id=?',
                                       ('S1',)).fetchone()
        value = json.loads(encoded)
        if table == 'plans':
            value['no_order_reason'] = 'x' * (1024 * 1024)
            value['header_hash'] = digest({k: v for k, v in value.items()
                if k not in {'id', 'original_record_ref', 'header_hash'}})
        else:
            value['reasons'] = [{'kind': 'x' * (1024 * 1024)}]
        encoded = canonical(value)
        assert len(encoded.encode()) > 1024 * 1024
        db.execute(f'UPDATE {table} SET data=? WHERE account_id=? AND id=?',
                   (encoded, 'S1', identity))
    clear_memory()
    assert book.frozen_plan_receipt('S1', target_session=FIRST) == {
        'state': 'UNAVAILABLE', 'reason': 'LEGACY_LARGE_INDEX'}
    monkeypatch.setattr(app, '_prepare', lambda *a: pytest.fail('large damaged index was replanned'))
    before = book.settings.database.read_bytes()
    result = app.recover('S1', FIRST, summary=True)
    assert result['state'] == 'INTEGRITY_ERROR', (first['plan_id'], result)
    assert book.settings.database.read_bytes() == before and not model.calls
