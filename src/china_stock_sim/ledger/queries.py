"""Consistent read-only ledger views. No query collects data or repairs an account."""

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from china_stock_sim.market.contracts import PendingEventRange, session_date

UNRESOLVED = {"WAITING_DATA", "IDENTITY_PENDING", "REVISION_PENDING", "RULE_PENDING"}


def latest_statements(tx, *, include_invalidated=False, summary=False):
    current = {}
    rows = (tx.select('daily_nav', fields=('session', 'id', 'statement_revision',
                'accounting_status', 'valuation_status', 'net_assets_fen', 'account_revision'))
            if summary else tx.list('daily_nav'))
    for row in rows:
        day = row["session"]
        if row.get("statement_revision", 1) > current.get(day, {}).get("statement_revision", 0):
            current[day] = row
    marker = tx.state.get("restate_from")
    if marker and not include_invalidated:
        for day, row in list(current.items()):
            if day >= marker:
                current[day] = {**row, "accounting_status": "PARTIAL", "valuation_status": "PROVISIONAL",
                    "net_assets_fen": None, "daily_pnl_fen": None, "cumulative_pnl_fen": None, "net_value": None,
                    "provisional_assets_fen": row.get("net_assets_fen", row.get("provisional_assets_fen")),
                    "requires_restatement": True, "published_accounting_status": row["accounting_status"],
                    "published_valuation_status": row["valuation_status"],
                    "published_net_assets_fen": row.get("net_assets_fen"),
                    "issues": [*row.get("issues", []), {"kind": "RESTATEMENT_PENDING", "from": marker}]}
    return current


def rebalance_views(tx, now):
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    result = []
    for task in tx.list("rebalance_tasks"):
        remaining = []
        for goal in task.get("targets", []):
            done = task.get("completed_quantities", {}).get(f'{goal["code"]}:{goal["side"]}', 0)
            if goal["quantity"] > done:
                remaining.append({**goal, "quantity": goal["quantity"] - done})
        expired = task["end_session"] < local.date().isoformat() or (
            task["end_session"] == local.date().isoformat() and local.time() >= time(18, 10))
        state = "COMPLETE" if not remaining else "EXPIRED" if expired else task.get("status", "ACTIVE")
        result.append({**task, "status": state, "remaining_targets": remaining,
                       "reason": "TRADING_WINDOW_EXPIRED" if state == "EXPIRED" else None})
    return result


def historical_lots(tx, day):
    """Rebuild quantities/cost from append-only movements, including later sold-out lots."""
    by_lot = defaultdict(list)
    for movement in tx.list("lot_movements"):
        by_lot[movement["lot_id"]].append(movement)
    result = []
    for lot in tx.list("position_lots"):
        if lot["acquired_date"] > day:
            continue
        movements = by_lot.get(lot["lot_id"])
        if movements:
            quantity = sum(m["quantity_delta"] for m in movements if m["effective_date"] <= day)
            cost = sum(m["cost_delta_fen"] for m in movements if m["effective_date"] <= day)
        else:
            quantity, cost = lot["quantity"], lot["cost_fen"]
        if quantity:
            result.append({**lot, "quantity": quantity, "cost_fen": cost})
    return sorted(result, key=lambda r: (r["acquired_date"], r["lot_id"]))


def position_rows(tx, day, own_order_id=None):
    positions = {}
    for lot in historical_lots(tx, day):
        row = positions.setdefault(lot["code"], {"code": lot["code"], "total_shares": 0,
            "sellable_shares": 0, "reserved_shares": 0, "cost_fen": 0, "lots": []})
        row["total_shares"] += lot["quantity"]
        row["cost_fen"] += lot["cost_fen"]
        row["lots"].append(lot)
        if lot.get("sellable_date") and lot["sellable_date"] <= day:
            row["sellable_shares"] += lot["quantity"]
    for reservation in tx.list("reservations"):
        if reservation.get("status") != "ACTIVE" or reservation.get("side") != "SELL":
            continue
        if reservation.get("order_id", reservation.get("id")) == own_order_id:
            continue
        if reservation["code"] in positions:
            positions[reservation["code"]]["reserved_shares"] += reservation["shares"]
    for row in positions.values():
        row["sellable_shares"] = max(0, row["sellable_shares"] - row["reserved_shares"])
    return [positions[code] for code in sorted(positions)]


def pending_cases(tx):
    entitlements, taxes = tx.list("entitlements"), tx.list("tax_items")
    pending = {row["case_id"] for row in entitlements if row.get("status") not in {"PAID", "DELIVERED"}}
    pending.update(row["case_id"] for row in taxes if row.get("status") not in {"PAID", "RESOLVED", "NOT_TRIGGERED"})
    result = []
    for case in tx.list("corporate_action_cases"):
        actions = case.get("actions", {})
        accrued = all(actions.get(action, {}).get("state") in {"APPLIED", "NOT_APPLICABLE"}
                      for action in ("cash_accrual", "share_accrual"))
        if case.get("status") in UNRESOLVED or case["case_id"] in pending or (
                case.get("status") == "REGISTERED" and not accrued and not any(
                    e["case_id"] == case["case_id"] for e in entitlements)):
            result.append(case)
    return result


def active_tax(tx):
    return [row for row in tx.list("tax_items") if row.get("status") != "NOT_TRIGGERED"]


def account_view(tx, target_session, now):
    cash = tx.balance()
    reserved = sum(r.get("cash_fen", 0) for r in tx.list("reservations")
                   if r.get("status") == "ACTIVE" and r.get("side") == "BUY")
    tax = active_tax(tx)
    known_tax = sum(max(0, row["assessed_fen"] - row.get("paid_fen", 0))
                    for row in tax if row.get("assessed_fen") is not None)
    reasons = []
    start = tx.state.get("simulation_start_session")
    if not start or target_session < start:
        reasons.append({"kind": "NOT_STARTED"})
    if any(row.get("assessed_fen") is None for row in tax):
        reasons.append({"kind": "UNKNOWN_TAX_LIABILITY"})
    pending_orders = [o for o in tx.list("orders") if o.get("status") in {"PENDING", "WAITING_DATA"}
                      and o["target_session"] < target_session]
    if pending_orders:
        reasons.append({"kind": "UNRESOLVED_ORDERS", "order_ids": [o["order_id"] for o in pending_orders]})
    cases = [c for c in tx.list("corporate_action_cases") if c.get("status") in UNRESOLVED
             and not (c.get("actions") and all(
                 action.get("state") == "NOT_DUE" and action.get("operation_date", "") > now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                 for action in c["actions"].values()))]
    if cases:
        reasons.append({"kind": "UNRESOLVED_ENTITLEMENTS", "case_ids": [c["case_id"] for c in cases]})
    if any(e.get("status") == "WAITING_ALLOCATION" for e in tx.list("entitlements")):
        reasons.append({"kind": "UNKNOWN_SHARE_ALLOCATION"})
    if tx.state.get("restate_from") and tx.state["restate_from"] < target_session:
        reasons.append({"kind": "RESTATEMENT_PENDING", "from": tx.state["restate_from"]})
    if cash < reserved + known_tax:
        reasons.append({"kind": "CASH_SHORTFALL", "shortfall_fen": reserved + known_tax - cash})
    plans = tx.list("plans", target_session=target_session)
    if plans:
        reasons.append({"kind": "PLAN_ALREADY_FROZEN", "plan_id": plans[0]["plan_id"]})
    cutoff = datetime.combine(date.fromisoformat(target_session), time(9), ZoneInfo("Asia/Shanghai"))
    if now >= cutoff:
        reasons.append({"kind": "MISSED_CUTOFF"})
    statements = latest_statements(tx, summary=True)
    latest = max(statements.values(), key=lambda row: row["session"], default=None)
    if latest and latest["session"] < target_session and latest["valuation_status"] != "FINAL":
        reasons.append({"kind": "VALUATION_NOT_FINAL", "session": latest["session"]})
    return {"account_id": tx.account["account_id"], "strategy_id": tx.account["strategy_id"],
            "initial_cash_fen": tx.account["initial_cash_fen"],
            "simulation_start_session": start,
            "account_revision": tx.state.get("account_revision", 0),
            "report_revision": tx.state.get("report_revision", 0),
            "last_rendered_revision": tx.state.get("last_rendered_revision", 0),
            "state_id": f'{tx.account["account_id"]}:{tx.state.get("account_revision", 0)}',
            "target_session": target_session, "snapshot_at": now.isoformat(),
            "settled_through": tx.state.get("settled_through"),
            "valued_through": tx.state.get("valued_through"), "cash_fen": cash,
            "reserved_cash_fen": reserved, "tax_reserved_fen": known_tax, "reserved_tax_fen": known_tax,
            "available_cash_fen": max(0, cash - reserved - known_tax),
            "known_tax_payable_fen": known_tax,
            "positions": position_rows(tx, target_session),
            "cash_receivable_fen": sum(e.get("amount_fen", 0) - e.get("paid_fen", 0)
                for e in tx.list("entitlements") if e.get("kind") == "CASH"),
            "share_receivables": [e for e in tx.list("entitlements")
                if e.get("kind") == "SHARES" and Decimal(e["quantity"]) > e.get("delivered_quantity", 0)],
            "unknown_items": [r for r in tax if r.get("assessed_fen") is None] + cases,
            "valuation_status": latest["valuation_status"] if latest else "NOT_STARTED",
            "blocking_reasons": reasons, "can_plan": not reasons,
            "active_plan": plans[0] if plans else None}


def _page(rows, *, start=None, end=None, offset=0, limit=1000):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("分页必须为非负offset及1到10000的limit")
    start = session_date(start) if start else None
    end = session_date(end) if end else None
    if start and end and start > end:
        raise ValueError("起始日不能晚于截止日")
    def day(row):
        return row.get("session", row.get("effective_date", row.get("target_session", "")))
    rows = [row for row in rows if (not start or day(row) >= start) and (not end or day(row) <= end)]
    rows.sort(key=lambda row: (day(row), row.get("id", row.get("order_id", ""))))
    return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit}


class QueriesMixin:
    def revision_state(self, account_id):
        """Small consistent revisions; never expand financial or audit documents."""
        return self._read(account_id, lambda tx: {
            'account_id': tx.account_id,
            **{key: tx.state.get(key) for key in (
                'account_revision', 'report_revision', 'last_rendered_revision',
                'simulation_start_session', 'settled_through', 'valued_through',
                'restate_from', 'restate_through', 'last_processed_at')},
            'report_pending': tx.state['report_revision'] > tx.state.get('last_rendered_revision', 0),
        })

    def workflow_status(self, account_id, *, through=None):
        """Recovery metadata only. This view never claims a full audit was performed."""
        end = session_date(through) if through is not None else None

        def read(tx):
            orders = tx.select('orders', fields=('id', 'plan_id', 'code', 'target_session', 'status'),
                               in_values={'status': ('PENDING', 'WAITING_DATA')})
            rows = tx.select('daily_nav', fields=('id', 'session', 'statement_revision',
                'accounting_status', 'valuation_status'),
                ranges={'session': {'lte': end}} if end else None)
            statements = {}
            for row in rows:
                day = row['session']
                if row.get('statement_revision', 1) > statements.get(day, {}).get('statement_revision', 0):
                    statements[day] = row
            marker = tx.state.get('restate_from')
            if marker:
                for day, row in statements.items():
                    if day >= marker:
                        row.update(accounting_status='PARTIAL', valuation_status='PROVISIONAL',
                                   requires_restatement=True)
            return {'account_id': tx.account_id,
                **{key: tx.state.get(key) for key in (
                    'account_revision', 'report_revision', 'last_rendered_revision',
                    'simulation_start_session', 'settled_through', 'valued_through',
                    'restate_from', 'restate_through')},
                'report_pending': tx.state['report_revision'] > tx.state.get('last_rendered_revision', 0),
                'orders': orders, 'statements': [statements[d] for d in sorted(statements)],
                'read_only': True, 'audit_scope': 'WORKFLOW_METADATA'}
        return self._read(account_id, read)

    def list_accounts(self):
        """Read the persistent registry without consulting current seed policies."""
        from china_stock_sim.ledger.store import read_snapshot

        snapshot = read_snapshot(self.settings)
        rows = snapshot['accounts']
        if snapshot['schema_version'] >= 3:
            from china_stock_sim.ledger.kernel import LedgerError

            day = self._now().astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            for row in rows:
                try:
                    row['display_name'] = self.get_account_policy(row['account_id'], day)['display_name']
                except LedgerError:
                    row.update(display_name=row['account_id'], account_policy_status='AUDIT_UNAVAILABLE')
        return [{**row, 'display_name': row.get('display_name') or row['account_id'],
                 'registration_source': 'ledger'} for row in rows]

    def get_account_profile(self, account_id):
        from china_stock_sim.ledger.kernel import LedgerError

        for account in self.list_accounts():
            if account['account_id'] == account_id:
                return account
        raise LedgerError('INVALID_INPUT', '未知账户', account_id=account_id)

    def _account_state_tx(self, tx, target_session, own_order_id=None):
        day, now = session_date(target_session), self._now()
        view = account_view(tx, day, now)
        view["positions"] = position_rows(tx, day, own_order_id)
        if view["simulation_start_session"] and day > view["simulation_start_session"]:
            previous = self.market.calendar_operation("previous_session", day)
            if not previous.ready or not view["settled_through"] or view["settled_through"] < previous.data:
                view["blocking_reasons"].append({"kind": "PRIOR_ACCOUNTING_INCOMPLETE"})
                view["can_plan"] = False
        return view

    def account_state(self, account_id, target_session, *, own_order_id=None):
        return self._read(account_id, lambda tx: self._account_state_tx(tx, target_session, own_order_id))

    def execution_view(self, account_id, plan_id):
        from china_stock_sim.ledger.execution_support import build_execution_view
        return self._read(account_id, lambda tx: build_execution_view(self, tx, plan_id))

    def get_plan_header(self, account_id, *, target_session):
        """Read a locator only; no assertion of full original/audit validation."""
        day = session_date(target_session)

        def read(tx):
            from china_stock_sim.ledger.kernel import LedgerError
            rows = tx.select('plans', fields=('id', 'plan_id', 'target_session', 'plan_hash'),
                             equals={'target_session': day}, limit=2)
            if len(rows) > 1:
                raise LedgerError('INTEGRITY_ERROR', '同日存在重复冻结计划')
            return {**rows[0], 'audit_scope': 'PLAN_LOCATOR'} if rows else None
        return self._read(account_id, read)

    def terminal_execution_status(self, account_id, plan_id):
        """Read an already-completed execution without reopening its original tree.

        The cached proof binds the full plan original to this header. This is
        terminal status, not a replacement for replay/audit or pending guards.
        """
        from china_stock_sim.ledger.decision_evidence import (
            cached_validation,
            read_business_document,
            verification_scope,
        )
        from china_stock_sim.ledger.kernel import LedgerError, digest, identifier
        from china_stock_sim.ledger.plan_storage import FORMAT, verified_header_cached

        def read(tx):
            identity = identifier(plan_id, '计划ID')
            found = tx.select('plans', fields=('storage_format',), equals={'id': identity}, limit=1)
            if not found:
                raise LedgerError('INVALID_INPUT', '冻结计划不存在', plan_id=plan_id)
            if found[0]['storage_format'] != FORMAT:
                return {'state': 'UNAVAILABLE', 'reason': 'LEGACY_FULL_PLAN'}
            plan = tx.get('plans', identity)
            if not verified_header_cached(self.root, plan):
                return {'state': 'UNAVAILABLE', 'reason': 'PLAN_HEADER_VERIFICATION_REQUIRED'}
            orders = sorted(tx.select('orders', fields=('id', 'order_id', 'account_id', 'plan_id',
                'code', 'side', 'quantity', 'limit_price', 'order_index', 'target_session', 'status'),
                equals={'plan_id': identity}), key=lambda r: r['order_index'])
            if any(row.get('status') not in {'FILLED', 'NOT_FILLED'} for row in orders):
                return {'state': 'UNAVAILABLE', 'reason': 'ORDERS_NOT_TERMINAL'}
            frozen = plan['orders']
            if ([row['order_id'] for row in orders] != plan['order_ids']
                    or [row['order_id'] for row in frozen] != plan['order_ids']
                    or [row['order_index'] for row in orders] != list(range(len(orders)))):
                raise LedgerError('INTEGRITY_ERROR', '终态订单集合或顺序与冻结计划不一致')
            for order, original in zip(orders, frozen, strict=True):
                if (order.get('account_id') != account_id or order.get('plan_id') != identity
                        or any(order.get(key) != original.get(key) for key in (
                            'code', 'side', 'quantity', 'limit_price', 'order_index', 'target_session'))):
                    raise LedgerError('INTEGRITY_ERROR', '终态订单经济合同与冻结计划不一致')
                reserves = tx.select('reservations', fields=('status',), equals={'id': order['order_id']}, limit=1)
                fills = tx.select('fills', fields=('order_id', 'plan_id', 'code', 'side', 'quantity',
                    'target_session'), equals={'id': order['order_id']}, limit=1)
                fill = fills[0] if fills else None
                if not reserves or reserves[0].get('status') != 'RELEASED':
                    raise LedgerError('INTEGRITY_ERROR', '终态订单预留未释放')
                if order['status'] == 'FILLED':
                    if not fill or any(fill.get(key) != order.get(key) for key in (
                            'order_id', 'plan_id', 'code', 'side', 'quantity', 'target_session')):
                        raise LedgerError('INTEGRITY_ERROR', '终态成交登记与订单不一致')
                elif fill:
                    raise LedgerError('INTEGRITY_ERROR', '未成交订单存在矛盾成交登记')
            if plan.get('provenance_mode') == 'STRATEGY':
                reference = plan['decision_ref']
                decisions = tx.select('strategy_decisions', fields=('id', 'state', 'plan_id',
                    'target_session', 'decision_ref'), equals={'id': reference['decision_id']}, limit=1)
                if (not decisions or decisions[0]['state'] != 'FROZEN'
                        or decisions[0]['plan_id'] != identity
                        or decisions[0]['target_session'] != plan['target_session']
                        or json_value(decisions[0]['decision_ref']) != reference):
                    raise LedgerError('INTEGRITY_ERROR', '终态计划与权威冻结决策不一致')
                size = tx.db.execute('SELECT length(CAST(data AS BLOB)) FROM strategy_decisions '
                    'WHERE account_id=? AND id=?', (account_id, reference['decision_id'])).fetchone()[0]
                if size > 1024 * 1024:
                    return {'state': 'UNAVAILABLE', 'reason': 'LEGACY_FULL_DECISION'}
                decision = tx.get('strategy_decisions', reference['decision_id'])
                if not cached_validation(self.root, 'strategy_decision', digest(decision)):
                    return {'state': 'UNAVAILABLE', 'reason': 'DECISION_ORIGINAL_VERIFICATION_REQUIRED'}
            states = {row['order_id']: row['status'] for row in orders}
            result = {'state': 'READY', 'audit_scope': 'TERMINAL_STATUS', 'account_id': account_id,
                'plan_id': identity, 'plan_hash': plan['plan_hash'], 'target_session': plan['target_session'],
                'account_revision': tx.state['account_revision'], 'completion_status': 'COMPLETE',
                'plan_status': 'COMPLETE' if orders else 'EMPTY', 'order_states': states}
            if orders:
                step = {'effective_date': plan['target_session'], 'step': 'execution',
                        'status': 'SUCCEEDED', 'plan_id': identity, 'order_states': states}
                step_id = 'execution-step:' + digest(step)
                saved = tx.get('job_runs', step_id)
                if saved is None:
                    return {'state': 'UNAVAILABLE', 'reason': 'EXECUTION_STEP_REPAIR_REQUIRED'}
                body, _ = read_business_document(self.root, saved['document'])
                if body != step or any(saved.get(key) != value for key, value in step.items()):
                    raise LedgerError('INTEGRITY_ERROR', '终态执行步骤与订单状态不一致')
                result['step_receipt'] = {'state': 'ALREADY_APPLIED', 'step_id': step_id,
                                          'step_state': 'SUCCEEDED'}
            return result

        def json_value(value):
            import json
            return json.loads(value) if isinstance(value, str) else value

        with verification_scope(self.root, persist=False):
            return self._read(account_id, read)

    def positions(self, account_id, target_session, *, own_order_id=None):
        return self._read(account_id, lambda tx: position_rows(tx, session_date(target_session), own_order_id))

    def lots(self, account_id, target_session=None, *, code=None):
        return self._read(account_id, lambda tx: [r for r in (
            historical_lots(tx, session_date(target_session)) if target_session else tx.list("position_lots"))
            if code is None or r["code"] == code])

    def get_plan(self, account_id, plan_id=None, *, target_session=None):
        if (plan_id is None) == (target_session is None):
            raise ValueError("应明确plan_id或target_session其中一项")
        from china_stock_sim.ledger.plan_storage import resolve_plan
        return self._read(account_id, lambda tx: resolve_plan(self.root,
            tx.get("plans", plan_id) if plan_id is not None else
            next(iter(tx.list("plans", target_session=session_date(target_session))), None)))

    def orders(self, account_id, *, plan_id=None, **pagination):
        return self._read(account_id, lambda tx: _page(tx.list("orders", **({"plan_id": plan_id} if plan_id else {})), **pagination))

    def fills(self, account_id, **pagination):
        return self._read(account_id, lambda tx: _page(tx.list("fills"), **pagination))

    def corporate_action_cases(self, account_id, *, code=None):
        return self._read(account_id, lambda tx: tx.list("corporate_action_cases", **({"code": code} if code else {})))

    def tax_items(self, account_id, *, code=None):
        return self._read(account_id, lambda tx: tx.list("tax_items", **({"code": code} if code else {})))

    def no_plan_status(self, account_id, target_session):
        """One durable no-plan receipt; no plans, policies or audit are expanded."""
        day = session_date(target_session)
        return self._read(account_id, lambda tx: tx.get('job_runs', f'no-plan:{day}'))

    def daily_statement(self, account_id, session, *, statement_revision=None):
        day = session_date(session)
        def read(tx):
            if statement_revision is None:
                rows = tx.list('daily_nav', session=day)
                row = max(rows, key=lambda r: r.get('statement_revision', 1), default=None)
                marker = tx.state.get('restate_from')
                if row and marker and day >= marker:
                    row = {**row, 'accounting_status': 'PARTIAL', 'valuation_status': 'PROVISIONAL',
                        'net_assets_fen': None, 'daily_pnl_fen': None, 'cumulative_pnl_fen': None,
                        'net_value': None, 'provisional_assets_fen': row.get(
                            'net_assets_fen', row.get('provisional_assets_fen')),
                        'requires_restatement': True,
                        'published_accounting_status': row['accounting_status'],
                        'published_valuation_status': row['valuation_status'],
                        'published_net_assets_fen': row.get('net_assets_fen'),
                        'issues': [*row.get('issues', []), {'kind': 'RESTATEMENT_PENDING', 'from': marker}]}
                return row
            return next((r for r in tx.list("daily_nav", session=day)
                         if r["statement_revision"] == statement_revision), None)
        return self._read(account_id, read)

    def history(self, account_id, *, include_revisions=False, **pagination):
        return self._read(account_id, lambda tx: _page(tx.list("daily_nav") if include_revisions else
                                                     list(latest_statements(tx).values()), **pagination))

    def _report(self, tx, target_session, now, *, mode='full', recent_sessions=60,
                include_trading_history=False):
        # Every section reads through this single database transaction.
        from china_stock_sim.ledger.account_policies import verified_policy
        from china_stock_sim.ledger.kernel import LedgerError
        from china_stock_sim.ledger.plan_storage import resolve_plan
        from china_stock_sim.ledger.strategy_decisions import verify_decision_record

        if mode not in {'full', 'recent'} or type(recent_sessions) is not int or not 1 <= recent_sessions <= 250:
            raise LedgerError('INVALID_INPUT', '报告范围须为full或有界recent交易日')
        if type(include_trading_history) is not bool:
            raise LedgerError('INVALID_INPUT', '交易历史投影开关须为布尔值')
        scope = {'mode': mode, 'recent_sessions': recent_sessions if mode == 'recent' else None,
                 'has_more': False, 'next_before': None, 'includes_all_unresolved_plans': True}
        plan_rows = tx.select('plans', fields=('id', 'target_session'))
        scope['total_plans'] = len(plan_rows)
        selected_ids, selected_days = None, None
        if mode == 'recent':
            days = sorted({row['target_session'] for row in plan_rows
                if row.get('target_session')} | {row['session'] for row in tx.select('daily_nav', fields=('session',))
                                                if row.get('session')}, reverse=True)
            selected_days = set(days[:recent_sessions])
            selected_days.add(target_session)
            selected_ids = {row['id'] for row in plan_rows if row['target_session'] in selected_days}
            selected_ids.update(row['plan_id'] for row in tx.select('orders', fields=('plan_id',),
                in_values={'status': ('PENDING', 'WAITING_DATA')}) if row.get('plan_id'))
            active_tasks = {row['id'] for row in tx.select('rebalance_tasks', fields=('id',),
                in_values={'status': ('ACTIVE', 'PENDING', 'IN_PROGRESS')},
                ranges={'end_session': {'gte': now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()}})}
            if active_tasks:
                selected_ids.update(row['id'] for row in tx.select('plans', fields=('id',),
                                    in_values={'task_id': tuple(active_tasks)}))
            scope.update(has_more=len(selected_ids) < len(plan_rows),
                         next_before=min(selected_days) if len(days) > recent_sessions and selected_days else None,
                         first_session=min(selected_days) if selected_days else None,
                         last_session=max(selected_days) if selected_days else None)

        def selected(table, field='target_session'):
            if mode == 'full':
                return tx.list(table)
            date_fields = (field, 'effective_date', 'session') if table == 'job_runs' else (field,)
            identifiers = tx.select(table, fields=tuple(dict.fromkeys(('id', *date_fields, 'plan_id'))))
            return [tx.get(table, row['id']) for row in identifiers
                    if any(row.get(key) in selected_days for key in date_fields)
                    or row.get('plan_id') in selected_ids]

        plans = (tx.list('plans') if mode == 'full' else
                 [tx.get('plans', identity) for identity in sorted(selected_ids)])
        if mode == 'full':
            expanded = []
            for row in plans:
                try:
                    expanded.append(resolve_plan(self.root, row))
                except (LedgerError, OSError, KeyError, TypeError):
                    # A report must retain financial rows when an audit original is
                    # unavailable. Its unchanged header lets audit_account expose
                    # AUDIT_UNAVAILABLE; explicit get_plan/execution still fail closed.
                    expanded.append(row)
            plans = expanded
        scope['returned_plans'] = len(plans)

        def audit_projection(verify, row):
            try:
                if verify is verify_decision_record and mode == 'recent':
                    return verify(self.root, row, materialize=False)
                return verify(self.root, row)
            except LedgerError as exc:
                return {'state': 'AUDIT_UNAVAILABLE', 'id': row['id'], 'reason': str(exc)}

        policies = tx.list('account_policies')
        applicable = [r for r in policies if r['effective_session'] is None
                      or r['effective_session'] <= target_session]
        current = max(applicable, key=lambda r: (r['effective_session'] or '', r['policy_revision']),
                      default=None)
        latest = latest_statements(tx, summary=mode == 'recent')
        if mode == 'recent':
            latest = {day: {**tx.get('daily_nav', row['id']), **row}
                      for day, row in latest.items() if day in selected_days}
        report = {"account": self._account_state_tx(tx, target_session),
            "account_revision": tx.state["account_revision"],
            "report_revision": tx.state["report_revision"],
            "last_rendered_revision": tx.state.get("last_rendered_revision", 0),
            "snapshot_at": now.isoformat(), "target_session": target_session,
            "statements": [latest[day] for day in sorted(latest)],
            "orders": selected("orders"), "fills": selected("fills"),
            "cash_entries": tx.list("cash_entries"), "lots": tx.list("position_lots"),
            "cases": tx.list("corporate_action_cases"), "entitlements": tx.list("entitlements"),
            "tax_items": tx.list("tax_items"), "plans": plans, "report_scope": scope,
            "steps": selected("job_runs"), "model_attempts": selected("model_attempts"),
            "account_policy": (audit_projection(verified_policy, current) if current else
                               {'state': 'AUDIT_UNAVAILABLE', 'reason': '账户缺少适用政策'}),
            "account_policies": [audit_projection(verified_policy, r) for r in policies],
            "strategy_decisions": [audit_projection(verify_decision_record, r)
                                   for r in selected('strategy_decisions')],
            "rebalance_tasks": rebalance_views(tx, now)}
        if include_trading_history:
            from china_stock_sim.ledger.trading_history import trading_history_projection
            report['trading_history_projection'] = trading_history_projection(
                tx, now=now, target_session=target_session, statements=report['statements'])
        return report

    def report_snapshot(self, account_id, target_session=None, *, mode='full', recent_sessions=60,
                        include_trading_history=False):
        now = self._now()
        day = session_date(target_session or now.astimezone(ZoneInfo("Asia/Shanghai")).date())
        return self._read(account_id, lambda tx: self._report(tx, day, now,
            mode=mode, recent_sessions=recent_sessions,
            include_trading_history=include_trading_history))

    def export_rows(self, account_id, kind="statements", **pagination):
        allowed = {"statements": "daily_nav", "orders": "orders", "fills": "fills",
                   "cash_entries": "cash_entries", "lots": "position_lots", "tax_items": "tax_items"}
        if kind not in allowed:
            raise ValueError("不支持的导出类型")
        def read(tx):
            rows = list(latest_statements(tx).values()) if kind == "statements" else tx.list(allowed[kind])
            return {"account_id": account_id, "kind": kind, "report_revision": tx.state["report_revision"],
                    "account_revision": tx.state["account_revision"], **_page(rows, **pagination)}
        return self._read(account_id, read)

    def required_market_inputs(self, account_id, as_of):
        day = session_date(as_of)
        def read(tx):
            holdings = sorted({r['code'] for r in tx.select('position_lots', fields=('code', 'quantity'),
                ranges={'quantity': {'gte': 1}})})
            orders = sorted({r['code'] for r in tx.select('orders', fields=('code',),
                in_values={'status': ('PENDING', 'WAITING_DATA')})})
            cases = pending_cases(tx)
            ranges, unknown = [], []
            for case in cases:
                event = case.get("event", {})
                dates = [d for d in (case.get("record_date"), event.get("record_date"),
                         event.get("ex_date"), case.get("proposal_date")) if d and d <= day]
                if dates:
                    value = PendingEventRange(case["code"], min(dates), day, tuple(case.get("event_ids", ())))
                    ranges.append({"code": value.code, "start": value.start, "end": value.end,
                                   "event_ids": list(value.event_ids)})
                else:
                    unknown.append({"code": case["code"], "case_id": case["case_id"], "reason": "UNKNOWN_EVENT_RANGE"})
            unique = {str(sorted(r.items())): r for r in ranges}
            return {"as_of": day, "holdings": holdings, "pending_orders": orders,
                    "pending_actions": sorted({r["code"] for r in cases}),
                    "pending_event_ranges": list(unique.values()), "unknown_ranges": unknown}
        return self._read(account_id, read)

    def pending_work(self, account_id):
        def read(tx):
            return {"account_id": account_id, "settled_through": tx.state.get("settled_through"),
                "valued_through": tx.state.get("valued_through"),
                "simulation_start_session": tx.state.get("simulation_start_session"),
                "orders": [r for r in tx.list("orders") if r.get("status") in {"PENDING", "WAITING_DATA"}],
                "cases": pending_cases(tx),
                "entitlements": [r for r in tx.list("entitlements") if r.get("status") not in {"PAID", "DELIVERED"}],
                "tax_items": [r for r in tx.list("tax_items") if r.get("status") not in {"PAID", "RESOLVED", "NOT_TRIGGERED"}],
                "potential_tax_items": [r for r in tx.list("tax_items") if r.get("status") == "NOT_TRIGGERED"],
                "model_attempts": [r for r in tx.list("model_attempts") if r.get("status") in {"CLAIMED", "RUNNING", "RECEIVED"}],
                "rebalance_tasks": [r for r in rebalance_views(tx, self._now()) if r.get("status") not in {"COMPLETE", "EXPIRED"}],
                "expired_rebalance_tasks": [r for r in rebalance_views(tx, self._now()) if r.get("status") == "EXPIRED"],
                "restate_from": tx.state.get("restate_from"), "restate_through": tx.state.get("restate_through"),
                "steps": [r for r in tx.list("job_runs") if r.get("status") not in {"COMPLETE", "SUCCEEDED"}],
                "report_pending": tx.state["report_revision"] > tx.state.get("last_rendered_revision", 0),
                "report_revision": tx.state["report_revision"]}
        return self._read(account_id, read)
