"""Review, archive, and explicitly remove only proven maintenance duplicates."""

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from china_stock_sim.common.evidence_objects import is_reference, iter_bytes, put_blob, verify
from china_stock_sim.ledger.kernel import ledger_lock
from china_stock_sim.ledger.storage_promotion import _digest, _hash, _json, _path, _sync, _write
from china_stock_sim.scheduler.workspace import writer_lock


class FinalizationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise FinalizationError(code)


def _stamp(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        _fail('FINALIZE_NOT_REGULAR')
    return {'bytes': value.st_size, 'allocated_bytes': value.st_blocks * 512,
            'device': value.st_dev, 'inode': value.st_ino, 'mtime_ns': value.st_mtime_ns,
            'links': value.st_nlink}


def _proof(root, path):
    target = _path(root, path)
    return {'path': path, 'sha256': _hash(target)}, _json(target)


def _closed_database(path):
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            _fail('FINALIZE_ACTIVE_DATABASE_SIDECAR')


def _documents(root, promotion, receipt):
    if receipt.get('state') != 'COMPLETE':
        _fail('FINALIZE_DOCUMENT_PROOF_INCOMPLETE')
    result = {}
    for row in receipt.get('results', []):
        name, reference = row['document']['path'], row['object_ref']
        if (not name.startswith('data/plans/') or not is_reference(reference)
                or row.get('original_sha256') != reference['_evidence_ref']['sha256']):
            _fail('FINALIZE_DOCUMENT_PROOF_INVALID')
        relative = str(Path(promotion['staging_root']) / name)
        _path(root, relative)
        if relative in result:
            _fail('FINALIZE_DOCUMENT_DUPLICATE')
        result[relative] = {'kind': 'staging_document', 'path': relative,
            'sha256': row['original_sha256'], 'production_path': name,
            'wrapper_sha256': row['wrapper_sha256'], 'object_ref': reference}
    return result


def prepare(root, *, promotion_plan, document_receipt):
    """Read-only, fixed-path plan. No archive/object/database is written."""
    root = Path(root).resolve()
    promotion_proof, promotion = _proof(root, promotion_plan)
    document_proof, documents = _proof(root, document_receipt)
    if promotion.get('plan_sha256') != _digest({k: v for k, v in promotion.items()
                                               if k != 'plan_sha256'}):
        _fail('FINALIZE_PROMOTION_PLAN_INVALID')
    current = _json(_path(root, promotion['state_path']))
    if current.get('phase') != 'VERIFIED' or current.get('plan_sha256') != promotion['plan_sha256']:
        _fail('FINALIZE_PROMOTION_NOT_VERIFIED')
    rollback = promotion['rollback_path']
    staging_db = str(Path(promotion['staging_root']) / 'data/state/ledger.sqlite3')
    candidates = []
    for name, kind in [(rollback, 'rollback'), (staging_db, 'staging_database')]:
        path = _path(root, name)
        _closed_database(path)
        if path.samefile(_path(root, 'data/state/ledger.sqlite3')) or path.samefile(
                _path(root, promotion['compact_path'])) or path.stat().st_nlink != 1:
            _fail('FINALIZE_DATABASE_NOT_ISOLATED')
        candidates.append({'path': name, 'kind': kind, 'stat': _stamp(path),
            'sha256': promotion['source_sha256'] if kind == 'rollback' else _hash(path)})
    for row in _documents(root, promotion, documents).values():
        path = _path(root, row['path'])
        if not path.exists():
            _fail('FINALIZE_STAGING_DOCUMENT_MISSING')
        if _hash(path) != row['sha256'] or _hash(_path(root, row['production_path'])) != row['wrapper_sha256']:
            _fail('FINALIZE_DOCUMENT_CHANGED')
        candidates.append({**row, 'stat': _stamp(path)})
    result = {'schema_version': 1, 'state': 'PLANNED', 'read_only': True,
        'created_at': datetime.now().astimezone().isoformat(),
        'promotion': promotion_proof, 'documents': document_proof,
        'promotion_plan_sha256': promotion['plan_sha256'],
        'state_path': str(Path(promotion['rollback_path']).parent / 'finalization.json'),
        'baseline_sha256': promotion['source_sha256'], 'baseline_bytes': promotion['source_bytes'],
        'candidates': candidates, 'retained_compact_path': promotion['compact_path'],
        'logical_candidate_bytes': sum(r['stat']['bytes'] for r in candidates),
        'exclusive_reclaimable_bytes': None,
        'restore_destination_example': f'data/maintenance/recovery-{promotion["promotion_id"]}/ledger.sqlite3',
        'recovery_boundary': 'Isolated original baseline only; never overwrite advanced production.'}
    result['plan_sha256'] = _digest(result)
    return result


def _validate(root, plan):
    if plan.get('plan_sha256') != _digest({k: v for k, v in plan.items() if k != 'plan_sha256'}):
        _fail('FINALIZE_PLAN_INVALID')
    proofs = {}
    for key in ('promotion', 'documents'):
        item = plan[key]
        if _hash(_path(root, item['path'])) != item['sha256']:
            _fail('FINALIZE_PROOF_CHANGED')
        proofs[key] = _json(_path(root, item['path']))
    promotion = proofs['promotion']
    current = _json(_path(root, promotion['state_path']))
    if (current.get('phase') != 'VERIFIED'
            or current.get('plan_sha256') != plan['promotion_plan_sha256']
            or promotion['plan_sha256'] != plan['promotion_plan_sha256']
            or promotion['source_sha256'] != plan['baseline_sha256']
            or promotion['source_bytes'] != plan['baseline_bytes']
            or plan['state_path'] != str(Path(promotion['rollback_path']).parent / 'finalization.json')):
        _fail('FINALIZE_PROMOTION_CHANGED')
    allowed = _documents(root, promotion, proofs['documents'])
    allowed[promotion['rollback_path']] = {'kind': 'rollback', 'sha256': plan['baseline_sha256']}
    allowed[str(Path(promotion['staging_root']) / 'data/state/ledger.sqlite3')] = {'kind': 'staging_database'}
    if len(plan['candidates']) != len(allowed) or {r['path'] for r in plan['candidates']} != set(allowed):
        _fail('FINALIZE_ALLOWLIST_INVALID')
    for row in plan['candidates']:
        if any(row.get(key) != value for key, value in allowed[row['path']].items()):
            _fail('FINALIZE_ALLOWLIST_INVALID')
        path = _path(root, row['path'])
        if row['kind'] in {'rollback', 'staging_database'}:
            _closed_database(path)
        if path.exists() and path.samefile(_path(root, 'data/state/ledger.sqlite3')):
            _fail('FINALIZE_DATABASE_NOT_ISOLATED')
    return promotion, current


def _state(root, plan):
    target = _path(root, plan['state_path'])
    value = _json(target) if target.exists() else {'schema_version': 1,
        'plan_sha256': plan['plan_sha256'], 'phase': 'PLANNED', 'deleted': [], 'pending': None}
    if value.get('plan_sha256') != plan['plan_sha256']:
        _fail('FINALIZE_STATE_CONFLICT')
    return target, value


def _archive(root, plan, target, state):
    if 'baseline_ref' in state:
        reference = state['baseline_ref']
        verify(root, reference)
    else:
        row = next(r for r in plan['candidates'] if r['kind'] == 'rollback')
        source = _path(root, row['path'])
        if _stamp(source) != row['stat']:
            _fail('FINALIZE_SOURCE_CHANGED')
        with source.open('rb') as stream:
            reference = put_blob(root, stream, max_bytes=plan['baseline_bytes'], encoding='bytes+zlib')
        if _stamp(source) != row['stat']:
            _fail('FINALIZE_SOURCE_CHANGED')
    metadata = reference['_evidence_ref']
    if metadata['sha256'] != plan['baseline_sha256'] or metadata['bytes'] != plan['baseline_bytes']:
        _fail('FINALIZE_BASELINE_MISMATCH')
    state.update(baseline_ref=reference, baseline_verified_at=datetime.now().astimezone().isoformat())
    if state['phase'] == 'PLANNED':
        state['phase'] = 'ARCHIVED'
    _write(target, state)


def archive(root, plan, *, maintenance_confirmed=False):
    if maintenance_confirmed is not True:
        _fail('FINALIZE_MAINTENANCE_REQUIRED')
    root = Path(root).resolve()
    with writer_lock(SimpleNamespace(root=root)), ledger_lock(root):
        _validate(root, plan)
        target, state = _state(root, plan)
        _archive(root, plan, target, state)
        return state


def _business(root, plan, promotion_state, receipt_path):
    proof, value = _proof(root, receipt_path)
    flags = ('business_recovery_reviewed', 'production_verified', 'no_program_errors',
             'original_fills_unchanged', 'new_fills_traceable')
    if (value.get('state') != 'RULE_COMPLIANT_RECOVERY_COMPLETE'
            or any(value.get(name) is not True for name in flags)
            or value.get('promotion_plan_sha256') != plan['promotion_plan_sha256']
            or not isinstance(value.get('run_id'), str) or not value['run_id']
            or value.get('workflow_state') not in {'COMPLETE', 'PARTIAL'}):
        _fail('FINALIZE_BUSINESS_PROOF_INCOMPLETE')
    pending, outcomes = value.get('unresolved_data'), value.get('rule_outcomes')
    if (not isinstance(pending, list) or not isinstance(outcomes, list)
            or any(not isinstance(row, dict) or row.get('code') != 'UNDATED_EVENT' for row in pending)
            or any(not isinstance(row, dict) or row.get('code') != 'MISSED_CUTOFF' for row in outcomes)
            or (value['workflow_state'] == 'PARTIAL' and not pending and not outcomes)):
        _fail('FINALIZE_UNREVIEWED_OUTCOME')
    try:
        finished = datetime.fromisoformat(value['finished_at'])
        promoted = datetime.fromisoformat(promotion_state['verified_at'])
        if finished.utcoffset() is None or promoted.utcoffset() is None or finished < promoted:
            _fail('FINALIZE_BUSINESS_TIME_INVALID')
    except (KeyError, TypeError, ValueError):
        _fail('FINALIZE_BUSINESS_TIME_INVALID')
    production = _path(root, 'data/state/ledger.sqlite3')
    if _hash(production) != value.get('production_database_sha256'):
        _fail('FINALIZE_PRODUCTION_CHANGED')
    _closed_database(production)
    return proof


def apply(root, plan, *, business_receipt, maintenance_confirmed=False, checkpoint=None):
    """Delete only after independently reviewed, rule-compliant business recovery."""
    if maintenance_confirmed is not True:
        _fail('FINALIZE_MAINTENANCE_REQUIRED')
    root = Path(root).resolve()
    with writer_lock(SimpleNamespace(root=root)), ledger_lock(root):
        _, promotion_state = _validate(root, plan)
        proof = _business(root, plan, promotion_state, business_receipt)
        target, state = _state(root, plan)
        if state.get('business_proof') not in (None, proof):
            _fail('FINALIZE_BUSINESS_PROOF_CHANGED')
        _archive(root, plan, target, state)
        deleted = {r['path'] for r in state['deleted']}
        known = {r['path'] for r in plan['candidates']}
        if not deleted <= known or state['pending'] not in known | {None}:
            _fail('FINALIZE_STATE_INVALID')
        # Full preflight precedes any deletion. Repeat uses pending to close an interrupted unlink.
        for row in plan['candidates']:
            path = _path(root, row['path'])
            if row['path'] in deleted:
                if path.exists():
                    _fail('FINALIZE_DELETED_PATH_REAPPEARED')
                continue
            if not path.exists():
                if state['pending'] != row['path']:
                    _fail('FINALIZE_UNEXPECTED_MISSING')
            elif _stamp(path) != row['stat'] or _hash(path) != row['sha256']:
                _fail('FINALIZE_CANDIDATE_CHANGED')
            if row['kind'] == 'staging_document':
                if _hash(_path(root, row['production_path'])) != row['wrapper_sha256']:
                    _fail('FINALIZE_DOCUMENT_CHANGED')
                verify(root, row['object_ref'])
        state.update(business_proof=proof, phase='DELETING')
        _write(target, state)
        for row in plan['candidates']:
            if row['path'] in deleted:
                continue
            path = _path(root, row['path'])
            state['pending'] = row['path']
            _write(target, state)
            if path.exists():
                if _stamp(path) != row['stat']:
                    _fail('FINALIZE_CANDIDATE_CHANGED')
                path.unlink()
                _sync(path.parent)
            if checkpoint:
                checkpoint('UNLINKED', row['path'])
            state['deleted'].append({'path': row['path'], 'sha256': row['sha256'],
                'bytes': row['stat']['bytes'], 'allocated_bytes': row['stat']['allocated_bytes']})
            state['pending'] = None
            _write(target, state)
        state.update(phase='COMPLETE', finished_at=datetime.now().astimezone().isoformat(),
            deleted_logical_bytes=sum(r['bytes'] for r in state['deleted']),
            exclusive_reclaimed_bytes=None, production_modified=False)
        _write(target, state)
        return state


def restore(root, state, *, destination):
    """Restore the exact old baseline to a new isolated file, never to the production DB."""
    root = Path(root).resolve()
    parts = Path(destination).parts
    if (len(parts) < 4 or parts[:2] != ('data', 'maintenance')
            or not parts[2].startswith('recovery-')):
        _fail('FINALIZE_RESTORE_DESTINATION_INVALID')
    target = _path(root, destination)
    if target.exists():
        _fail('FINALIZE_RESTORE_DESTINATION_EXISTS')
    reference = state.get('baseline_ref')
    if not is_reference(reference) or reference['_evidence_ref']['encoding'] != 'bytes+zlib':
        _fail('FINALIZE_BASELINE_REFERENCE_INVALID')
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.restore-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            for chunk in iter_bytes(root, reference):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic create-if-absent; a racing file can never be overwritten.
        os.link(temporary, target)
        _sync(target.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'state': 'RESTORED_ISOLATED', 'path': destination,
        'sha256': reference['_evidence_ref']['sha256'], 'bytes': reference['_evidence_ref']['bytes'],
        'production_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'archive', 'apply', 'restore'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--promotion-plan')
    parser.add_argument('--document-receipt')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--business-receipt')
    parser.add_argument('--archive-state', type=Path)
    parser.add_argument('--destination')
    parser.add_argument('--maintenance-confirmed', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        if not args.promotion_plan or not args.document_receipt:
            parser.error('prepare requires promotion plan and document receipt')
        result = prepare(args.root, promotion_plan=args.promotion_plan,
                         document_receipt=args.document_receipt)
    elif args.action == 'restore':
        if not args.archive_state or not args.destination:
            parser.error('restore requires archive state and isolated destination')
        result = restore(args.root, _json(args.archive_state), destination=args.destination)
    else:
        if not args.plan or not args.maintenance_confirmed:
            parser.error('archive/apply require plan and maintenance confirmation')
        if args.action == 'apply' and not args.business_receipt:
            parser.error('apply requires business success receipt')
        plan = _json(args.plan)
        result = (archive(args.root, plan, maintenance_confirmed=True) if args.action == 'archive'
                  else apply(args.root, plan, business_receipt=args.business_receipt,
                             maintenance_confirmed=True))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
