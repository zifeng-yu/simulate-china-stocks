"""Explicit, reversible storage compaction. Never starts or replaces a live ledger."""

import gc
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from china_stock_sim.common.evidence_objects import (
    EvidenceObjectError,
    is_reference,
    put_blob,
    put_json,
    verify,
)

STORAGE_FORMAT = 2
TARGET_SCHEMA_VERSION = 4
MAX_ROW_BYTES = 512 * 1024 * 1024
MAX_PROJECTED_BYTES = 16 * 1024 * 1024
RECORD_COLUMNS = {'orders': 'data', 'fills': 'data', 'daily_nav': 'data', 'operations': 'result'}
_PRODUCTION = Path(__file__).resolve().parents[3]
_FILL_FIELDS = (
    'fill_id', 'order_id', 'plan_id', 'result_id', 'code', 'side', 'target_session',
    'effective_date', 'processed_at', 'state', 'quantity', 'price', 'amount_fen',
    'commission_fen', 'stamp_fen', 'transfer_fen', 'fees_fen', 'net_cash_fen',
    'cost_fen', 'realized_pnl_fen', 'lot_allocations', 'fee_policy_hash',
    'rule_policy_hash', 'commit_guard_hash', 'account_view_hash',
)
_PROJECTIONS = {
    'orders': ('order_id', 'plan_id', 'code', 'side', 'quantity', 'limit_price',
               'status', 'result_hash', 'reason'),
    'fills': _FILL_FIELDS,
    'plans': ('plan_id', 'target_session', 'document', 'fee_policy_hash',
              'rule_policy_hash', 'decision_ref', 'frozen_at'),
}
_SMALL_TABLES = ('accounts', 'account_state', 'cash_entries', 'reservations',
                 'position_lots', 'lot_movements', 'daily_positions', 'entitlements',
                 'tax_items', 'corporate_action_cases', 'corrections')


class StorageMigrationError(ValueError):
    def __init__(self, code):
        self.code = self.state = code
        super().__init__(code)


def _error(code):
    return StorageMigrationError(code)


def _database(root, database=None):
    root = Path(root).resolve()
    path = Path(database) if database is not None else root / 'data/state/ledger.sqlite3'
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise _error('STORAGE_DATABASE_INVALID')
    return path


def _connect(path, *, readonly):
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, timeout=0)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return db


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode()


def capture_baseline(db):
    """Hash small financial projections, never materialize the giant evidence columns."""
    present, sections, counts, fills = _tables(db), {}, {}, []
    for table in sorted(present - {'sqlite_sequence'}):
        counts[table] = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    for table in _SMALL_TABLES:
        if table not in present:
            continue
        # These tables contain financial state, not execution account snapshots.
        rows = [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
        sections[table] = hashlib.sha256(_encoded(rows)).hexdigest()
    for table, fields in _PROJECTIONS.items():
        if table not in present:
            continue
        paths = ','.join("'$." + field + "'" for field in fields)
        rows = []
        for row in db.execute(f'SELECT id,account_id,json_extract(data,{paths}) projection '
                              f'FROM {table} ORDER BY account_id,id'):
            values = json.loads(row['projection'])
            item = {'id': row['id'], 'account_id': row['account_id'],
                    **dict(zip(fields, values, strict=True))}
            rows.append(item)
        if table == 'fills':
            fills = rows
        sections[table] = hashlib.sha256(_encoded(rows)).hexdigest()
    if 'daily_nav' in present:
        rows = []
        for row in db.execute("SELECT id,account_id,json_remove(data,'$.orders','$.fills',"
                              "'$.original_record_ref','$.storage_format') projection "
                              "FROM daily_nav ORDER BY account_id,id"):
            rows.append({'id': row['id'], 'account_id': row['account_id'],
                         'projection': json.loads(row['projection'])})
        sections['daily_nav'] = hashlib.sha256(_encoded(rows)).hexdigest()
    if 'operations' in present:
        rows = [dict(r) for r in db.execute('SELECT account_id,operation_id,payload_hash,processed_at '
                                          'FROM operations ORDER BY account_id,operation_id')]
        sections['operations'] = hashlib.sha256(_encoded(rows)).hexdigest()
    return {'counts': counts, 'sections': sections, 'fills': fills,
            'financial_sha256': hashlib.sha256(_encoded({'counts': counts,
                                                        'sections': sections})).hexdigest()}


def verify_financial_equivalence(before, after):
    if (before['counts'] != after['counts'] or before['sections'] != after['sections'] or
            before['financial_sha256'] != after['financial_sha256']):
        raise _error('STORAGE_FINANCIAL_MISMATCH')
    return {'state': 'READY', 'financial_sha256': before['financial_sha256'],
            'fill_count': len(before['fills'])}


def _inventory(db):
    present, rows = _tables(db), []
    for table, column in RECORD_COLUMNS.items():
        if table not in present:
            continue
        key = 'operation_id' if table == 'operations' else 'id'
        for row in db.execute(f'SELECT rowid AS row_id,account_id,{key} AS identity,'
                              f'length(CAST({column} AS BLOB)) AS bytes '
                              f'FROM {table} ORDER BY rowid'):
            rows.append({'table': table, 'column': column, **dict(row)})
    return rows


def dry_run(root, database=None, *, include_baseline=True):
    """Read-only production-safe inventory; does not create an object or migration receipt."""
    path = _database(root, database)
    with closing(_connect(path, readonly=True)) as db:
        db.execute('BEGIN')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (3, TARGET_SCHEMA_VERSION):
            raise _error('STORAGE_SCHEMA_UNSUPPORTED')
        rows = _inventory(db)
        result = {'state': 'READY', 'read_only': True, 'schema_version': version,
                  'target_schema_version': TARGET_SCHEMA_VERSION, 'records': rows,
                  'record_count': len(rows), 'logical_record_bytes': sum(r['bytes'] for r in rows),
                  'database_bytes': path.stat().st_size}
        if include_baseline:
            result['baseline'] = capture_baseline(db)
        return result


def archive_record(root, db, table, rowid):
    """Archive the exact existing TEXT bytes through SQLite's incremental read interface."""
    if table not in RECORD_COLUMNS or type(rowid) is not int or rowid <= 0:
        raise _error('STORAGE_RECORD_INVALID')
    with db.blobopen(table, RECORD_COLUMNS[table], rowid, readonly=True) as blob:
        reference = put_blob(root, blob)
    return reference


def _bounded_json(value, max_bytes):
    chunks, size = [], 0
    for chunk in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':'), allow_nan=False).iterencode(value):
        size += len(chunk.encode())
        if size > max_bytes:
            raise _error('STORAGE_PROJECTION_TOO_LARGE')
        chunks.append(chunk)
    return ''.join(chunks)


def _projection_signature(record):
    """Bind every retained field, including nested report/order/fill financial values."""
    digest = hashlib.sha256()

    def emit(text):
        digest.update(text.encode())

    def visit(value, *, top=False):
        if isinstance(value, dict):
            emit('{')
            keys = sorted(k for k in value if not top or
                          k not in {'original_record_ref', 'storage_format'})
            for index, key in enumerate(keys):
                if index:
                    emit(',')
                emit(json.dumps(key, ensure_ascii=False))
                emit(':')
                if key == 'account_view_evidence' and value[key] is not None:
                    emit('"ACCOUNT_VIEW_PRESERVED_BY_REFERENCE"')
                else:
                    visit(value[key])
            emit('}')
        elif isinstance(value, list):
            emit('[')
            for index, child in enumerate(value):
                if index:
                    emit(',')
                visit(child)
            emit(']')
        else:
            emit(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))

    visit(record, top=True)
    return digest.hexdigest()


def compact_record(root, record, *, max_projected_bytes=MAX_PROJECTED_BYTES):
    """Replace only the explicitly designated bulky evidence key; retain all other fields."""
    if not isinstance(record, dict):
        raise _error('STORAGE_RECORD_INVALID')
    references = {}

    def visit(value):
        if isinstance(value, dict):
            if '_evidence_ref' in value:
                if not is_reference(value):
                    raise _error('STORAGE_REFERENCE_INVALID')
                verify(root, value)
                references[value['_evidence_ref']['sha256']] = value
                return
            for key, child in list(value.items()):
                if key == 'account_view_evidence':
                    if is_reference(child):
                        verify(root, child)
                        reference = child
                    elif child is None:
                        continue
                    elif isinstance(child, dict):
                        if '_evidence_ref' in child:
                            raise _error('STORAGE_REFERENCE_INVALID')
                        reference = put_json(root, child)
                    else:
                        raise _error('STORAGE_ACCOUNT_EVIDENCE_INVALID')
                    value[key] = reference
                    references[reference['_evidence_ref']['sha256']] = reference
                else:
                    if key == 'data_evidence':
                        _bounded_json(child, max_projected_bytes)
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(record)
    _bounded_json(record, max_projected_bytes)
    return {'record': record, 'references': list(references.values())}


def apply_isolated(root, database=None, *, max_row_bytes=MAX_ROW_BYTES,
                   max_projected_bytes=MAX_PROJECTED_BYTES, progress=None):
    """Apply once to an isolated ledger, with exact originals and financial equivalence.

    Production promotion is deliberately owned by the maintenance orchestrator.
    The same isolated database may be retried after interruption; completed rows
    retain the first original_record_ref, rather than archiving their projection.
    """
    path = _database(root, database)
    production_database = _PRODUCTION / 'data/state/ledger.sqlite3'
    same_production_file = production_database.exists() and path.samefile(production_database)
    if Path(root).resolve() == _PRODUCTION or same_production_file:
        raise _error('STORAGE_PRODUCTION_APPLY_FORBIDDEN')
    if any(type(v) is not int or v <= 0 for v in (max_row_bytes, max_projected_bytes)):
        raise _error('STORAGE_LIMIT_INVALID')
    with closing(_connect(path, readonly=False)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (3, TARGET_SCHEMA_VERSION):
                raise _error('STORAGE_SCHEMA_UNSUPPORTED')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise _error('STORAGE_DATABASE_INVALID')
            before = capture_baseline(db)
            results, changed = [], version != TARGET_SCHEMA_VERSION
            for item in _inventory(db):
                if progress:
                    progress({'event': 'RECORD_START', **item})
                if item['bytes'] > max_row_bytes:
                    raise _error('STORAGE_ROW_TOO_LARGE')
                table, column, rowid = item['table'], item['column'], item['row_id']
                # Only one legacy row is materialized at a time. The original is
                # archived before mutation, without loading it into Python.
                marker = db.execute(f"SELECT json_extract({column},'$.storage_format') "
                                    f'FROM {table} WHERE rowid=?', (rowid,)).fetchone()[0]
                original = None if marker == STORAGE_FORMAT else archive_record(root, db, table, rowid)
                if progress:
                    progress({'event': 'RECORD_ARCHIVED', 'table': table, 'row_id': rowid})
                record = json.loads(db.execute(f'SELECT {column} FROM {table} WHERE rowid=?',
                                               (rowid,)).fetchone()[0])
                if not isinstance(record, dict):
                    raise _error('STORAGE_RECORD_INVALID')
                retained_before = _projection_signature(record)
                if marker == STORAGE_FORMAT:
                    original = record.get('original_record_ref')
                    if not is_reference(original):
                        raise _error('STORAGE_ORIGINAL_MISSING')
                    verify(root, original)
                elif 'original_record_ref' in record or 'storage_format' in record:
                    raise _error('STORAGE_MARKER_CONFLICT')
                compacted = compact_record(root, record, max_projected_bytes=max_projected_bytes)
                record.update(storage_format=STORAGE_FORMAT, original_record_ref=original)
                retained_after = _projection_signature(record)
                if retained_before != retained_after:
                    raise _error('STORAGE_RECORD_PROJECTION_MISMATCH')
                encoded = _bounded_json(record, max_projected_bytes)
                if marker != STORAGE_FORMAT:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (encoded, rowid))
                    changed = True
                results.append({**item, 'projected_bytes': len(encoded.encode()),
                                'retained_fields_sha256': retained_after,
                                'original_record_ref': original,
                                'references': compacted['references']})
                del compacted, record, encoded
                gc.collect()
                if progress:
                    progress({'event': 'RECORD_COMPLETE', 'table': table, 'row_id': rowid,
                              'projected_bytes': results[-1]['projected_bytes']})
            after = capture_baseline(db)
            equivalence = verify_financial_equivalence(before, after)
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise _error('STORAGE_FOREIGN_KEY_MISMATCH')
            db.execute(f'PRAGMA user_version={TARGET_SCHEMA_VERSION}')
            db.commit()
            return {'state': 'COMPLETE', 'changed': changed, 'schema_version': TARGET_SCHEMA_VERSION,
                    'storage_format': STORAGE_FORMAT, 'records': results,
                    'before': before, 'after': after, 'equivalence': equivalence,
                    'note': 'ISOLATED_DATABASE_ONLY_NOT_PRODUCTION_PROMOTION'}
        except (StorageMigrationError, EvidenceObjectError):
            db.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
            db.rollback()
            raise _error('STORAGE_MIGRATION_FAILED') from None
