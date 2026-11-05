"""Pure order sizing shared by plan validation and ledger intent binding."""

from decimal import ROUND_FLOOR, Decimal

from .fees import ExecutionSupportError, fee_breakdown, integer, number
from .policies import MAX_ORDER_SHARES


def _buy_quantity(budget, price, fees, target):
    low, high = 0, MAX_ORDER_SHARES // 100
    while low < high:
        mid = (low + high + 1) // 2
        cost = -fee_breakdown(price, mid * 100, "BUY", fees, target)["net_cash_fen"]
        if cost <= budget:
            low = mid
        else:
            high = mid - 1
    return low * 100


def _sell_quantity(available, fraction):
    raw = int((Decimal(available) * fraction).to_integral_value(rounding=ROUND_FLOOR))
    return bounded_sell_quantity(available, raw), raw


def bounded_sell_quantity(available, cap):
    """Largest legal sale at or below an integer cap, without fractional rounding."""
    available, cap = integer(available), integer(cap)
    raw = min(available, cap)
    whole, odd = raw // 100 * 100, available % 100
    remainder = ((raw - odd) // 100 * 100 + odd) if odd and raw >= odd else 0
    return max(whole, remainder)


def intent_quantity(intent, positions, fees, target):
    """Return (quantity, sizing evidence) for a normalized BUY/SELL intent.

    The caller supplies its normalized security code and Decimal ``price``.
    Quantity conversion is identical at validation and final ledger binding.
    """
    if not isinstance(intent, dict):
        raise ExecutionSupportError("INVALID_INPUT", "数量计算意图须为对象")
    side, requested = intent.get("side"), intent.get("quantity")
    if side not in ("BUY", "SELL"):
        raise ExecutionSupportError("INVALID_INPUT", "数量计算仅接受买入或卖出意图")
    if requested is not None:
        requested = integer(requested, minimum=1)
        if requested > MAX_ORDER_SHARES:
            raise ExecutionSupportError("INVALID_PLAN", "显式订单超过100万股，不自动拆分")
    if side == "BUY":
        budget = intent.get("budget_fen")
        if (requested is None) == (budget is None) or intent.get("sell_fraction") is not None:
            raise ExecutionSupportError("INVALID_INPUT", "买入意图须明确股数或含费预算其中一种")
        if requested is not None and requested % 100:
            raise ExecutionSupportError("INVALID_PLAN", "显式买入数量须为100股整手")
        q = (requested if requested is not None else _buy_quantity(
            integer(budget), number(intent["price"], positive=True), fees, target
        ))
        return q, {
            "method": "quantity" if requested is not None else "budget",
            "budget_fen": budget,
            "lot_size": 100,
            "quantity": q,
        }
    fraction_value = intent.get("sell_fraction")
    if (requested is None) == (fraction_value is None) or intent.get("budget_fen") is not None:
        raise ExecutionSupportError("INVALID_INPUT", "卖出意图须明确股数或可卖比例其中一种")
    if not isinstance(positions, (list, tuple)) or any(not isinstance(p, dict) for p in positions):
        raise ExecutionSupportError("INVALID_INPUT", "持仓须为对象列表")
    rows = [p for p in positions if p["code"] == intent["code"]]
    if len(rows) != 1:
        raise ExecutionSupportError("INVALID_PLAN", "卖出证券没有唯一持仓")
    available = integer(rows[0]["sellable_shares"])
    if requested is None:
        fraction = number(fraction_value, positive=True)
        if fraction > 1:
            raise ExecutionSupportError("INVALID_INPUT", "卖出比例须大于0且不超过1")
        q, raw = _sell_quantity(available, fraction)
    else:
        q, raw = requested, requested
    if q > MAX_ORDER_SHARES or q > available or (q % 100 and q % 100 != available % 100):
        raise ExecutionSupportError("INVALID_PLAN", "卖出数量超可卖量/单笔上限或不符合零股规则")
    return q, {
        "method": "quantity" if requested is not None else "sell_fraction",
        "sellable_shares": available,
        "sell_fraction": fraction_value,
        "unrounded_quantity": format(Decimal(available) * fraction, "f")
        if requested is None else str(raw),
        "floor_quantity": raw,
        "quantity": q,
        "rounding_reason": "LEGAL_LOT_FLOOR",
    }
