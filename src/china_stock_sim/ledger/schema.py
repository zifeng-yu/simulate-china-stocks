"""Single current SQLite ledger layout and explicit migration from initial accounts."""

import json
import sqlite3
from datetime import date, datetime

from china_stock_sim.config import MAX_CASH_FEN, valid_account_label

SCHEMA_VERSION = 4
SCHEMA2_TABLES = (
    'plans', 'orders', 'reservations', 'fills', 'position_lots', 'lot_movements',
    'corporate_action_cases', 'entitlements', 'tax_items', 'daily_nav', 'daily_positions',
    'job_runs', 'model_attempts', 'rebalance_tasks', 'corrections',
)
STRATEGY_TABLES = ('account_policies', 'strategy_decisions')
TABLES = SCHEMA2_TABLES + STRATEGY_TABLES


def create_storage_indexes(connection):
    """Explicit write-time indexes for bounded current/recent queries; readers never create them."""
    for table, suffix, fields in (
        ('orders', 'session', ('target_session',)),
        ('orders', 'status_session', ('status', 'target_session')),
        ('fills', 'session', ('target_session',)),
        ('lot_movements', 'session', ('effective_date',)),
        ('job_runs', 'kind_session', ('kind', 'target_session')),
        ('strategy_decisions', 'session_state', ('target_session', 'state')),
    ):
        expressions = ','.join(f"json_extract(data,'$.{field}')" for field in fields)
        connection.execute(f'CREATE INDEX IF NOT EXISTS storage_{table}_{suffix} ON {table}(account_id,{expressions})')
    connection.execute('CREATE INDEX IF NOT EXISTS storage_cash_session ON cash_entries(account_id,effective_date)')


def tables_for_version(version):
    """JSON record tables that actually exist in the requested source layout."""
    if version == 1:
        return ()
    if version == 2:
        return SCHEMA2_TABLES
    if version in (3, SCHEMA_VERSION):
        return TABLES
    raise ValueError(f'不支持的账本版本 {version}')


def create_strategy_schema(connection):
    """Add schema3 storage inside the caller's transaction; do not advance its version."""
    for table in STRATEGY_TABLES:
        connection.execute(f'''CREATE TABLE {table} (
            id TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(account_id),
            data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(account_id,id))''')
    connection.execute('''CREATE UNIQUE INDEX one_base_account_policy ON account_policies(account_id)
        WHERE json_extract(data,'$.effective_session') IS NULL''')
    for table, name, fields, condition in (
        ('account_policies', 'one_account_policy_revision', ['policy_revision'], '1'),
        ('account_policies', 'one_account_policy_session', ['effective_session'],
         "json_extract(data,'$.effective_session') IS NOT NULL"),
        ('strategy_decisions', 'one_strategy_decision_session', ['target_session'], '1'),
    ):
        expressions = ','.join(f"json_extract(data,'$.{field}')" for field in fields)
        connection.execute(f'CREATE UNIQUE INDEX {name} ON {table}(account_id,{expressions}) WHERE {condition}')


def initial_state():
    return {'account_revision': 0, 'report_revision': 0, 'last_rendered_revision': 0,
            'simulation_start_session': None, 'settled_through': None,
            'valued_through': None, 'last_processed_at': None}


def _valid_timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _valid_session(value):
    try:
        return value is None or isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def create_schema(connection, *, migrate=False, advance_version=True):
    if not migrate:
        connection.execute('''CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
            currency TEXT NOT NULL CHECK(currency='CNY'),
            initial_cash_fen INTEGER NOT NULL CHECK(initial_cash_fen>0),
            created_at TEXT NOT NULL)''')
    else:
        connection.execute('ALTER TABLE cash_entries RENAME TO opening_cash_entries')
    connection.execute('''CREATE TABLE cash_entries (
        entry_id TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(account_id),
        kind TEXT NOT NULL, amount_fen INTEGER NOT NULL,
        recorded_at TEXT NOT NULL, effective_date TEXT, reference_id TEXT,
        details TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(account_id,entry_id))''')
    connection.execute('''CREATE UNIQUE INDEX one_initial_capital ON cash_entries(account_id)
        WHERE kind='INITIAL_CAPITAL' ''')
    connection.execute('''CREATE INDEX cash_date ON cash_entries(account_id,effective_date)''')
    if migrate:
        connection.execute('''INSERT INTO cash_entries
            (entry_id,account_id,kind,amount_fen,recorded_at)
            SELECT entry_id,account_id,kind,amount_fen,recorded_at FROM opening_cash_entries''')
        connection.execute('DROP TABLE opening_cash_entries')
    connection.execute('''CREATE TABLE account_state (
        account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
        data TEXT NOT NULL CHECK(json_valid(data)))''')
    for table in SCHEMA2_TABLES:
        connection.execute(f'''CREATE TABLE {table} (
            id TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(account_id),
            data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(account_id,id))''')
    connection.execute('''CREATE TABLE operations (
        account_id TEXT NOT NULL REFERENCES accounts(account_id), operation_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, result TEXT NOT NULL CHECK(json_valid(result)),
        processed_at TEXT NOT NULL, PRIMARY KEY(account_id,operation_id))''')
    for table, name, fields, condition in (
        ('plans', 'one_plan_per_session', ['target_session'], "1"),
        ('daily_nav', 'one_statement_revision', ['session', 'statement_revision'], '1'),
        ('orders', 'one_symbol_per_plan', ['plan_id', 'code'], '1'),
        ('fills', 'one_fill_per_order', ['order_id'], '1'),
        ('model_attempts', 'one_attempt_number', ['target_session', 'attempt_number'], '1'),
    ):
        expressions = ','.join(f"json_extract(data,'$.{field}')" for field in fields)
        connection.execute(f'CREATE UNIQUE INDEX {name} ON {table}(account_id,{expressions}) WHERE {condition}')
    if migrate:
        for (account_id,) in connection.execute('SELECT account_id FROM accounts'):
            connection.execute('INSERT INTO account_state VALUES (?,?)',
                               (account_id, json.dumps(initial_state())))
    create_strategy_schema(connection)
    create_storage_indexes(connection)
    if advance_version:
        connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')


def check_schema(connection, *, legacy=False, full=True):
    """Check shape on ordinary access; explicitly retain full checks at audit boundaries.

    The default is deliberately the historical complete check. Callers opting
    into ``full=False`` still validate the version and required tables; normal
    writes also retain SQLite's enabled foreign-key/constraint enforcement.
    """
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version == 0:
        raise ValueError('发现未识别的数据库结构，拒绝覆盖')
    if version != SCHEMA_VERSION and not (legacy and version in (1, 2, 3)):
        raise ValueError(f'不支持的账本版本 {version}，请先执行显式迁移')
    expected = {'accounts', 'cash_entries'}
    if version >= 2:
        expected.update(('account_state', 'operations', *tables_for_version(version)))
    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not expected <= present:
        raise ValueError('账本结构缺少必要表，请先核查备份或显式迁移')
    if full and connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite 完整性检查失败')
    if full and connection.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('SQLite 账户引用完整性检查失败')
    return version


def validate_accounts(connection, settings):
    # Configuration is a seed for a new database, never an authority over an
    # existing account's identity, principal, or membership.
    rows = connection.execute('SELECT * FROM accounts ORDER BY account_id').fetchall()
    if not rows:
        raise ValueError('账本中没有已登记账户')
    for row in rows:
        identity, principal = row['account_id'], row['initial_cash_fen']
        entries = connection.execute("SELECT * FROM cash_entries WHERE account_id=? AND kind='INITIAL_CAPITAL'", (identity,)).fetchall()
        if (not valid_account_label(identity) or not valid_account_label(row['strategy_id'])
                or row['currency'] != 'CNY' or type(principal) is not int
                or not 0 < principal <= MAX_CASH_FEN or len(entries) != 1
                or type(entries[0]['amount_fen']) is not int or entries[0]['amount_fen'] != principal
                or not _valid_timestamp(row['created_at'])
                or entries[0]['recorded_at'] != row['created_at']):
            raise ValueError('初始账户或流水不一致，拒绝覆盖')
        amounts = [entry[0] for entry in connection.execute(
            'SELECT amount_fen FROM cash_entries WHERE account_id=?', (identity,))]
        if any(type(amount) is not int for amount in amounts) or not 0 <= sum(amounts) <= MAX_CASH_FEN:
            raise ValueError('账户确认现金不是合法的非负整数分')
    if connection.execute('PRAGMA user_version').fetchone()[0] >= 2:
        states = {row['account_id']: json.loads(row['data'])
                  for row in connection.execute('SELECT * FROM account_state')}
        if set(states) != {row['account_id'] for row in rows}:
            raise ValueError('账户运行状态缺失')
        for state in states.values():
            if (not isinstance(state, dict) or not initial_state().keys() <= state.keys()
                    or any(type(state[key]) is not int or state[key] < 0 for key in (
                        'account_revision', 'report_revision', 'last_rendered_revision'))
                    or state['last_rendered_revision'] > state['report_revision']
                    or any(not _valid_session(state[key]) for key in (
                        'simulation_start_session', 'settled_through', 'valued_through'))
                    or state['last_processed_at'] is not None and not _valid_timestamp(state['last_processed_at'])
                    or set(state).intersection({'account_id', 'strategy_id', 'currency',
                        'initial_cash_fen', 'created_at', 'cash_fen', 'opening_entries'})):
                raise ValueError('账户运行状态无效')
    return rows


def connection(path, *, readonly=False):
    if readonly and not path.is_file():
        raise FileNotFoundError(f'账本尚未初始化：{path}')
    db = sqlite3.connect(path.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, timeout=0)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return db
