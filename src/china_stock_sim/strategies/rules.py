"""S1 and S1-LT pure ranking, frozen rebalance targets and daily child intents."""

from copy import deepcopy
from decimal import ROUND_FLOOR, Decimal, localcontext

from china_stock_sim.common.fees import integer, money, number
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS, normalize_parameters
from china_stock_sim.common.strategy_transitions import parent_transition_matches
from china_stock_sim.common.wire import to_wire

from .base import PlanningContext, StrategyDecision
from .features import rank_features, rule_features
from .sizing import buy_size, limit_price, sell_size


def _parameters(context):
    if context.strategy_id not in {"S1", "S1-LT"}:
        raise ValueError("规则策略只支持S1/S1-LT")
    definition = {**STRATEGY_DEFINITIONS[context.strategy_id], "strategy_id": context.strategy_id}
    return normalize_parameters(definition, context.policy["parameters"])


def _positions(context):
    rows = context.account["positions"]
    positions = {row["code"]: row for row in rows if integer(row["total_shares"]) > 0}
    if len({row["code"] for row in rows}) != len(rows):
        raise ValueError("持仓证券重复")
    return positions


def _empty(reason, calculation=None, *, waiting=False, reasons=()):
    calculation = calculation or {}
    if waiting:
        calculation = {**calculation, "initial_evaluated": False}
    return StrategyDecision(state="WAITING_INPUT" if waiting else "READY",
                            no_order_reason=reason, calculation=calculation, reasons=reasons)


def _rank(context, parameters, positions):
    features, skipped, blocking = {}, [], []
    for code in sorted(set(context.securities) | set(positions)):
        item = context.securities.get(code, {})
        in_pool, eligibility = item.get("in_pool"), item.get("eligibility", "UNAVAILABLE")
        known_excluded = in_pool is False or eligibility == "EXCLUDED"
        if known_excluded:
            skipped.append({"code": code, "reason": "OUT_OF_POOL" if in_pool is False
                            else "EXCLUDED", "issues": deepcopy(item.get("issues", []))})
            continue
        try:
            if in_pool is not True or eligibility != "READY":
                raise ValueError("资格未知")
            number(item.get("close"), positive=True)
            features[code] = rule_features(
                item.get("adjusted_close", []), parameters["lookback_sessions"],
                parameters["momentum_skip_sessions"], parameters["volatility_sessions"])
        except (ValueError, ArithmeticError):
            reason = {"code": code, "reason": "UNAVAILABLE",
                      "issues": deepcopy(item.get("issues", []))}
            skipped.append(reason)
            if code in positions:
                blocking.append(reason)
    ranks = rank_features(features, parameters["momentum_weight"],
                          parameters["low_volatility_weight"])
    return ranks, skipped, blocking


def _target_shares(value_fen, close):
    return int((Decimal(value_fen) / 100 / close / 100).to_integral_value(
        rounding=ROUND_FLOOR)) * 100


def _targets(context, parameters, positions, ranking):
    ranks = {row["code"]: row["rank"] for row in ranking}
    count, nav = parameters["target_count"], context.net_assets_fen
    low_turnover = context.strategy_id == "S1-LT"
    target_weight = parameters["trim_target" if low_turnover else "target_weight"]
    target_value = int((Decimal(nav) * number(target_weight)).to_integral_value(rounding=ROUND_FLOOR))
    if low_turnover:
        retained = {code for code in positions if ranks.get(code, 10**9) <= parameters["retain_rank"]}
        entrants = [row["code"] for row in ranking
                    if row["rank"] <= parameters["enter_rank"] and row["code"] not in positions]
        selected = retained | set(entrants[:max(0, count - len(retained))])
    else:
        retained = set()
        selected = {row["code"] for row in ranking[:count]}
    targets, portfolio = [], []
    for code in sorted(selected | set(positions)):
        position = positions.get(code, {})
        current = position.get("total_shares", 0)
        item = context.securities.get(code, {})
        close = number(item.get("close"), positive=True)
        current_value = money(close * current)
        desired_value = target_value if code in selected else 0
        shares = _target_shares(desired_value, close)
        kind = "BUY"
        if code not in selected:
            kind, shares = "EXIT", 0
        elif low_turnover and code in retained:
            if Decimal(current_value) / nav > number(parameters["trim_trigger"]):
                desired_value = int((Decimal(nav) * number(parameters["trim_target"])
                                     ).to_integral_value(rounding=ROUND_FLOOR))
                shares, kind = _target_shares(desired_value, close), "TRIM"
            else:
                shares, desired_value, kind = current, current_value, "RETAIN"
        elif current > shares:
            kind = "TRIM"
        portfolio.append({"code": code, "target_shares": shares,
                          "target_value_fen": desired_value, "current_shares": current,
                          "current_value_fen": current_value, "kind": kind,
                          "rank": ranks.get(code), "reference_close": str(close)})
        if kind == "RETAIN":
            continue
        side = "SELL" if kind in {"EXIT", "TRIM"} else "BUY"
        price = limit_price(close, side, parameters)
        if side == "SELL":
            quantity = max(0, current - shares)
        else:
            quantity, _ = buy_size(code, max(0, desired_value - current_value), price,
                                   context.fee_policy, context.target_session)
        if quantity:
            targets.append({**portfolio[-1], "side": side, "quantity": quantity})
    return targets, portfolio


def _remaining(task):
    """Use original targets and actual fills; reject inconsistent supplied projections."""
    targets = []
    for target in task["targets"]:
        done = integer(task.get("completed_quantities", {}).get(
            f'{target["code"]}:{target["side"]}', 0))
        quantity = integer(target["quantity"], minimum=1)
        if done > quantity:
            raise ValueError("父任务成交量超过目标")
        if quantity > done:
            targets.append({**target, "quantity": quantity - done})
    return targets


def _intents(context, parameters, positions, targets):
    cash = integer(context.account["available_cash_fen"])
    holdings = set(positions)
    maximum = parameters["max_daily_symbols"]
    intents, sizing, skipped, blocking = [], [], [], []
    priority = {"EXIT": 0, "TRIM": 1, "BUY": 2}
    ordered = sorted(targets, key=lambda row: (
        priority[row["kind"]], row.get("rank") or 10**9, row["code"]))
    for target in ordered:
        code, side = target["code"], target["side"]
        item = context.securities.get(code, {})
        if side == "BUY" and (item.get("in_pool") is False
                              or item.get("eligibility") == "EXCLUDED"):
            skipped.append({"code": code, "reason": "BUY_ELIGIBILITY_CHANGED"})
            continue
        if side == "BUY" and (item.get("in_pool") is not True
                              or item.get("eligibility") != "READY"):
            blocking.append({"code": code, "reason": "BUY_ELIGIBILITY_UNKNOWN"})
            continue
        try:
            close = number(item.get("close"), positive=True)
        except ValueError:
            blocking.append({"code": code, "reason": "RAW_CLOSE_UNAVAILABLE"})
            continue
        price = limit_price(close, side, parameters)
        position = positions.get(code, {"code": code, "total_shares": 0, "sellable_shares": 0})
        if side == "SELL":
            cap = min(target["quantity"], max(0, position["total_shares"] - target["target_shares"]))
            quantity, evidence = sell_size(code, position, cap, price,
                                           context.fee_policy, context.target_session)
        else:
            if code not in holdings and len(holdings) >= parameters["target_count"]:
                skipped.append({"code": code, "reason": "HOLDING_SLOTS_FULL"})
                continue
            current_value = money(close * position["total_shares"])
            budget = max(0, min(cash, target["target_value_fen"] - current_value))
            quantity, evidence = buy_size(code, budget, price, context.fee_policy,
                                          context.target_session, cap=target["quantity"])
        sizing.append({"code": code, "side": side, "limit_price": str(price), **evidence})
        if not quantity:
            skipped.append({"code": code, "reason": "NO_LEGAL_QUANTITY"})
            continue
        if maximum and len(intents) >= maximum:
            skipped.append({"code": code, "reason": "DAILY_SYMBOL_LIMIT"})
            continue
        intents.append({"code": code, "side": side, "order_index": len(intents),
                        "reason": target["kind"], "quantity": quantity,
                        "limit_price": str(price)})
        if side == "BUY":
            cash -= evidence["committed_cash_fen"]
            holdings.add(code)
        # An intended sale neither releases a holding slot nor increases cash.
    return intents, sizing, skipped, blocking


def plan(context: PlanningContext) -> StrategyDecision:
    """Produce a pure decision; no account revision, clock, market read or persistence."""
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        parameters = _parameters(context)
        positions = _positions(context)
        calculation = {"parameters": parameters, "initial_evaluated": False,
                       "net_assets_fen": context.net_assets_fen}
        task = context.parent_task
        if task is None and context.initial_evaluated and not context.month_end:
            return _empty("NON_REBALANCE_DAY", calculation)
        if type(context.net_assets_fen) is not int:
            raise ValueError("净资产必须为分整数")
        if context.net_assets_fen <= 0:
            return _empty("NON_POSITIVE_NET_ASSETS", {**calculation, "initial_evaluated": True})
        if task is not None:
            if (task.get("status") in {"EXPIRED", "COMPLETE"}
                    or context.target_session not in task["sessions"]):
                return _empty("REBALANCE_WINDOW_CLOSED", calculation)
            if (task.get("parameters") != parameters
                    and not parent_transition_matches(task, context.policy,
                                                      target_session=context.target_session)):
                raise ValueError("父任务参数与当前账户政策不一致")
            targets = _remaining(task)
            task_id = task["task_id"]
            calculation.update(trigger="MONTH_END_COALESCED" if context.month_end
                               else "CONTINUE_REBALANCE", parent_task_id=task_id,
                               frozen_ranking=deepcopy(task.get("ranking", [])))
        else:
            ranking, skipped, blocking = _rank(context, parameters, positions)
            calculation.update(ranking=to_wire(ranking), exclusions=skipped,
                               percentile_method="(strict_better+0.5*(ties-1))/(n-1)")
            if blocking:
                return _empty("HOLDING_INPUT_UNAVAILABLE", calculation, waiting=True,
                              reasons=tuple(blocking))
            if not ranking and any(row["reason"] == "UNAVAILABLE" for row in skipped):
                return _empty("ALL_CANDIDATES_UNAVAILABLE", calculation, waiting=True,
                              reasons=tuple(skipped))
            try:
                targets, portfolio = _targets(context, parameters, positions, ranking)
            except ValueError:
                return _empty("RAW_CLOSE_UNAVAILABLE", calculation, waiting=True,
                              reasons=({"reason": "RAW_CLOSE_UNAVAILABLE"},))
            calculation.update(trigger="MONTH_END" if context.month_end else "INITIAL_BUILD",
                               initial_evaluated=True, portfolio_targets=portfolio)
            task_id = "rebalance-" + policy_digest({
                "account_id": context.account_id, "target_session": context.target_session,
                "policy": context.policy, "input_bundle": context.input_bundle,
                "targets": targets})[:24] if targets else None
            if targets:
                sessions = context.task_sessions[:parameters["rebalance_window_sessions"]]
                if (len(sessions) != parameters["rebalance_window_sessions"]
                        or sessions[0] != context.target_session
                        or tuple(sorted(set(sessions))) != sessions):
                    return _empty("REBALANCE_CALENDAR_UNAVAILABLE", calculation, waiting=True)
                calculation["new_task"] = {"task_id": task_id, "start_session": sessions[0],
                    "end_session": sessions[-1], "targets": deepcopy(targets),
                    "ranking": to_wire(ranking), "portfolio_targets": deepcopy(portfolio),
                    "parameters": deepcopy(parameters), "initial_evaluated": True}
        intents, sizing, skipped, blocking = _intents(context, parameters, positions, targets)
        calculation.update(sizing=sizing, skipped=skipped)
        if blocking:
            return _empty("TARGET_INPUT_UNAVAILABLE", calculation, waiting=True,
                          reasons=tuple(blocking))
        return StrategyDecision(ordered_intents=tuple(intents),
            no_order_reason=None if intents else "NO_ACTIONABLE_TARGETS",
            calculation=to_wire(calculation), targets=tuple(deepcopy(targets)), task_id=task_id)


class RuleStrategy:
    def plan(self, context: PlanningContext) -> StrategyDecision:
        return plan(context)
