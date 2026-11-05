"""One bounded SQLite snapshot of the economic facts needed by the shutdown lamp.

Only previously verified immutable evidence is reused. A miss is not permission
to materialize a plan or repair a cache from this read-only observer.
"""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

from china_stock_sim.ledger.decision_evidence import cached_validation, verification_scope
from china_stock_sim.ledger.kernel import digest
from china_stock_sim.ledger.plan_storage import FORMAT, verified_header_cached

TZ = ZoneInfo('Asia/Shanghai')
MAX_OBJECT = 1024 * 1024
MAX_ORDERS = 1000
MAX_PENDING = 1000
ORDER_FIELDS = ('order_id', 'plan_id', 'code', 'side', 'quantity', 'limit_price',
                'target_session', 'order_index', 'reserved_cash_fen', 'reserved_shares', 'status')
STATE_FIELDS = ('account_revision', 'report_revision', 'last_rendered_revision',
                'simulation_start_session', 'settled_through', 'valued_through',
                'restate_from', 'restate_through')


class Unavailable(ValueError):
    pass


class Integrity(ValueError):
    pass


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise Integrity('INVALID_TIMESTAMP')
    return result.astimezone(TZ)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise Integrity('INVALID_SESSION')
    return value


def _int(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise Integrity('INVALID_NUMERIC_FACT')
    return value


def _select(db, table, account, fields, *, where='', parameters=(), limit=MAX_PENDING):
    # All expressions and names below are internal constants, never user SQL.
    sql = ','.join(f"json_extract(data,'$.{field}') AS {field}" for field in fields)
    rows = db.execute(f'SELECT id,account_id,{sql} FROM {table} WHERE account_id=? '
                      f'{where} ORDER BY rowid LIMIT ?', (account, *parameters, limit + 1)).fetchall()
    if len(rows) > limit:
        raise Unavailable('READINESS_RANGE_EXCEEDED')
    return [dict(row) for row in rows]


def _object(db, table, account, identity):
    row = db.execute(f'SELECT length(CAST(data AS BLOB)) AS size FROM {table} '
                     'WHERE account_id=? AND id=?', (account, identity)).fetchone()
    if row is None:
        raise Integrity('REFERENCED_RECORD_MISSING')
    if row['size'] > MAX_OBJECT:
        raise Unavailable('LEGACY_LARGE_INDEX')
    raw = db.execute(f'SELECT data FROM {table} WHERE account_id=? AND id=?',
                     (account, identity)).fetchone()[0]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise Integrity('INVALID_RECORD')
    return {**data, 'id': identity, 'account_id': account}


def _plan(db, root, account, strategy, day, target, now):
    rows = _select(db, 'plans', account, ('plan_id',),
                   where="AND json_extract(data,'$.target_session')=?", parameters=(target,), limit=1)
    if not rows:
        return None
    plan = _object(db, 'plans', account, rows[0]['id'])
    if plan.get('storage_format') != FORMAT:
        raise Unavailable('LEGACY_PLAN_REQUIRES_BUSINESS_CHECK')
    if not verified_header_cached(root, plan):
        raise Unavailable('PLAN_PROOF_UNAVAILABLE')
    if (plan.get('account_id') != account or plan.get('strategy_id') != strategy
            or plan.get('target_session') != target or plan.get('market_as_of') != day
            or plan.get('provenance_mode') != 'STRATEGY'
            or plan.get('status') not in {'FROZEN', 'EMPTY'}):
        raise Integrity('PLAN_IDENTITY_MISMATCH')
    frozen, cutoff = _time(plan['frozen_at']), _time(plan['input_cutoff'])
    deadline = datetime.fromisoformat(target + 'T09:00:00+08:00')
    if not datetime.fromisoformat(day + 'T18:10:00+08:00') <= cutoff <= frozen <= now or frozen >= deadline:
        raise Integrity('PLAN_TIME_INVALID')
    reference = plan['decision_ref']
    decision = _object(db, 'strategy_decisions', account, reference['decision_id'])
    if not cached_validation(root, 'strategy_decision', digest(decision)):
        raise Unavailable('DECISION_PROOF_UNAVAILABLE')
    if (decision.get('state') != 'FROZEN' or decision.get('plan_id') != plan['plan_id']
            or decision.get('plan_document') != plan['document']
            or decision.get('decision_ref') != reference or decision.get('target_session') != target
            or decision.get('market_as_of') != day or decision.get('strategy_id') != strategy):
        raise Integrity('DECISION_PLAN_MISMATCH')
    policies = _select(db, 'account_policies', account, ('policy_id', 'policy_revision', 'effective_session'),
                       where="AND (json_extract(data,'$.effective_session') IS NULL OR "
                       "json_extract(data,'$.effective_session')<=?)", parameters=(target,))
    if not policies:
        raise Integrity('POLICY_MISSING')
    selected = max(policies, key=lambda p: (p['effective_session'] or '', p['policy_revision']))
    policy = _object(db, 'account_policies', account, selected['id'])
    contract = policy['policy']
    expected_id = 'policy:' + digest({'account_id': account, 'revision': policy['policy_revision']})
    if (policy['policy_id'] != expected_id or policy['id'] != expected_id
            or policy['policy_hash'] != digest(contract)
            or policy['policy_hash'] != reference['account_policy_hash']
            or policy['policy_id'] != reference['account_policy_id']
            or decision.get('policy_id') != policy['policy_id']
            or decision.get('policy_hash') != policy['policy_hash']
            or decision.get('policy_document') != policy.get('document')
            or contract.get('account_id') != account or contract.get('strategy_id') != strategy
            or contract.get('policy_id') != policy['policy_id']
            or contract.get('effective_from') != policy['effective_session']
            or contract.get('effective_to') is not None
            or _time(contract['known_at']) > cutoff
            or policy.get('definition_status') != 'REGISTERED'
            or policy['definition'].get('implementation_hash') != reference['strategy_implementation_hash']
            or contract['strategy_version'] != plan['strategy_version']
            or decision['strategy_version'] != plan['strategy_version']):
        raise Integrity('POLICY_PLAN_MISMATCH')
    orders = _select(db, 'orders', account, ORDER_FIELDS,
                     where="AND json_extract(data,'$.target_session')=?", parameters=(target,), limit=MAX_ORDERS)
    orders.sort(key=lambda row: row['order_index'])
    original = plan['orders']
    if (not isinstance(original, list) or len(original) > MAX_ORDERS
            or [row['order_id'] for row in orders] != plan['order_ids']
            or [row.get('order_id') for row in original] != plan['order_ids']
            or [row['order_index'] for row in orders] != list(range(len(orders)))
            or (plan['status'] == 'EMPTY') != (len(orders) == 0)):
        raise Integrity('ORDER_SET_MISMATCH')
    cash_reserved, shares_reserved = 0, {}
    for row, saved in zip(orders, original, strict=True):
        if (row['id'] != row['order_id'] or row['plan_id'] != plan['plan_id']
                or row['status'] != 'PENDING'
                or any(row.get(field) != saved.get(field) for field in ORDER_FIELDS)):
            raise Integrity('ORDER_CONTRACT_MISMATCH')
        _int(row['quantity'], minimum=1)
        reserve = _object(db, 'reservations', account, row['order_id'])
        if (reserve.get('status') != 'ACTIVE' or reserve.get('order_id') != row['order_id']
                or reserve.get('code') != row['code'] or reserve.get('side') != row['side']
                or reserve.get('cash_fen') != row['reserved_cash_fen']
                or reserve.get('shares') != row['reserved_shares']):
            raise Integrity('RESERVATION_MISMATCH')
        _int(reserve['cash_fen'])
        _int(reserve['shares'])
        if row['side'] == 'SELL':
            allocations = reserve.get('lot_allocations')
            if (reserve['cash_fen'] != 0 or reserve['shares'] != row['quantity']
                    or not isinstance(allocations, list) or len(allocations) > MAX_PENDING
                    or sum(_int(a['quantity']) for a in allocations) != row['quantity']):
                raise Integrity('SELL_RESERVATION_MISMATCH')
            for allocation in allocations:
                lot_id = allocation['lot_id']
                shares_reserved[lot_id] = shares_reserved.get(lot_id, 0) + allocation['quantity']
                lot = _select(db, 'position_lots', account, ('code', 'quantity'),
                              where='AND id=?', parameters=(lot_id,), limit=1)
                if (not lot or lot[0]['code'] != row['code']
                        or _int(lot[0]['quantity']) < shares_reserved[lot_id]):
                    raise Integrity('SELL_RESERVATION_MISMATCH')
        elif row['side'] != 'BUY' or reserve['shares'] != 0:
            raise Integrity('RESERVATION_MISMATCH')
        cash_reserved += reserve['cash_fen']
    active = _select(db, 'reservations', account, ('order_id',),
                     where="AND json_extract(data,'$.status')='ACTIVE'", limit=MAX_ORDERS)
    if {row['order_id'] for row in active} != set(plan['order_ids']):
        raise Integrity('UNEXPECTED_RESERVATION')
    if cash_reserved != _int(plan['buy_budget_fen']):
        raise Integrity('RESERVATION_MISMATCH')
    cash = db.execute('SELECT COALESCE(SUM(amount_fen),0) FROM cash_entries WHERE account_id=?',
                      (account,)).fetchone()[0]
    tax_reserve = db.execute("SELECT COALESCE(SUM(MAX(0,json_extract(data,'$.assessed_fen')-"
        "COALESCE(json_extract(data,'$.paid_fen'),0))),0) FROM tax_items WHERE account_id=? "
        "AND json_extract(data,'$.status') NOT IN ('PAID','RESOLVED','NOT_TRIGGERED')", (account,)).fetchone()[0]
    if type(cash) is not int or cash < cash_reserved + tax_reserve:
        raise Integrity('CASH_RESERVATION_UNFUNDED')
    if db.execute("SELECT 1 FROM fills WHERE account_id=? AND "
                  "json_extract(data,'$.target_session')=? LIMIT 1", (account, target)).fetchone():
        raise Integrity('FUTURE_FILL_PRESENT')
    return {'plan_id': plan['plan_id'], 'plan_hash': plan['plan_hash'], 'status': plan['status'],
            'frozen_at': plan['frozen_at'], 'order_count': len(orders),
            'policy_id': policy['policy_id'], 'proof_identity': digest([plan, decision, policy, orders])}


def _pending(db, account, day, target):
    reasons = []
    rows = _select(db, 'orders', account, ('target_session', 'status'),
                   where="AND (json_extract(data,'$.target_session')<? OR "
                   "json_extract(data,'$.target_session') IS NULL) AND "
                   "(json_extract(data,'$.status') NOT IN ('FILLED','NOT_FILLED') OR "
                   "json_extract(data,'$.status') IS NULL)", parameters=(target,))
    if rows:
        reasons.append('HISTORICAL_ORDERS_PENDING')
    taxes = _select(db, 'tax_items', account, ('status', 'assessed_fen', 'paid_fen', 'due_date'),
                    where="AND (json_extract(data,'$.status') NOT IN ('PAID','RESOLVED','NOT_TRIGGERED') "
                    "OR json_extract(data,'$.status') IS NULL)")
    if any(row['status'] != 'PAYABLE' or type(row['assessed_fen']) is not int
           or type(row['paid_fen']) is not int
           or row['assessed_fen'] < 0 or row['paid_fen'] < 0
           or row['due_date'] and _day(row['due_date']) <= day
           and row['assessed_fen'] > row['paid_fen'] for row in taxes):
        reasons.append('TAX_OBLIGATION_PENDING')
    entitlements = _select(db, 'entitlements', account, ('kind', 'status', 'payment_date', 'listing_date'),
                          where="AND (json_extract(data,'$.status') NOT IN ('PAID','DELIVERED') "
                          "OR json_extract(data,'$.status') IS NULL)")
    if any(row['status'] == 'WAITING_ALLOCATION' for row in entitlements):
        reasons.append('SHARE_ALLOCATION_PENDING')
    if any(row['kind'] not in {'CASH', 'SHARES'} for row in entitlements):
        reasons.append('ENTITLEMENT_KIND_UNKNOWN')
    if any((value := row['payment_date'] if row['kind'] == 'CASH' else row['listing_date'])
           and _day(value) <= day for row in entitlements if row['kind'] in {'CASH', 'SHARES'}):
        reasons.append('DUE_ENTITLEMENT_PENDING')
    cases = _select(db, 'corporate_action_cases', account, ('status', 'actions'),
                    where="AND json_extract(data,'$.status') IN "
                    "('WAITING_DATA','IDENTITY_PENDING','REVISION_PENDING','RULE_PENDING')")
    for case in cases:
        raw = case['actions']
        if not isinstance(raw, str) or len(raw) > 64 * 1024:
            reasons.append('CORPORATE_ACTION_PENDING')
            break
        actions = json.loads(raw)
        if not (isinstance(actions, dict) and actions and all(
            isinstance(action, dict) and action.get('state') in {'NOT_DUE', 'NOT_APPLICABLE'}
            and (action.get('state') == 'NOT_APPLICABLE'
                 or _day(action.get('operation_date')) > day) for action in actions.values())):
            reasons.append('CORPORATE_ACTION_PENDING')
            break
    return reasons


def read_facts(settings, day, target, now):
    """No writes, lock creation, schema repair, full originals or history scans."""
    _day(day)
    _day(target)
    path = settings.database
    if (not path.is_file() or path.is_symlink()
            or not path.resolve().is_relative_to(settings.root.resolve())):
        raise Unavailable('LEDGER_UNAVAILABLE')
    result = {'accounts': [], 'fingerprint': None}
    with verification_scope(settings.root, persist=False), closing(sqlite3.connect(
            path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise Unavailable('LEDGER_SCHEMA_UNSUPPORTED')
        for spec in settings.accounts:
            facts = {'account_id': spec.id, 'strategy_id': spec.strategy, 'reasons': [], 'state': 'READY'}
            try:
                account = db.execute('SELECT strategy_id FROM accounts WHERE account_id=?', (spec.id,)).fetchone()
                if account is None:
                    raise Integrity('ACCOUNT_MISSING')
                if account[0] != spec.strategy:
                    raise Integrity('ACCOUNT_STRATEGY_MISMATCH')
                sql = ','.join(f"json_extract(data,'$.{field}') AS {field}" for field in STATE_FIELDS)
                row = db.execute(f'SELECT {sql} FROM account_state WHERE account_id=?', (spec.id,)).fetchone()
                if row is None:
                    raise Integrity('ACCOUNT_STATE_MISSING')
                state = dict(row)
                facts.update(state)
                for name in ('account_revision', 'report_revision', 'last_rendered_revision'):
                    _int(state[name])
                if not state['simulation_start_session'] or _day(state['simulation_start_session']) > day:
                    facts['reasons'].append('ACCOUNT_NOT_ACTIVE_FOR_DAY')
                if state['restate_from'] and _day(state['restate_from']) < target:
                    facts['reasons'].append('RESTATEMENT_PENDING')
                if any(not state[key] or _day(state[key]) < day for key in ('settled_through', 'valued_through')):
                    facts['reasons'].append('CONTINUOUS_WATERMARK_PENDING')
                fields = ('session', 'statement_revision', 'accounting_status', 'valuation_status', 'processed_at')
                select = ','.join(f"json_extract(data,'$.{field}') AS {field}" for field in fields)
                nav = db.execute(f'SELECT {select} FROM daily_nav WHERE account_id=? '
                    "AND json_extract(data,'$.session')=? ORDER BY "
                    "json_extract(data,'$.statement_revision') DESC LIMIT 1", (spec.id, day)).fetchone()
                facts['statement'] = dict(nav) if nav else None
                if not nav or nav['accounting_status'] != 'COMPLETE' or nav['valuation_status'] != 'FINAL':
                    facts['reasons'].append('FINAL_STATEMENT_PENDING')
                elif _time(nav['processed_at']) > now:
                    raise Integrity('FUTURE_STATEMENT')
                facts['reasons'].extend(_pending(db, spec.id, day, target))
                facts['plan'] = _plan(db, settings.root, spec.id, spec.strategy, day, target, now)
                if facts['plan'] is None:
                    facts['reasons'].append('NEXT_PLAN_PENDING')
            except Unavailable as exc:
                facts.update(state='UNKNOWN', reasons=[str(exc)])
            except (Integrity, KeyError, TypeError, ValueError) as exc:
                facts.update(state='BLOCKED', reasons=[str(exc) if isinstance(exc, Integrity) else 'INVALID_LEDGER_FACTS'])
            result['accounts'].append(facts)
        db.rollback()
    result['fingerprint'] = digest(result['accounts'])
    return result
