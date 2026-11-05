"""Bounded, read-only operational facts; never expand financial audit documents."""

import sqlite3
from contextlib import closing
from datetime import date

MAX_ACCOUNTS = 100
MAX_SESSIONS = 12
STATE_FIELDS = (
    'simulation_start_session', 'account_revision', 'report_revision',
    'last_rendered_revision', 'settled_through', 'valued_through',
    'restate_from', 'restate_through', 'last_processed_at',
)


def _days(sessions):
    values = list(dict.fromkeys(sessions))
    if len(values) > MAX_SESSIONS:
        raise ValueError('监控日期范围过大')
    if any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
           for day in values):
        raise ValueError('监控日期无效')
    return values


def _fields(fields, *, prefix='data'):
    integers = {'account_revision', 'report_revision', 'last_rendered_revision', 'statement_revision'}
    return ','.join((f"json_extract({prefix},'$.{field}')" if field in integers else
                     f"substr(json_extract({prefix},'$.{field}'),1,256)") + f' AS {field}'
                    for field in fields)


def _account(db, account, days):
    """One short read snapshot per account; a malformed peer does not hide it."""
    identity = account['account_id']
    db.execute('BEGIN')
    try:
        row = db.execute(f'SELECT {_fields(STATE_FIELDS)} FROM account_state WHERE account_id=?',
                         (identity,)).fetchone()
        if row is None or any(type(row[key]) is not int or row[key] < 0 for key in (
                'account_revision', 'report_revision', 'last_rendered_revision')):
            raise ValueError('账户状态无效')
        if row['last_rendered_revision'] > row['report_revision']:
            raise ValueError('发布修订无效')
        _days([row[key] for key in ('simulation_start_session', 'settled_through',
              'valued_through', 'restate_from', 'restate_through') if row[key] is not None])
        result = {**account, **dict(row), 'state': 'READY', 'sessions': {}}
        for day in days:
            name = db.execute('''SELECT substr(json_extract(data,'$.display_name'),1,256) FROM account_policies
                WHERE account_id=? AND (json_extract(data,'$.effective_session') IS NULL
                OR json_extract(data,'$.effective_session')<=?)
                ORDER BY json_extract(data,'$.policy_revision') DESC LIMIT 1''', (identity, day)).fetchone()
            plan_fields = ('plan_id', 'target_session', 'status', 'frozen_at')
            plan = db.execute(f'''SELECT {_fields(plan_fields)},
                json_array_length(data,'$.order_ids') AS order_count FROM plans
                WHERE account_id=? AND json_extract(data,'$.target_session')=? LIMIT 1''',
                (identity, day)).fetchone()
            nav_fields = ('session', 'statement_revision', 'accounting_status', 'valuation_status',
                          'processed_at', 'account_revision')
            statement = db.execute(f'''SELECT {_fields(nav_fields)} FROM daily_nav
                WHERE account_id=? AND json_extract(data,'$.session')=?
                ORDER BY json_extract(data,'$.statement_revision') DESC LIMIT 1''',
                (identity, day)).fetchone()
            counts = db.execute('''SELECT COUNT(*) AS order_count,
                COALESCE(SUM(json_extract(data,'$.status') IN ('PENDING','WAITING_DATA')),0)
                AS pending_order_count FROM orders WHERE account_id=?
                AND json_extract(data,'$.target_session')=?''', (identity, day)).fetchone()
            no_plan = db.execute('''SELECT substr(json_extract(data,'$.status'),1,96) FROM job_runs
                WHERE account_id=? AND json_extract(data,'$.kind')='NO_PLAN'
                AND json_extract(data,'$.target_session')=? LIMIT 1''', (identity, day)).fetchone()
            value = {'plan': dict(plan) if plan else None,
                     'statement': dict(statement) if statement else None, **dict(counts),
                     'display_name': str(name[0])[:256] if name and name[0] else identity,
                     'no_plan_reason': str(no_plan[0])[:96] if no_plan and no_plan[0] else None}
            if value['statement']:
                value['statement']['requires_restatement'] = bool(
                    row['restate_from'] and day >= row['restate_from'])
            result['sessions'][day] = value
        return result
    finally:
        db.rollback()


def read_runtime_facts(settings, sessions):
    """Read only bounded SQL scalar projections, without init/schema/audit repair.

    The requested dates are explicit calendar-derived ISO sessions. This is a
    monitoring view of durable rows, not a substitute for frozen-evidence audit.
    """
    days = _days(sessions)
    result = {'state': 'UNKNOWN', 'schema_version': None, 'sessions': days,
              'accounts': [], 'accounts_truncated': False, 'issues': []}
    path = settings.database
    if not path.is_file():
        return {**result, 'state': 'MISSING', 'issues': [{'kind': 'LEDGER_NOT_INITIALIZED'}]}
    if path.is_symlink() or not path.resolve().is_relative_to(settings.root.resolve()):
        return {**result, 'issues': [{'kind': 'LEDGER_PATH_UNAVAILABLE'}]}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                     timeout=0.2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            result['schema_version'] = version
            if version not in {3, 4}:
                return {**result, 'issues': [{'kind': 'LEDGER_SCHEMA_UNSUPPORTED'}]}
            rows = db.execute('SELECT account_id,strategy_id FROM accounts ORDER BY account_id LIMIT ?',
                              (MAX_ACCOUNTS + 1,)).fetchall()
            result['accounts_truncated'] = len(rows) > MAX_ACCOUNTS
            for row in rows[:MAX_ACCOUNTS]:
                account = dict(row)
                try:
                    value = _account(db, account, days)
                except (sqlite3.Error, ValueError, TypeError, KeyError):
                    value = {**account, 'state': 'UNKNOWN', 'sessions': {},
                             'reason': 'ACCOUNT_FACTS_UNAVAILABLE'}
                result['accounts'].append(value)
            result['state'] = ('PARTIAL' if result['accounts_truncated'] or any(
                row['state'] != 'READY' for row in result['accounts']) else 'READY')
            if result['accounts_truncated']:
                result['issues'].append({'kind': 'ACCOUNT_LIST_TRUNCATED'})
            return result
    except (sqlite3.Error, OSError, ValueError):
        return {**result, 'state': 'UNKNOWN', 'issues': [{'kind': 'LEDGER_UNAVAILABLE'}]}
