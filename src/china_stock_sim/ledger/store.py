"""Account initialization and read-only compatibility; migration is always explicit."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime

from china_stock_sim.ledger.schema import (
    SCHEMA_VERSION,
    check_schema,
    connection,
    create_schema,
    initial_state,
    validate_accounts,
)


def readonly_connection(path):
    return connection(path, readonly=True)


def _check_schema(db):
    return check_schema(db, legacy=True, full=False)


def initialize(settings, *, recorded_at=None):
    # Existing ledgers are validated before any writes. A legacy file must be explicitly migrated.
    if settings.database.is_file():
        read_snapshot(settings)
        return {'created_accounts': 0, 'database': str(settings.database)}
    if recorded_at is not None:
        try:
            if not isinstance(recorded_at, str) or datetime.fromisoformat(recorded_at).utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError('初始政策记录时间须为带时区时间') from None
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    from china_stock_sim.ledger.account_policies import initialize_base_policy
    from china_stock_sim.ledger.kernel import ledger_lock

    with ledger_lock(settings.root), closing(sqlite3.connect(settings.database, timeout=0)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('发现未识别的数据库结构，拒绝覆盖')
            create_schema(db, advance_version=False)
            for spec in settings.accounts:
                now = recorded_at or datetime.now(settings.timezone).isoformat()
                db.execute("INSERT INTO accounts VALUES (?,?,'CNY',?,?)",
                           (spec.id, spec.strategy, spec.initial_cash_fen, now))
                db.execute('''INSERT INTO cash_entries
                    (entry_id,account_id,kind,amount_fen,recorded_at) VALUES (?,?,'INITIAL_CAPITAL',?,?)''',
                    (f'initial:{spec.id}', spec.id, spec.initial_cash_fen, now))
                db.execute('INSERT INTO account_state VALUES (?,?)',
                           (spec.id, json.dumps(initial_state())))
                account = dict(db.execute('SELECT * FROM accounts WHERE account_id=?', (spec.id,)).fetchone())
                initialize_base_policy(db, root=settings.root, settings=settings,
                                       account=account, recorded_at=now)
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            validate_accounts(db, settings)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'created_accounts': len(settings.accounts), 'database': str(settings.database)}


def read_snapshot(settings):
    with closing(readonly_connection(settings.database)) as db:
        db.execute('BEGIN')
        version = _check_schema(db)
        validate_accounts(db, settings)
        accounts = [dict(row) for row in db.execute('''
            SELECT a.*, COALESCE(SUM(c.amount_fen),0) cash_fen,
            SUM(CASE WHEN c.kind='INITIAL_CAPITAL' THEN 1 ELSE 0 END) opening_entries
            FROM accounts a LEFT JOIN cash_entries c USING(account_id)
            GROUP BY a.account_id ORDER BY a.account_id''')]
        if version == 1:
            # An old initialization file cannot already contain financial transactions.
            if db.execute('SELECT COUNT(*) FROM cash_entries').fetchone()[0] != len(accounts):
                raise ValueError('旧初始化账本包含未知流水')
            states = {}
        else:
            states = {r['account_id']: json.loads(r['data']) for r in db.execute('SELECT * FROM account_state')}
            if set(states) != {r['account_id'] for r in accounts}:
                raise ValueError('账户运行状态缺失')
        for account in accounts:
            if states:
                account.update(states[account['account_id']])
        active = [v for v in states.values() if v['simulation_start_session']]
        days = db.execute('SELECT COUNT(DISTINCT json_extract(data,\'$.session\')) FROM daily_nav').fetchone()[0] if active else 0
        settled = [v['settled_through'] for v in active]
        return {'state': 'ACTIVE' if active else 'INITIALIZED_NOT_RUNNING',
                'schema_version': version,
                'last_settled_session': min(settled) if settled and all(settled) else None,
                'financial_observation_days': days, 'accounts': accounts}
