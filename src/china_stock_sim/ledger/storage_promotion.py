"""Explicit maintenance cutover with durable phases and a full database rollback point."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from china_stock_sim.common.evidence_objects import is_reference, verify
from china_stock_sim.ledger.kernel import ledger_lock
from china_stock_sim.ledger.storage_migration import capture_baseline, verify_financial_equivalence
from china_stock_sim.scheduler.workspace import writer_lock


class StoragePromotionError(ValueError):
    def __init__(self, code):
        self.code = self.state = code
        super().__init__(code)


def _error(code):
    return StoragePromotionError(code)


def _path(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise _error('PROMOTION_UNSAFE_PATH')
    path = Path(root).resolve()
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink():
            raise _error('PROMOTION_UNSAFE_PATH')
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise _error('PROMOTION_UNSAFE_PATH')
    return path


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _json(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise _error('PROMOTION_RECEIPT_TOO_LARGE')
    return json.loads(path.read_bytes())


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.promotion-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(body, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _configs(root):
    rows = []
    for path in sorted(_path(root, 'configs').glob('*.toml')):
        if path.is_symlink():
            raise _error('PROMOTION_CONFIG_UNSAFE')
        if path.is_file():
            rows.append({'path': str(path.relative_to(root)), 'sha256': _hash(path)})
    return rows


def _connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def _references(path):
    found = {}

    def visit(value):
        if isinstance(value, dict):
            if '_evidence_ref' in value:
                if not is_reference(value):
                    raise _error('PROMOTION_REFERENCE_INVALID')
                found[value['_evidence_ref']['sha256'], value['_evidence_ref']['encoding']] = value
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    with closing(_connect(path)) as db:
        for saved in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            table = saved[0]
            if not table.replace('_', '').isalnum():
                raise _error('PROMOTION_TABLE_INVALID')
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            column = 'result' if table == 'operations' else 'data' if 'data' in columns else None
            if column:
                for row in db.execute(f'SELECT length(CAST({column} AS BLOB)) size,{column} value FROM {table}'):
                    if row['size'] > 64 * 1024 * 1024:
                        raise _error('PROMOTION_COMPACT_ROW_TOO_LARGE')
                    visit(json.loads(row['value']))
    return [found[key] for key in sorted(found)]


def prepare_promotion(root, *, staging_receipt, projection_receipt):
    """Read-only plan. The final source-database hash is checked once, under cutover locks."""
    root = Path(root).resolve()
    staging_path = _path(root, staging_receipt)
    projection_path = _path(root, projection_receipt)
    staging, projection = _json(staging_path), _json(projection_path)
    if staging.get('state') != 'COMPLETE' or projection.get('state') != 'COMPLETE':
        raise _error('PROMOTION_PROOF_INCOMPLETE')
    staging_sha = _hash(staging_path)
    if projection.get('source_staging_receipt_sha256') != staging_sha:
        raise _error('PROMOTION_PROOF_MISMATCH')
    expected_baseline = staging['final_baseline']
    if projection.get('financial_sha256') != expected_baseline['financial_sha256']:
        raise _error('PROMOTION_FINANCIAL_PROOF_MISMATCH')
    compact = _path(root, projection['compact_path'])
    if compact.parent.parts[-2:] != ('data', 'state'):
        raise _error('PROMOTION_STAGING_PATH_INVALID')
    staging_root = compact.parents[2]
    production = _path(root, 'data/state/ledger.sqlite3')
    if staging_root == root or compact.samefile(production):
        raise _error('PROMOTION_STAGING_NOT_ISOLATED')
    compact_sha = _hash(compact)
    if compact_sha != projection.get('compact_sha256'):
        raise _error('PROMOTION_COMPACT_HASH_MISMATCH')
    with closing(_connect(compact)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise _error('PROMOTION_SCHEMA_MISMATCH')
        verify_financial_equivalence(expected_baseline, capture_baseline(db))
    identity = uuid4().hex
    plan = {'schema_version': 1, 'promotion_id': identity, 'state': 'PLANNED', 'read_only': True,
        'created_at': datetime.now().astimezone().isoformat(),
        'production_path': 'data/state/ledger.sqlite3',
        'source_sha256': staging['source_sha256'], 'source_bytes': staging['source_bytes'],
        'compact_path': projection['compact_path'], 'compact_sha256': compact_sha,
        'compact_bytes': compact.stat().st_size,
        'staging_root': str(staging_root.relative_to(root)),
        'staging_receipt': {'path': staging_receipt, 'sha256': staging_sha},
        'projection_receipt': {'path': projection_receipt, 'sha256': _hash(projection_path)},
        'financial_baseline': expected_baseline, 'configurations': _configs(root),
        'references': _references(compact), 'verified_cas_proofs': staging.get('cas', {}).get('checks', []),
        'state_path': f'data/maintenance/promotion-{identity}/promotion.json',
        'rollback_path': f'data/maintenance/promotion-{identity}/rollback.sqlite3',
        'forward_path': f'data/maintenance/promotion-{identity}/rolled-forward.sqlite3',
        'temporary_path': f'data/state/.ledger-promotion-{identity}.sqlite3'}
    plan['plan_sha256'] = _digest(plan)
    return plan


def _validate_plan(root, plan, *, require_staging=True):
    if plan.get('plan_sha256') != _digest({k: v for k, v in plan.items() if k != 'plan_sha256'}):
        raise _error('PROMOTION_PLAN_INVALID')
    if plan.get('production_path') != 'data/state/ledger.sqlite3':
        raise _error('PROMOTION_PRODUCTION_PATH_INVALID')
    identity = plan.get('promotion_id')
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise _error('PROMOTION_IDENTITY_INVALID')
    expected_paths = {'state_path': f'data/maintenance/promotion-{identity}/promotion.json',
        'rollback_path': f'data/maintenance/promotion-{identity}/rollback.sqlite3',
        'forward_path': f'data/maintenance/promotion-{identity}/rolled-forward.sqlite3',
        'temporary_path': f'data/state/.ledger-promotion-{identity}.sqlite3'}
    if any(plan.get(key) != value for key, value in expected_paths.items()):
        raise _error('PROMOTION_PATH_BINDING_INVALID')
    receipts = {}
    for key in ('staging_receipt', 'projection_receipt'):
        item = plan[key]
        path = _path(root, item['path'])
        if _hash(path) != item['sha256']:
            raise _error('PROMOTION_PROOF_CHANGED')
        receipts[key] = _json(path)
    staging, projection = receipts['staging_receipt'], receipts['projection_receipt']
    if (plan['source_sha256'] != staging.get('source_sha256') or
            plan['financial_baseline'] != staging.get('final_baseline') or
            plan['verified_cas_proofs'] != staging.get('cas', {}).get('checks', []) or
            plan['compact_path'] != projection.get('compact_path') or
            plan['compact_sha256'] != projection.get('compact_sha256') or
            projection.get('source_staging_receipt_sha256') != plan['staging_receipt']['sha256']):
        raise _error('PROMOTION_PROOF_MISMATCH')
    if require_staging and _references(_path(root, plan['compact_path'])) != plan['references']:
        raise _error('PROMOTION_REFERENCE_SET_CHANGED')
    if _configs(root) != plan['configurations']:
        raise _error('PROMOTION_CONFIG_CHANGED')


def _merge_objects(root, plan):
    stage = _path(root, plan['staging_root'])
    proven = {(p['sha256'], p['stored_sha256'], p['bytes'], p['stored_bytes'])
              for p in plan['verified_cas_proofs'] if p.get('state') == 'READY'}
    result = {'created': 0, 'reused': 0, 'fully_verified_new_objects': 0}
    for reference in plan['references']:
        metadata = reference['_evidence_ref']
        source = _path(stage, metadata['path'])
        expected = (metadata['sha256'], metadata['stored_sha256'],
                    metadata['bytes'], metadata['stored_bytes'])
        if source.stat().st_size != metadata['stored_bytes'] or _hash(source) != metadata['stored_sha256']:
            raise _error('PROMOTION_CAS_SOURCE_CHANGED')
        if expected not in proven:
            verify(stage, reference)
            result['fully_verified_new_objects'] += 1
        if not stat.S_ISREG(source.lstat().st_mode) or stat.S_IMODE(source.stat().st_mode) & 0o222:
            raise _error('PROMOTION_CAS_NOT_IMMUTABLE')
        destination = _path(root, metadata['path'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if (not stat.S_ISREG(destination.lstat().st_mode) or
                    stat.S_IMODE(destination.stat().st_mode) & 0o222 or
                    destination.stat().st_size != metadata['stored_bytes'] or
                    _hash(destination) != metadata['stored_sha256']):
                raise _error('PROMOTION_CAS_DESTINATION_CONFLICT')
            result['reused'] += 1
            continue
        # Both paths are immutable content objects, never mutable database files.
        os.link(source, destination)
        _sync(destination.parent)
        result['created'] += 1
    return result


def _copy_compact(root, plan):
    source = _path(root, plan['compact_path'])
    destination = _path(root, plan['temporary_path'])
    if destination.exists():
        if _hash(destination) == plan['compact_sha256']:
            if destination.samefile(source):
                raise _error('PROMOTION_DATABASE_HARDLINK_FORBIDDEN')
            return
        if destination.stat().st_nlink != 1:
            raise _error('PROMOTION_TEMPORARY_CONFLICT')
        # This UUID-bound staging path may contain a crash-truncated copy only.
        destination.unlink()
    digest = hashlib.sha256()
    with source.open('rb') as origin, destination.open('xb') as target:
        for chunk in iter(lambda: origin.read(1024 * 1024), b''):
            target.write(chunk)
            digest.update(chunk)
        target.flush()
        os.fsync(target.fileno())
        os.fchmod(target.fileno(), 0o600)
    if digest.hexdigest() != plan['compact_sha256']:
        raise _error('PROMOTION_COMPACT_CHANGED')
    _sync(destination.parent)


def _quick_check(path, expected):
    with closing(_connect(path)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise _error('PROMOTION_SCHEMA_MISMATCH')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise _error('PROMOTION_QUICK_CHECK_FAILED')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise _error('PROMOTION_FOREIGN_KEY_FAILED')
        return verify_financial_equivalence(expected, capture_baseline(db))


def apply_promotion(root, plan, *, maintenance_confirmed=False, checkpoint=None):
    """Explicit cutover/resume. Caller must stop services; never starts or resets accounts."""
    if maintenance_confirmed is not True:
        raise _error('PROMOTION_MAINTENANCE_REQUIRED')
    root = Path(root).resolve()
    with writer_lock(SimpleNamespace(root=root)), ledger_lock(root):
        _validate_plan(root, plan)
        production = _path(root, plan['production_path'])
        rollback = _path(root, plan['rollback_path'])
        temporary = _path(root, plan['temporary_path'])
        state_path = _path(root, plan['state_path'])
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(production) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise _error('PROMOTION_ACTIVE_DATABASE_SIDECAR')
        state = _json(state_path) if state_path.exists() else {'plan_sha256': plan['plan_sha256'],
            'promotion_id': plan['promotion_id'], 'phase': 'PREPARING', 'plan': plan}
        if state['plan_sha256'] != plan['plan_sha256'] or state['phase'] == 'ROLLED_BACK':
            raise _error('PROMOTION_STATE_CONFLICT')
        production_sha = _hash(production) if production.exists() else None
        source_stat = production.stat() if production.exists() else None
        if rollback.exists() and _hash(rollback) != plan['source_sha256']:
            raise _error('PROMOTION_ROLLBACK_CHANGED')
        if production_sha == plan['compact_sha256'] and rollback.exists():
            state['equivalence'] = _quick_check(production, plan['financial_baseline'])
            state.update(phase='VERIFIED', verified_at=datetime.now().astimezone().isoformat())
            _write(state_path, state)
            return state
        if production_sha not in (None, plan['source_sha256']):
            raise _error('PROMOTION_SOURCE_CHANGED_OR_BUSINESS_ADVANCED')
        if production_sha is None and not rollback.exists():
            raise _error('PROMOTION_ROLLBACK_MISSING')
        if production_sha is not None and rollback.exists():
            raise _error('PROMOTION_AMBIGUOUS_DATABASE_STATE')
        state['objects'] = _merge_objects(root, plan)
        _copy_compact(root, plan)
        state.update(phase='STAGED', updated_at=datetime.now().astimezone().isoformat())
        _write(state_path, state)
        if checkpoint:
            checkpoint('STAGED', state)
        rollback.parent.mkdir(parents=True, exist_ok=True)
        if production.exists():
            latest = production.stat()
            if (latest.st_ino, latest.st_size, latest.st_mtime_ns) != (
                    source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns):
                raise _error('PROMOTION_SOURCE_CHANGED')
            os.replace(production, rollback)
            _sync(production.parent)
            _sync(rollback.parent)
        state.update(phase='OLD_MOVED', updated_at=datetime.now().astimezone().isoformat())
        _write(state_path, state)
        if checkpoint:
            checkpoint('OLD_MOVED', state)
        os.replace(temporary, production)
        _sync(production.parent)
        state.update(phase='SWITCHED', updated_at=datetime.now().astimezone().isoformat())
        _write(state_path, state)
        if checkpoint:
            checkpoint('SWITCHED', state)
        state['equivalence'] = _quick_check(production, plan['financial_baseline'])
        state.update(phase='VERIFIED', verified_at=datetime.now().astimezone().isoformat())
        _write(state_path, state)
        return state


def rollback_promotion(root, plan, *, maintenance_confirmed=False):
    """Restore the complete rollback DB only before any new business write has occurred."""
    if maintenance_confirmed is not True:
        raise _error('PROMOTION_MAINTENANCE_REQUIRED')
    root = Path(root).resolve()
    with writer_lock(SimpleNamespace(root=root)), ledger_lock(root):
        _validate_plan(root, plan, require_staging=False)
        production = _path(root, plan['production_path'])
        rollback = _path(root, plan['rollback_path'])
        forward = _path(root, plan['forward_path'])
        state_path = _path(root, plan['state_path'])
        state = _json(state_path)
        if state['plan_sha256'] != plan['plan_sha256']:
            raise _error('PROMOTION_STATE_CONFLICT')
        if state['phase'] == 'ROLLED_BACK':
            if _hash(production) != plan['source_sha256']:
                raise _error('PROMOTION_BUSINESS_ADVANCED')
            return state
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(production) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise _error('PROMOTION_ACTIVE_DATABASE_SIDECAR')
        if (state['phase'] == 'ROLLBACK_NEW_MOVED' and production.exists() and not rollback.exists()
                and _hash(production) == plan['source_sha256'] and forward.exists()
                and _hash(forward) == plan['compact_sha256']):
            state.update(phase='ROLLED_BACK', rolled_back_at=datetime.now().astimezone().isoformat(),
                         services_started=False)
            _write(state_path, state)
            return state
        if not rollback.exists() or _hash(rollback) != plan['source_sha256']:
            raise _error('PROMOTION_ROLLBACK_CHANGED')
        if production.exists():
            if _hash(production) != plan['compact_sha256']:
                raise _error('PROMOTION_BUSINESS_ADVANCED')
            if forward.exists():
                raise _error('PROMOTION_FORWARD_CONFLICT')
            os.replace(production, forward)
            _sync(production.parent)
            _sync(forward.parent)
            state['phase'] = 'ROLLBACK_NEW_MOVED'
            _write(state_path, state)
        elif state['phase'] not in {'STAGED', 'OLD_MOVED', 'ROLLBACK_NEW_MOVED'}:
            raise _error('PROMOTION_AMBIGUOUS_DATABASE_STATE')
        os.replace(rollback, production)
        _sync(production.parent)
        _sync(rollback.parent)
        state.update(phase='ROLLED_BACK', rolled_back_at=datetime.now().astimezone().isoformat(),
                     services_started=False)
        _write(state_path, state)
        return state
