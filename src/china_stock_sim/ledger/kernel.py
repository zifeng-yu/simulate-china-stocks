"""Local deterministic transaction boundary. No model, collector or renderer imports."""

import fcntl
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from china_stock_sim.config import local_path
from china_stock_sim.ledger.schema import TABLES, check_schema, validate_accounts
from china_stock_sim.ledger.store import readonly_connection
from china_stock_sim.market.contracts import aware_time, session_date


class LedgerError(ValueError):
    def __init__(self, state, reason, **details):
        super().__init__(reason)
        self.state, self.reason, self.details = state, reason, details

    def as_dict(self):
        return {'state': self.state, 'reason': self.reason, **self.details}


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, float):
        raise LedgerError('INVALID_INPUT', '账务数据不接受float')
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerError('INVALID_INPUT', '账务数值必须有限')
        return format(value, 'f')
    if isinstance(value, datetime):
        return aware_time(value).astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise LedgerError('INVALID_INPUT', '字段名必须是字符串')
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise LedgerError('INVALID_INPUT', f'不支持的账务类型：{type(value).__name__}')


def canonical(value, *, max_bytes=None):
    if max_bytes is not None:
        # Reject oversized input before recursively normalizing/copying it.
        # This lower bound cannot accept an oversize serialized record: exact
        # encoded size is still checked below. It bounds the intermediate tree.
        budget = [max_bytes]
        active = set()

        def measure(item):
            if isinstance(item, (dict, list, tuple)) or (is_dataclass(item) and not isinstance(item, type)):
                identity = id(item)
                if identity in active:
                    raise LedgerError('INVALID_INPUT', '账务数据不能包含循环引用')
                active.add(identity)
                budget[0] -= 2
                if is_dataclass(item) and not isinstance(item, type):
                    for field in fields(item):
                        budget[0] -= len(field.name) + 3
                        measure(getattr(item, field.name))
                elif isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise LedgerError('INVALID_INPUT', '字段名必须是字符串')
                        budget[0] -= len(key) + 3
                        measure(child)
                else:
                    for index, child in enumerate(item):
                        budget[0] -= int(index > 0)
                        measure(child)
                active.remove(identity)
            elif isinstance(item, str):
                budget[0] -= len(item) + 2
            else:
                budget[0] -= 1
            if budget[0] < 0:
                raise LedgerError('RECORD_TOO_LARGE', '账务记录超过有界存储上限', limit_bytes=max_bytes)

        measure(value)
    normalized = _json_value(value)
    if max_bytes is None:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    chunks, size = [], 0
    for chunk in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':')).iterencode(normalized):
        size += len(chunk.encode('utf-8'))
        if size > max_bytes:
            raise LedgerError('RECORD_TOO_LARGE', '账务记录超过有界存储上限', limit_bytes=max_bytes)
        chunks.append(chunk)
    return ''.join(chunks)


def reject_credentials(value):
    """Reject credential-bearing input before a write; never echo the rejected content."""
    # Validate once in place. Re-normalizing each subtree used to copy the same
    # execution evidence once per ancestor and amplify garbage collection.
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerError('INVALID_INPUT', '字段名必须是字符串')
            normalized = re.sub(r'[-_\s]', '', key).lower()
            if normalized in {'apikey', 'authorization', 'password'}:
                raise LedgerError('INVALID_INPUT', '账本输入禁止包含凭据字段')
            reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_credentials(item)
    elif isinstance(value, str):
        if re.search(r'(sk-[A-Za-z0-9_.-]{12,}|Bearer\s+\S+)', value, re.I):
            raise LedgerError('INVALID_INPUT', '账本输入禁止包含密钥或授权头')
        if re.search(r'(?:api[-_ ]?key|authorization|password)\s*[\\"\']?\s*[:=]', value, re.I):
            raise LedgerError('INVALID_INPUT', '账本文本禁止包含凭据赋值')
    else:
        _json_value(value)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value, label='ID'):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise LedgerError('INVALID_INPUT', f'{label}必须为1到256字非空字符串')
    return value


@contextmanager
def ledger_lock(root):
    path = local_path(root, 'data/state/ledger-write.lock')
    # Writes may create a lock; read operations never call this function.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LedgerError('CONFLICT', '已有进程在写入账本') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Transaction:
    def __init__(self, db, account_id, now, operation_id=None):
        self.db, self.account_id, self.now = db, account_id, now
        self.operation_id = operation_id
        row = db.execute('SELECT * FROM accounts WHERE account_id=?', (account_id,)).fetchone()
        if row is None:
            raise LedgerError('INVALID_INPUT', '未知账户', account_id=account_id)
        self.account = dict(row)
        state = db.execute('SELECT data FROM account_state WHERE account_id=?', (account_id,)).fetchone()
        if state is None:
            raise LedgerError('INTEGRITY_ERROR', '缺少账户运行状态')
        self.state = json.loads(state['data'])

    def _table(self, table):
        if table not in TABLES and table != 'cash_entries':
            raise LedgerError('INVALID_INPUT', '未知账务记录类型', table=table)
        return table

    def get(self, table, identity):
        table = self._table(table)
        key = 'entry_id' if table == 'cash_entries' else 'id'
        row = self.db.execute(f'SELECT * FROM {table} WHERE account_id=? AND {key}=?',
                              (self.account_id, identity)).fetchone()
        return self._decode(table, row) if row else None

    def _decode(self, table, row):
        if table == 'cash_entries':
            result = dict(row)
            result['details'] = json.loads(result['details'])
            result['id'] = result['entry_id']
            return result
        data = json.loads(row['data'])
        return {**data, 'id': row['id'], 'account_id': row['account_id']}

    def list(self, table, **filters):
        table = self._table(table)
        where, parameters = self._where(table, equals=filters)
        rows = self.db.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY rowid',
                               parameters).fetchall()
        results = [self._decode(table, row) for row in rows]
        return results

    def _field(self, table, field):
        if not isinstance(field, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field):
            raise LedgerError('INVALID_INPUT', '查询字段非法')
        if field in {'account_id', 'rowid'}:
            return field
        if field == 'id':
            return 'entry_id' if table == 'cash_entries' else 'id'
        if table == 'cash_entries':
            allowed = {'entry_id', 'kind', 'amount_fen', 'recorded_at', 'effective_date',
                       'reference_id', 'details'}
            if field not in allowed:
                raise LedgerError('INVALID_INPUT', '现金查询字段非法')
            return field
        return f"json_extract(data,'$.{field}')"

    def _where(self, table, *, equals=None, in_values=None, ranges=None):
        clauses, parameters = ['account_id=?'], [self.account_id]
        for field, value in (equals or {}).items():
            expression = self._field(table, field)
            clauses.append(f'{expression} IS ?')
            parameters.append(value)
        for field, values in (in_values or {}).items():
            if not isinstance(values, (tuple, list, set)):
                raise LedgerError('INVALID_INPUT', '查询集合非法')
            values = list(values)
            clauses.append(f'{self._field(table, field)} IN ({",".join("?" for _ in values)})'
                           if values else '0')
            parameters.extend(values)
        for field, bounds in (ranges or {}).items():
            for operator, value in bounds.items():
                symbol = {'gte': '>=', 'lte': '<=', 'gt': '>', 'lt': '<'}.get(operator)
                if symbol is None:
                    raise LedgerError('INVALID_INPUT', '查询范围非法')
                clauses.append(f'{self._field(table, field)} {symbol} ?')
                parameters.append(value)
        return ' AND '.join(clauses), parameters

    def select(self, table, *, fields, equals=None, in_values=None, ranges=None,
               order_by=None, limit=None):
        """Explicit bounded projection; never decode excluded evidence columns."""
        table = self._table(table)
        if not fields or not isinstance(fields, (tuple, list)):
            raise LedgerError('INVALID_INPUT', '查询投影须为字段列表')
        selected = ','.join(f'{self._field(table, field)} AS "{field}"' for field in fields)
        where, parameters = self._where(table, equals=equals, in_values=in_values, ranges=ranges)
        ordering = ','.join(self._field(table, f) for f in (order_by or ('rowid',)))
        sql = f'SELECT {selected} FROM {table} WHERE {where} ORDER BY {ordering}'
        if limit is not None:
            if type(limit) is not int or limit < 0:
                raise LedgerError('INVALID_INPUT', '查询条数非法')
            sql += ' LIMIT ?'
            parameters.append(limit)
        rows = [dict(row) for row in self.db.execute(sql, parameters)]
        # SQLite JSON projections return arrays/objects as text. Scalar strings
        # stay strings; only declared JSON-valued financial columns are decoded.
        nested = {'data_evidence', 'snapshot_ids', 'market_snapshot_ids', 'positions',
                  'issues', 'order_states', 'payments', 'assessments', 'actions', 'event_ids',
                  'details', 'orders', 'fills', 'lot_allocations'}
        for row in rows:
            for key in nested.intersection(row):
                if isinstance(row[key], str) and row[key][:1] in '[{':
                    row[key] = json.loads(row[key])
        return rows

    def _data(self, identity, data):
        identifier(identity)
        reject_credentials(data)
        if not isinstance(data, dict):
            raise LedgerError('INVALID_INPUT', '记录必须为对象')
        if data.get('account_id', self.account_id) != self.account_id:
            raise LedgerError('INVALID_INPUT', '禁止跨账户记录')
        if data.get('id', identity) != identity:
            raise LedgerError('INVALID_INPUT', '记录身份不匹配')
        return canonical({**data, 'id': identity, 'account_id': self.account_id},
                         max_bytes=128 * 1024 * 1024 if 'plan_hash' in data and
                         'validation_report' in data else 16 * 1024 * 1024)

    def save(self, table, identity, data):
        table = self._table(table)
        if table == 'cash_entries':
            raise LedgerError('INVALID_INPUT', '现金流水必须通过cash追加')
        value = self._data(identity, data)
        if table in {'plans', 'fills', 'lot_movements', 'daily_nav', 'daily_positions', 'corrections'}:
            prior = self.get(table, identity)
            if prior is not None and canonical(prior) != value:
                raise LedgerError('CONFLICT', '不可覆盖已确认记录；请追加更正或修订', table=table)
        self.db.execute(f'''INSERT INTO {table}(id,account_id,data) VALUES (?,?,?)
            ON CONFLICT(account_id,id) DO UPDATE SET data=excluded.data''',
                        (identity, self.account_id, value))

    def append(self, table, identity, data):
        table = self._table(table)
        if table == 'cash_entries':
            raise LedgerError('INVALID_INPUT', '现金流水必须通过cash追加')
        value = self._data(identity, data)
        self.db.execute(f'INSERT INTO {table}(id,account_id,data) VALUES (?,?,?)',
                        (identity, self.account_id, value))

    def cash(self, entry_id, kind, amount_fen, effective_date, reference_id=None, details=None):
        reject_credentials({'entry_id': entry_id, 'kind': kind, 'reference_id': reference_id, 'details': details})
        identifier(entry_id, '流水ID')
        if kind == 'INITIAL_CAPITAL':
            raise LedgerError('INVALID_INPUT', '业务接口不能追加本金')
        identifier(kind, '流水类型')
        if type(amount_fen) is not int or amount_fen == 0:
            raise LedgerError('INVALID_INPUT', '现金流水必须为非零整数分')
        day = session_date(effective_date)
        self.db.execute('''INSERT INTO cash_entries
            (entry_id,account_id,kind,amount_fen,recorded_at,effective_date,reference_id,details)
            VALUES (?,?,?,?,?,?,?,?)''',
            (entry_id, self.account_id, kind, amount_fen, self.now, day,
             reference_id, canonical(details or {})))

    def balance(self):
        return self.db.execute('SELECT COALESCE(SUM(amount_fen),0) FROM cash_entries WHERE account_id=?',
                               (self.account_id,)).fetchone()[0]


class LedgerKernel:
    def __init__(self, settings, market=None, clock=None, test_mode=False):
        self.settings = settings
        self.root = settings.root
        self.market = market
        actual = Path(__file__).resolve().parents[3]
        if clock is not None and (not test_mode or settings.database.resolve() == actual / 'data/state/ledger.sqlite3'):
            raise ValueError('测试时钟只能在明确隔离的测试账本使用')
        self.test_mode = test_mode
        self._clock = clock

    def _now(self):
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        return aware_time(now).astimezone(timezone.utc)

    def _read(self, account_id, callback):
        with closing(readonly_connection(self.settings.database)) as db:
            db.execute('BEGIN')
            check_schema(db, full=False)
            validate_accounts(db, self.settings)
            return callback(Transaction(db, account_id, self._now().isoformat()))

    def _mutate(self, account_id, operation_id, payload, expected_revision, effective_date,
                callback, financial=True, report=True):
        try:
            reject_credentials({'operation_id': operation_id, 'payload': payload})
            identifier(operation_id, '操作ID')
            session_date(effective_date)
            # Do not include retry revision or generated transaction timestamps in the business hash.
            business = {key: value for key, value in payload.items()
                        if key not in {'expected_account_revision', 'expected_revision',
                                       'processed_at', 'commit_id', 'account_revision',
                                       'report_revision', 'frozen_at'}}
            payload_hash = digest({'input': business, 'effective_date': effective_date})
            if not self.settings.database.is_file():
                raise LedgerError('INTEGRITY_ERROR', '账本尚未初始化')
            with ledger_lock(self.root), closing(sqlite3.connect(self.settings.database, timeout=0)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                try:
                    check_schema(db, full=False)
                    validate_accounts(db, self.settings)
                    old = db.execute('SELECT * FROM operations WHERE account_id=? AND operation_id=?',
                                     (account_id, operation_id)).fetchone()
                    if old:
                        if old['payload_hash'] != payload_hash:
                            raise LedgerError('CONFLICT', '同一操作ID对应不同业务内容')
                        saved = json.loads(old['result'])
                        db.rollback()
                        return {**saved, 'original_state': saved['state'], 'state': 'ALREADY_APPLIED'}
                    tx = Transaction(db, account_id, self._now().isoformat(), operation_id)
                    revision = tx.state['account_revision']
                    report_revision = tx.state['report_revision']
                    if type(expected_revision) is not int or expected_revision != revision:
                        raise LedgerError('STALE_ACCOUNT', '账户修订号已变化', account_revision=revision)
                    prior_time = tx.state.get('last_processed_at')
                    if prior_time and aware_time(tx.now) < aware_time(prior_time):
                        raise LedgerError('INTEGRITY_ERROR', '系统时钟早于已保存时点，暂停新写入')
                    value = callback(tx) or {}
                    if not isinstance(value, dict):
                        raise LedgerError('INVALID_INPUT', '操作结果必须为对象')
                    reject_credentials(value)
                    reject_credentials(tx.state)
                    state = value.get('state', 'APPLIED')
                    if state == 'ALREADY_APPLIED':
                        db.rollback()
                        return {**value, 'account_revision': revision,
                                'report_revision': report_revision}
                    if state in {'CONFLICT', 'INVALID_INPUT', 'STALE_ACCOUNT', 'INTEGRITY_ERROR', 'ERROR'}:
                        raise LedgerError(state, value.get('reason', state))
                    for table, fields in (
                            ('position_lots', ('quantity', 'cost_fen')),
                            ('reservations', ('cash_fen', 'quantity', 'shares')),
                            ('tax_items', ('amount_fen', 'assessed_fen', 'paid_fen', 'outstanding_fen'))):
                        for row in tx.list(table):
                            for field in fields:
                                amount = row.get(field)
                                if amount is not None and (type(amount) is not int or amount < 0):
                                    raise LedgerError('INTEGRITY_ERROR', '股数、成本、预留或税款不变量失败',
                                                      table=table, record_id=row['id'], field=field)
                    if tx.balance() < 0:
                        raise LedgerError('CONFLICT', '确认现金不足，拒绝形成负余额')
                    tx.state['account_revision'] = revision + int(financial)
                    tx.state['report_revision'] = report_revision + int(report)
                    tx.state['last_processed_at'] = tx.now
                    result = {**value, 'state': state, 'operation_id': operation_id,
                              'commit_id': uuid4().hex, 'payload_hash': payload_hash,
                              'account_revision': tx.state['account_revision'],
                              'report_revision': tx.state['report_revision'], 'processed_at': tx.now}
                    db.execute('UPDATE account_state SET data=? WHERE account_id=?',
                               (canonical(tx.state), account_id))
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                               (account_id, operation_id, payload_hash,
                                canonical(result, max_bytes=16 * 1024 * 1024), tx.now))
                    db.commit()
                    return result
                except Exception as exc:
                    db.rollback()
                    from china_stock_sim.runtime.recording import record_exception
                    record_exception(exc, account_id=account_id, operation_id=operation_id)
                    raise
        except LedgerError as exc:
            return exc.as_dict()
        except (ValueError, TypeError, KeyError) as exc:
            return {'state': 'INVALID_INPUT', 'reason': str(exc)}
        except (OSError, sqlite3.Error) as exc:
            return {'state': 'INTEGRITY_ERROR', 'reason': str(exc)}

    def initialize(self):
        from china_stock_sim.ledger.store import initialize
        return initialize(self.settings, recorded_at=self._now().isoformat())
