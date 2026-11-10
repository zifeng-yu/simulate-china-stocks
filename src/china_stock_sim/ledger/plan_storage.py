"""Small frozen-plan indexes with explicit, hash-bound access to the original contract."""

import hashlib
import json
import sqlite3
from pathlib import Path

from china_stock_sim.common.evidence_objects import put_blob
from china_stock_sim.ledger.decision_evidence import (
    cached_validation,
    read_business_document,
    remember_validation,
)
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest, ledger_lock

FORMAT = 'frozen-plan-header-v1'
FIELDS = ('plan_id', 'account_id', 'strategy_id', 'strategy_version', 'target_session',
          'market_as_of', 'input_cutoff', 'generated_at', 'frozen_at', 'status', 'no_order_reason',
          'plan_hash', 'document', 'order_ids', 'orders', 'fee_policy_hash', 'rule_policy_hash',
          'ruleset_hash', 'account_revision_at_freeze', 'account_revision', 'account_snapshot_hash',
          'buy_budget_fen', 'provenance_mode', 'decision_ref', 'task_id', 'parent_task_id')
_PRODUCTION = Path(__file__).resolve().parents[3]


def plan_header(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('document'), dict):
        raise LedgerError('INTEGRITY_ERROR', '冻结计划缺少不可变原件')
    value = {key: plan[key] for key in FIELDS if key in plan}
    value['storage_format'] = FORMAT
    value['header_hash'] = digest(value)
    if len(canonical(value).encode()) > 1024 * 1024:
        raise LedgerError('INTEGRITY_ERROR', '冻结计划索引超出有界大小')
    return value


def _header_identity(row):
    value = {key: item for key, item in row.items()
             if key not in {'id', 'original_record_ref', 'header_hash'}}
    if (row.get('storage_format') != FORMAT or set(value) - {*FIELDS, 'storage_format'}
            or row.get('id', row.get('plan_id')) != row.get('plan_id')
            or digest(value) != row.get('header_hash')):
        raise LedgerError('INTEGRITY_ERROR', '冻结计划索引身份或摘要无效')
    return digest({**value, 'header_hash': row['header_hash']})


def verified_header_cached(root, row):
    """A self-hash alone never authorizes a no-expansion execution status read."""
    return cached_validation(root, 'frozen_plan_header', _header_identity(row))


def resolve_plan(root, row):
    """Only explicit consumers expand one plan; generic transaction reads stay small."""
    if row is None or 'storage_format' not in row:
        return row
    if row.get('storage_format') != FORMAT:
        raise LedgerError('INTEGRITY_ERROR', '冻结计划索引存储格式不支持')
    body, verified = read_business_document(root, row['document'])
    result = {**body, 'document': row['document']}
    expected = plan_header(result)
    if any(row.get(key) != value for key, value in expected.items()) or any(
            key not in {*expected, 'id', 'original_record_ref'} for key in row):
        raise LedgerError('INTEGRITY_ERROR', '冻结计划索引与原件不一致')
    if row.get('id', result['plan_id']) != result['plan_id']:
        raise LedgerError('INTEGRITY_ERROR', '冻结计划登记身份不一致')
    if 'id' in row:
        result['id'] = row['id']
    remember_validation(root, 'frozen_plan_header', _header_identity(row), [
        verified, *verified.get('storage_dependencies', [])],
        details={'resolved_plan_hash': digest(result)})
    return result


def compact_strategy_indexes(root, *, database=None, apply=False, expected_plan=None):
    """Explicit isolated schema4 substep; never rerun financial-row compaction or touch production."""
    from china_stock_sim.ledger.schema import create_storage_indexes
    from china_stock_sim.ledger.strategy_decisions import (
        compact_decision_index,
        verify_decision_record,
    )

    root = Path(root).resolve()
    path = Path(database).resolve() if database else root / 'data/state/ledger.sqlite3'
    if root == _PRODUCTION or not path.is_relative_to(root) or not path.is_file():
        raise LedgerError('INVALID_INPUT', '策略索引压缩只允许已隔离的schema4目录')

    def inspect(db):
        if db.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise LedgerError('INVALID_INPUT', '须先完成schema4主体隔离迁移')
        changes, summary = [], []
        for table in ('plans', 'strategy_decisions'):
            for saved in db.execute(f'SELECT id,account_id,data FROM {table} ORDER BY account_id,id'):
                raw = saved['data'].encode()
                if len(raw) > 128 * 1024 * 1024:
                    raise LedgerError('INTEGRITY_ERROR', '策略原记录超出128MiB边界')
                row = {**json.loads(raw), 'id': saved['id'], 'account_id': saved['account_id']}
                if table == 'plans':
                    if row.get('storage_format') == FORMAT:
                        resolve_plan(root, row)
                        continue
                    original, _ = read_business_document(root, row['document'])
                    if canonical(original) != canonical({k: v for k, v in row.items()
                                                        if k not in {'id', 'document'}}):
                        raise LedgerError('INTEGRITY_ERROR', '冻结计划原件与旧登记不一致')
                    projected = plan_header(row)
                else:
                    verified = verify_decision_record(root, row)
                    projected = compact_decision_index(verified)
                    if projected == row:
                        continue
                projected['id'] = saved['id']
                summary.append({'table': table, 'id': saved['id'], 'account_id': saved['account_id'],
                    'original_sha256': hashlib.sha256(raw).hexdigest(), 'original_bytes': len(raw),
                    'projected_hash': digest(projected)})
                changes.append((table, saved['id'], saved['account_id'], raw, projected))
        return changes, summary

    if not apply:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            _, rows = inspect(db)
        return {'state': 'PLANNED' if rows else 'UNCHANGED', 'plan_id': digest(rows),
                'changes': rows, 'changed': False}
    with ledger_lock(root), sqlite3.connect(path, timeout=0) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        changes, rows = inspect(db)
        if changes and expected_plan != digest(rows):
            raise LedgerError('CONFLICT', '策略索引压缩计划摘要已变化')
        for table, identity, account, raw, projected in changes:
            projected['original_record_ref'] = put_blob(root, raw)
            db.execute(f'UPDATE {table} SET data=? WHERE account_id=? AND id=?',
                       (canonical(projected), account, identity))
        if changes:
            create_storage_indexes(db)
            db.commit()
        else:
            db.rollback()
    return {'state': 'APPLIED' if rows else 'UNCHANGED', 'plan_id': digest(rows),
            'changes': rows, 'changed': bool(rows), 'financial_rows_changed': False}
