"""Frozen execution resources, live transaction guards and retry-safe batches.

This is deliberately not a historical account replay engine. An old order can
use only its frozen resources; an unprovable change keeps that order pending.
"""

from copy import deepcopy
from decimal import Decimal

from china_stock_sim.common.execution_views import order_projection, result_summary
from china_stock_sim.ledger.execution_evidence import account_input_hash
from china_stock_sim.ledger.kernel import LedgerError, digest, identifier

TERMINAL = {"FILLED", "NOT_FILLED"}
PENDING = {"PENDING", "WAITING_DATA"}
DONE_ACTIONS = {"APPLIED", "NOT_APPLICABLE"}


def capture_original_resources(tx, account_snapshot, orders, reservations):
    """Called inside freeze_plan, before appending the plan and its reservations."""
    return {
        "schema": 1,
        "account_snapshot_id": account_snapshot["state_id"],
        "account_snapshot_hash": account_input_hash(account_snapshot),
        "account_revision": account_snapshot["account_revision"],
        "cash_fen": account_snapshot["cash_fen"],
        "available_cash_fen": account_snapshot["available_cash_fen"],
        "reserved_cash_fen": account_snapshot["reserved_cash_fen"],
        "known_tax_payable_fen": account_snapshot["known_tax_payable_fen"],
        "tax_items": deepcopy(tx.list("tax_items")),
        "cash_entry_ids": [r["id"] for r in tx.list("cash_entries")],
        "lot_movement_ids": [r["id"] for r in tx.list("lot_movements")],
        "orders": deepcopy(orders),
        "reservations": deepcopy(reservations),
    }


def _issue(kind, **details):
    return {"kind": kind, **details}


def _identity(row):
    return row.get("id", row.get("tax_id", row.get("order_id")))


def _stages(service, tx, plan):
    target = plan["target_session"]
    issues, actions = [], []
    previous = None
    start = tx.state.get("simulation_start_session")
    if not start or target < start:
        issues.append(_issue("NOT_STARTED"))
    elif target > start:
        result = service.market.calendar_operation("previous_session", target)
        previous = result.data if result.ready else None
        if not previous or not tx.state.get("settled_through") or tx.state["settled_through"] < previous:
            issues.append(_issue("PRIOR_ACCOUNTING_INCOMPLETE", previous_session=previous))
    earlier = [o["order_id"] for o in tx.list("orders")
               if o["target_session"] < target and o["status"] in PENDING]
    if earlier:
        issues.append(_issue("PRIOR_ORDERS_PENDING", order_ids=earlier))
    marker = tx.state.get("restate_from")
    if marker and marker < target:
        issues.append(_issue("RESTATEMENT_PENDING", effective_date=marker))
    later = []
    for table in ("cash_entries", "lot_movements", "fills"):
        for row in tx.list(table):
            if row.get("kind") != "INITIAL_CAPITAL" and row.get("effective_date", "") > target:
                later.append({"table": table, "id": row["id"], "date": row["effective_date"]})
    for row in tx.list("tax_items"):
        for item in [row, *row.get("assessments", []), *row.get("payments", [])]:
            day = item.get("effective_date") or item.get("trigger_date") or ""
            if day > target:
                later.append({"table": "tax_items", "id": row["id"], "date": day})
    for row in tx.list("orders"):
        if row["target_session"] > target:
            later.append({"table": "orders", "id": row["id"], "date": row["target_session"]})
    if tx.state.get("settled_through") and tx.state["settled_through"] > target:
        later.append({"table": "account_state", "id": "settled_through", "date": tx.state["settled_through"]})
    if later:
        issues.append(_issue("LATER_FINANCIAL_DEPENDENCY", references=later))

    for case in tx.list("corporate_action_cases"):
        if case.get("status") in {"NO_ENTITLEMENT", "MERGED"}:
            continue
        event, states = case.get("event", {}), case.get("actions", {})
        record = case.get("record_date") or event.get("record_date")
        # Registration on N is a post-trade action, not a pre-open dependency.
        if record and record >= target:
            continue
        if case.get("status") in {"IDENTITY_PENDING", "REVISION_PENDING"}:
            issues.append(_issue("CORPORATE_IDENTITY_PENDING", case_id=case["case_id"],
                                 code=case.get("code")))
            continue
        due = []
        ex = event.get("ex_date")
        cash = event.get("cash_per_share")
        bonus, capital = event.get("bonus_per_share"), event.get("capital_per_share")
        has_shares = bonus is None or capital is None or Decimal(bonus) + Decimal(capital) > 0
        if ex and ex <= target:
            if cash is None or Decimal(cash) > 0:
                due.append(("cash_accrual", ex))
            if has_shares:
                due.append(("share_accrual", ex))
        listed = event.get("share_listing_date")
        if has_shares and listed and listed <= target:
            due.append(("share_delivery", listed))
        for operation, day in due:
            status = states.get(operation, {}).get("state", "PENDING")
            actions.append({"case_id": case["case_id"], "operation": operation,
                            "effective_date": day, "state": status})
            if status not in DONE_ACTIONS:
                issues.append(_issue("PRE_OPEN_ACTION_PENDING", case_id=case["case_id"],
                                     code=case.get("code"), operation=operation))
    return {"target_session": target, "previous_session": previous,
            "settled_through": tx.state.get("settled_through"), "actions": actions,
            "issues": issues, "state": "WAITING_DATA" if issues else "READY"}


def build_execution_view(service, tx, plan_id):
    """One transaction supplies both immutable resources and current guards."""
    from china_stock_sim.ledger.plan_storage import resolve_plan
    plan = resolve_plan(service.root, tx.get("plans", identifier(plan_id, "计划ID")))
    if not plan:
        raise LedgerError("INVALID_INPUT", "冻结计划不存在", plan_id=plan_id)
    issues = []
    snapshot, original = plan.get("account_snapshot"), plan.get("original_resources")
    if not isinstance(snapshot, dict) or account_input_hash(snapshot) != plan.get("account_snapshot_hash"):
        issues.append(_issue("ACCOUNT_SNAPSHOT_UNPROVEN"))
    if not isinstance(original, dict) or original.get("schema") != 1:
        original = {}
        issues.append(_issue("ORIGINAL_RESOURCES_UNPROVEN"))
    elif original.get("account_snapshot_hash") != plan.get("account_snapshot_hash"):
        issues.append(_issue("ORIGINAL_RESOURCES_UNPROVEN"))
    stages = _stages(service, tx, plan)
    issues.extend(stages["issues"])
    orders = sorted(tx.list("orders", plan_id=plan_id), key=lambda o: o["order_index"])
    frozen_orders = {o["order_id"]: o for o in plan.get("orders", [])}
    frozen_reserves = {r["order_id"]: r for r in plan.get("reservations", [])}
    if not frozen_reserves and original:
        frozen_reserves = {r["order_id"]: r for r in original.get("reservations", [])}
    ordered_sides = [o["side"] for o in orders]
    if ordered_sides != sorted(ordered_sides, key=lambda s: s != "SELL"):
        issues.append(_issue("FROZEN_SUBMISSION_ORDER_INVALID"))
    if [o["order_index"] for o in orders] != list(range(len(orders))):
        issues.append(_issue("FROZEN_SUBMISSION_ORDER_INVALID"))

    current_reserves = tx.list("reservations", status="ACTIVE")
    original_lots = {lot["lot_id"]: lot for position in (snapshot or {}).get("positions", [])
                     for lot in position.get("lots", [])}
    old_movements = set(original.get("lot_movement_ids", []))
    changed_lots = set()
    for movement in tx.list("lot_movements"):
        if movement["id"] in old_movements or movement["lot_id"] not in original_lots:
            continue
        if movement.get("reference_id") in frozen_orders and movement.get("kind") == "SELL":
            continue
        case = tx.get("corporate_action_cases", movement.get("reference_id"))
        proven_cost_transfer = (movement.get("kind") == "CORPORATE_COST_TRANSFER"
                                and movement.get("quantity_delta") == 0 and case
                                and case.get("actions", {}).get("share_accrual", {}).get("state") == "APPLIED")
        if not proven_cost_transfer:
            changed_lots.add(movement["lot_id"])
    current_taxes = [t for t in tx.list("tax_items") if t.get("status") != "NOT_TRIGGERED"]
    known_tax = sum(max(0, t["assessed_fen"] - t.get("paid_fen", 0))
                    for t in current_taxes if t.get("assessed_fen") is not None)
    unknown_taxes = [_identity(t) for t in current_taxes if t.get("assessed_fen") is None]
    baseline_taxes = {_identity(t): t for t in original.get("tax_items", [])}
    additional_tax = sum(max(0, t["assessed_fen"] - (baseline_taxes.get(_identity(t), {}).get("assessed_fen") or 0))
                         for t in current_taxes if t.get("assessed_fen") is not None)
    plan_fills = tx.list("fills", plan_id=plan_id)
    spent = sum(-f["net_cash_fen"] for f in plan_fills if f["side"] == "BUY")
    # A tiny odd-lot sale can cost more in minimum commission than its proceeds.
    # Such a sale consumes original cash even though ordinary sale credits cannot
    # enlarge it.
    sale_debits = sum(max(0, -f["net_cash_fen"]) for f in plan_fills if f["side"] == "SELL")
    original_ids = set(original.get("cash_entry_ids", []))
    # Dividends/sale proceeds never enlarge this ceiling. Tax payments are
    # already represented by the frozen liability plus additional assessments.
    extra_debits = sum(-r["amount_fen"] for r in tx.list("cash_entries")
                       if r["id"] not in original_ids and r["amount_fen"] < 0
                       and r.get("reference_id") not in frozen_orders
                       and r["kind"] != "DIVIDEND_TAX")
    original_remaining = max(0, original.get("available_cash_fen", 0) - spent
                             - additional_tax - extra_debits - sale_debits)
    budget_remaining = max(0, plan.get("buy_budget_fen", 0) - spent)
    cash = tx.balance()
    order_views = []
    for order in orders:
        identity = order["order_id"]
        frozen, reserve = frozen_orders.get(identity, {}), tx.get("reservations", identity)
        old_reserve = frozen_reserves.get(identity)
        own_cash = old_reserve.get("cash_fen", 0) if old_reserve else 0
        own_shares = old_reserve.get("shares", 0) if old_reserve else 0
        other_cash = sum(r.get("cash_fen", 0) for r in current_reserves if r["order_id"] != identity)
        same_plan_other_cash = sum(r.get("cash_fen", 0) for r in current_reserves
                                   if r["order_id"] != identity and r["order_id"] in frozen_orders)
        # An unrelated unresolved distribution can constrain account cash, but
        # must not block a legal exit from a different, fully proven security.
        order_issues = deepcopy([i for i in issues if order["side"] == "BUY"
                                 or i.get("kind") not in {"CORPORATE_IDENTITY_PENDING", "PRE_OPEN_ACTION_PENDING"}
                                 or i.get("code") == order["code"]])
        keys = ("code", "side", "quantity", "limit_price", "order_index", "target_session")
        if any(order.get(k) != frozen.get(k) for k in keys):
            order_issues.append(_issue("FROZEN_ORDER_MISMATCH"))
        terminal = order["status"] in TERMINAL
        if not terminal:
            if not old_reserve:
                order_issues.append(_issue("FROZEN_RESERVATION_UNPROVEN"))
            elif not reserve or reserve.get("status") != "ACTIVE" or any(
                    reserve.get(k) != old_reserve.get(k) for k in ("code", "side", "cash_fen", "shares", "lot_allocations")):
                order_issues.append(_issue("ORIGINAL_RESERVATION_CHANGED"))
            if order["side"] == "SELL":
                allocations = old_reserve.get("lot_allocations", []) if old_reserve else []
                if sum(a["quantity"] for a in allocations) != order["quantity"]:
                    order_issues.append(_issue("FROZEN_LOT_ALLOCATION_UNPROVEN"))
                for allocation in allocations:
                    lot = tx.get("position_lots", allocation["lot_id"])
                    old_lot = original_lots.get(allocation["lot_id"])
                    if (not lot or not old_lot or old_lot["quantity"] < allocation["quantity"]
                            or lot["quantity"] < allocation["quantity"] or lot["code"] != order["code"]
                            or lot.get("acquired_date") != old_lot.get("acquired_date")
                            or lot.get("sellable_date") != old_lot.get("sellable_date")
                            or lot["lot_id"] in changed_lots
                            or not lot.get("sellable_date") or lot["sellable_date"] > plan["target_session"]):
                        order_issues.append(_issue("FROZEN_LOT_RESOURCE_CHANGED", lot_id=allocation["lot_id"]))
            else:
                pending_sells = [o["order_id"] for o in orders if o["side"] == "SELL" and o["status"] not in TERMINAL]
                if pending_sells:
                    order_issues.append(_issue("SELL_PHASE_PENDING", order_ids=pending_sells))
                if unknown_taxes:
                    order_issues.append(_issue("UNKNOWN_TAX_LIABILITY", tax_ids=unknown_taxes))
        allowed = min(own_cash, budget_remaining,
                      max(0, original_remaining - same_plan_other_cash),
                      max(0, cash - other_cash - known_tax))
        order_views.append({"order_id": identity, "order": order_projection(order),
            "status": order["status"], "result": result_summary(order.get("result")),
            "own_reserved_cash_fen": own_cash, "own_reserved_shares": own_shares,
            "other_reserved_cash_fen": other_cash,
            "frozen_lot_allocations": deepcopy((old_reserve or {}).get("lot_allocations", [])),
            "original_reservation": old_reserve, "current_reservation": reserve,
            "max_payable_fen": allowed, "issues": [] if terminal else order_issues,
            "can_submit": not terminal and not order_issues})
    guards = {"cash_fen": cash, "reserved_cash_fen": sum(r.get("cash_fen", 0) for r in current_reserves),
              "known_tax_payable_fen": known_tax, "unknown_tax_ids": unknown_taxes,
              "additional_tax_fen": additional_tax, "other_debits_fen": extra_debits,
              "sale_cash_debits_fen": sale_debits,
              "original_remaining_cash_fen": original_remaining,
              "buy_budget_remaining_fen": budget_remaining, "spent_buy_fen": spent,
              "account_revision": tx.state["account_revision"]}
    all_issues = [*issues, *[{**i, "order_id": o["order_id"]} for o in order_views for i in o["issues"]]]
    view = {"account_id": tx.account_id, "plan_id": plan_id, "target_session": plan["target_session"],
            "frozen_plan": plan, "original_resources": deepcopy(original), "commit_guards": guards,
            "orders": order_views, "stages": stages, "account_revision": tx.state["account_revision"],
            "plan_hash": plan["plan_hash"], "issues": all_issues,
            "state": "WAITING_DATA" if all_issues else "READY"}
    view["view_hash"] = digest(view)
    view["snapshot_at"] = tx.now
    return view


def assert_execution_guards(service, tx, plan, order, *, expense_fen=None):
    """Recheck inside the same transaction that will create the fill."""
    view = build_execution_view(service, tx, plan["plan_id"])
    row = next(r for r in view["orders"] if r["order_id"] == order["order_id"])
    if row["issues"]:
        raise LedgerError("WAITING_DATA", "原订单资源或前序阶段待核", issues=row["issues"])
    if order["side"] == "BUY":
        if type(expense_fen) is not int or expense_fen <= 0:
            raise LedgerError("INVALID_INPUT", "买单守卫须提供复算后的实际支出")
        if expense_fen > row["max_payable_fen"]:
            raise LedgerError("WAITING_DATA", "原资金或当前提交守卫不足，保留原单待核",
                              max_payable_fen=row["max_payable_fen"], expense_fen=expense_fen)
    return view


def apply_execution_batch(service, account_id, payload, *, operation_id, expected_revision):
    """Commit each item independently, retaining WAITING semantics on replay."""
    results = payload.get("results")
    if not isinstance(results, list) or not results or any(not isinstance(r, dict) for r in results):
        return {"state": "INVALID_INPUT", "reason": "批量结果须为非空对象列表"}
    identifier(operation_id, "操作ID")
    outcomes, revision = [], expected_revision
    common = {k: v for k, v in payload.items() if k != "results"}
    identities = [(common | r).get("order_id") for r in results]
    if any(not isinstance(oid, str) or not oid for oid in identities) or len(set(identities)) != len(identities):
        return {"state": "INVALID_INPUT", "reason": "每个批量结果须有唯一原订单ID"}
    order_keys = service._read(account_id, lambda tx: {
        oid: tx.get("orders", oid) for oid in identities})
    if any(order is None for order in order_keys.values()):
        return {"state": "INVALID_INPUT", "reason": "批量结果包含不存在的订单"}
    def sort_key(result):
        order = order_keys[(common | result)["order_id"]]
        return order["target_session"], order["plan_id"], order["order_index"]
    results = sorted(results, key=sort_key)
    for result in results:
        item = common | result
        explicit = item.pop("operation_id", None)
        business = {k: v for k, v in item.items()
                    if k not in {"computed_at", "processed_at", "expected_revision", "account_revision"}}
        # New evidence/state gets a new attempt, rather than occupying order's
        # terminal id with the first WAITING response.
        identity = explicit or "execution-attempt:" + digest({"batch": operation_id, "input": business})
        outcome = service._apply_execution(account_id, item, operation_id=identity,
                                            expected_revision=revision)
        outcomes.append(outcome)
        # The first call retains the caller's revision (except idempotent
        # replay accepted by the kernel); subsequent calls use live state.
        revision = service._read(account_id, lambda tx: tx.state["account_revision"])
        if outcome["state"] not in {"APPLIED", "ALREADY_APPLIED", "WAITING_DATA"}:
            break
    def summarize(tx):
        states = []
        for item in results[:len(outcomes)]:
            order = tx.get("orders", (common | item).get("order_id"))
            states.append(order["status"] if order else None)
        complete = len(outcomes) == len(results) and all(s in TERMINAL for s in states)
        successful = all(r["state"] in {"APPLIED", "ALREADY_APPLIED", "WAITING_DATA"} for r in outcomes)
        return {"state": "APPLIED" if complete and successful else "PARTIAL",
                "completion_status": "COMPLETE" if complete and successful else "PARTIAL",
                "results": outcomes, "order_states": states,
                "submission_order": [(common | item)["order_id"] for item in results[:len(outcomes)]],
                "account_revision": tx.state["account_revision"]}
    return service._read(account_id, summarize)
