import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from china_stock_sim.common.evidence_objects import put_blob
from china_stock_sim.ledger.storage_promotion import _digest
from china_stock_sim.scheduler.workspace import writer_lock

_SPEC = importlib.util.spec_from_file_location('storage_finalize_tested',
    Path(__file__).resolve().parents[1] / 'scripts/storage_finalize.py')
finalize = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(finalize)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def _fixture(root):
    stage = 'data/maintenance/test-staging'
    promotion_dir = 'data/maintenance/promotion-' + 'a' * 32
    raw = b'SQLite format 3\x00' + b'original immutable baseline\x00' * 3000
    rollback = root / promotion_dir / 'rollback.sqlite3'
    rollback.parent.mkdir(parents=True)
    rollback.write_bytes(raw)
    staging = root / stage / 'data/state/ledger.sqlite3'
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b'changed staging database with freed pages' * 1000)
    compact = staging.with_name('ledger.compact.sqlite3')
    compact.write_bytes(b'preserved compact baseline')
    production = root / 'data/state/ledger.sqlite3'
    production.parent.mkdir(parents=True)
    production.write_bytes(b'new production after additional business')
    promotion = {'source_sha256': _hash(rollback), 'source_bytes': len(raw),
        'rollback_path': str(rollback.relative_to(root)), 'staging_root': stage,
        'compact_path': str(compact.relative_to(root)), 'promotion_id': 'a' * 32,
        'state_path': promotion_dir + '/promotion.json'}
    promotion['plan_sha256'] = _digest(promotion)
    _write(root, 'logs/promotion.json', promotion)
    _write(root, promotion['state_path'], {'phase': 'VERIFIED',
        'plan_sha256': promotion['plan_sha256'], 'verified_at': '2026-11-03T12:00:00+08:00'})
    original_doc = b'{"original":"frozen document"}\n'
    ref = put_blob(root, original_doc)
    doc_path = 'data/plans/frozen/known.json'
    wrapper = _write(root, doc_path, {'storage': 'compressed', 'reference': ref})
    staging_document = root / stage / doc_path
    staging_document.parent.mkdir(parents=True)
    staging_document.write_bytes(original_doc)
    unknown = staging_document.with_name('unknown.json')
    unknown.write_bytes(b'unknown must remain')
    _write(root, 'logs/documents.json', {'state': 'COMPLETE', 'results': [{
        'document': {'path': doc_path}, 'object_ref': ref,
        'original_sha256': hashlib.sha256(original_doc).hexdigest(), 'wrapper_sha256': _hash(wrapper)}]})
    plan = finalize.prepare(root, promotion_plan='logs/promotion.json', document_receipt='logs/documents.json')
    review = {'state': 'RULE_COMPLIANT_RECOVERY_COMPLETE', 'business_recovery_reviewed': True,
        'production_verified': True, 'no_program_errors': True, 'original_fills_unchanged': True,
        'new_fills_traceable': True, 'promotion_plan_sha256': promotion['plan_sha256'],
        'run_id': 'b' * 32, 'finished_at': '2026-11-03T13:00:00+08:00',
        'production_database_sha256': _hash(production), 'workflow_state': 'PARTIAL',
        'unresolved_data': [{'code': 'UNDATED_EVENT', 'account_id': 'S1'}],
        'rule_outcomes': [{'code': 'MISSED_CUTOFF', 'session': '2026-11-03'}]}
    _write(root, 'logs/business.json', review)
    return plan, raw, production, compact, unknown, ref, review


def test_prepare_and_archive_do_not_delete_then_restore_exact_baseline(tmp_path):
    plan, raw, production, compact, unknown, _, _ = _fixture(tmp_path)
    assert plan['read_only'] and len(plan['candidates']) == 3
    assert not (tmp_path / plan['state_path']).exists()
    before = production.read_bytes()
    state = finalize.archive(tmp_path, plan, maintenance_confirmed=True)
    assert state['phase'] == 'ARCHIVED'
    assert all((tmp_path / r['path']).exists() for r in plan['candidates'])
    destination = 'data/maintenance/recovery-test/ledger.sqlite3'
    result = finalize.restore(tmp_path, state, destination=destination)
    assert result['state'] == 'RESTORED_ISOLATED'
    assert (tmp_path / destination).read_bytes() == raw
    assert production.read_bytes() == before and compact.exists() and unknown.exists()


def test_reviewed_partial_can_finalize_exact_allowlist_and_repeat(tmp_path):
    plan, raw, production, compact, unknown, _, _ = _fixture(tmp_path)
    before = production.read_bytes()
    result = finalize.apply(tmp_path, plan, business_receipt='logs/business.json', maintenance_confirmed=True)
    assert result['phase'] == 'COMPLETE' and len(result['deleted']) == 3
    assert not any((tmp_path / r['path']).exists() for r in plan['candidates'])
    assert production.read_bytes() == before and compact.exists() and unknown.exists()
    assert result['exclusive_reclaimed_bytes'] is None and not result['production_modified']
    assert finalize.apply(tmp_path, plan, business_receipt='logs/business.json',
                          maintenance_confirmed=True)['phase'] == 'COMPLETE'
    finalize.restore(tmp_path, result, destination='data/maintenance/recovery-final/ledger.sqlite3')
    assert (tmp_path / 'data/maintenance/recovery-final/ledger.sqlite3').read_bytes() == raw


@pytest.mark.parametrize('change', [
    {'state': 'COMPLETE'}, {'business_recovery_reviewed': False}, {'no_program_errors': False},
    {'original_fills_unchanged': False}, {'new_fills_traceable': False}, {'workflow_state': 'FAILED'},
    {'unresolved_data': [{'code': 'PROGRAM_ERROR'}]}, {'rule_outcomes': [{'code': 'UNKNOWN'}]},
    {'finished_at': '2026-11-03T11:59:00+08:00'}, {'finished_at': '2026-11-03T13:00:00'},
    {'unresolved_data': [], 'rule_outcomes': []}, {'production_database_sha256': '0' * 64},
])
def test_bad_review_preserves_every_candidate(tmp_path, change):
    plan, _, _, _, _, _, review = _fixture(tmp_path)
    _write(tmp_path, 'logs/business.json', {**review, **change})
    with pytest.raises(finalize.FinalizationError):
        finalize.apply(tmp_path, plan, business_receipt='logs/business.json', maintenance_confirmed=True)
    assert all((tmp_path / r['path']).exists() for r in plan['candidates'])
    assert not (tmp_path / plan['state_path']).exists()


def test_clean_complete_review_and_rule_only_partial_are_allowed(tmp_path):
    plan, _, _, _, _, _, review = _fixture(tmp_path)
    _write(tmp_path, 'logs/business.json', {**review, 'workflow_state': 'COMPLETE',
                                          'unresolved_data': [], 'rule_outcomes': []})
    assert finalize.apply(tmp_path, plan, business_receipt='logs/business.json',
                          maintenance_confirmed=True)['phase'] == 'COMPLETE'


@pytest.mark.parametrize('damage', ['candidate', 'wrapper', 'object', 'missing'])
def test_preflight_failure_never_starts_deletion(tmp_path, damage):
    plan, _, _, _, _, ref, _ = _fixture(tmp_path)
    candidate = tmp_path / plan['candidates'][-1]['path']
    if damage == 'candidate':
        candidate.write_bytes(b'changed original')
    elif damage == 'wrapper':
        (tmp_path / plan['candidates'][-1]['production_path']).write_bytes(b'changed wrapper')
    elif damage == 'object':
        obj = tmp_path / ref['_evidence_ref']['path']
        obj.chmod(0o600)
        obj.write_bytes(b'broken compression')
    else:
        candidate.unlink()
    with pytest.raises(ValueError):
        finalize.apply(tmp_path, plan, business_receipt='logs/business.json', maintenance_confirmed=True)
    assert (tmp_path / plan['candidates'][0]['path']).exists()
    assert (tmp_path / plan['candidates'][1]['path']).exists()


def test_resume_after_unlink_before_receipt(tmp_path):
    plan, _, _, _, _, _, _ = _fixture(tmp_path)

    def interrupt(phase, path):
        raise RuntimeError('isolated interruption')

    with pytest.raises(RuntimeError):
        finalize.apply(tmp_path, plan, business_receipt='logs/business.json',
                       maintenance_confirmed=True, checkpoint=interrupt)
    assert finalize.apply(tmp_path, plan, business_receipt='logs/business.json',
                          maintenance_confirmed=True)['phase'] == 'COMPLETE'


def test_lock_and_explicit_maintenance_required(tmp_path):
    plan, _, _, _, _, _, _ = _fixture(tmp_path)
    with pytest.raises(finalize.FinalizationError, match='MAINTENANCE'):
        finalize.archive(tmp_path, plan)
    with writer_lock(SimpleNamespace(root=tmp_path)), pytest.raises(RuntimeError):
        finalize.archive(tmp_path, plan, maintenance_confirmed=True)
    assert all((tmp_path / r['path']).exists() for r in plan['candidates'])


@pytest.mark.parametrize('item', ['production', 'rollback', 'staging_database'])
def test_pending_database_transactions_block_finalization(tmp_path, item):
    plan, _, production, _, _, _, _ = _fixture(tmp_path)
    path = production if item == 'production' else tmp_path / next(
        r['path'] for r in plan['candidates'] if r['kind'] == item)
    Path(str(path) + '-wal').write_bytes(b'pending transaction')
    with pytest.raises(finalize.FinalizationError, match='SIDECAR'):
        finalize.apply(tmp_path, plan, business_receipt='logs/business.json', maintenance_confirmed=True)
    assert all((tmp_path / r['path']).exists() for r in plan['candidates'])


def test_rehashed_plan_cannot_expand_allowlist(tmp_path):
    plan, _, production, _, _, _, _ = _fixture(tmp_path)
    plan['candidates'][1]['path'] = 'data/state/ledger.sqlite3'
    plan['plan_sha256'] = _digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
    with pytest.raises(finalize.FinalizationError, match='ALLOWLIST'):
        finalize.archive(tmp_path, plan, maintenance_confirmed=True)
    assert production.exists()


@pytest.mark.parametrize('destination', ['data/state/ledger.sqlite3', '../outside.sqlite3',
                                        'data/maintenance/test/ledger.sqlite3'])
def test_restore_cannot_overwrite_production_or_non_recovery_paths(tmp_path, destination):
    plan, _, production, _, _, _, _ = _fixture(tmp_path)
    state = finalize.archive(tmp_path, plan, maintenance_confirmed=True)
    before = production.read_bytes()
    with pytest.raises(finalize.FinalizationError, match='DESTINATION'):
        finalize.restore(tmp_path, state, destination=destination)
    assert production.read_bytes() == before


def test_restore_refuses_existing_file_and_corruption_leaves_no_target(tmp_path):
    plan, _, _, _, _, _, _ = _fixture(tmp_path)
    state = finalize.archive(tmp_path, plan, maintenance_confirmed=True)
    destination = 'data/maintenance/recovery-test/ledger.sqlite3'
    target = tmp_path / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(b'existing')
    with pytest.raises(finalize.FinalizationError, match='EXISTS'):
        finalize.restore(tmp_path, state, destination=destination)
    target.unlink()
    obj = tmp_path / state['baseline_ref']['_evidence_ref']['path']
    obj.chmod(0o600)
    obj.write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        finalize.restore(tmp_path, state, destination=destination)
    assert not target.exists()
