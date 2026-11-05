"""Rule-strategy limits and caps backed by the common execution sizing contract."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from china_stock_sim.common.fees import fee_breakdown, integer, number
from china_stock_sim.common.policies import MAX_ORDER_SHARES
from china_stock_sim.common.sizing import bounded_sell_quantity, intent_quantity


def limit_price(close, side, parameters):
    with localcontext() as ctx:
        ctx.prec = 34
        if side not in {"BUY", "SELL"}:
            raise ValueError("限价仅支持BUY/SELL")
        offset = number(parameters["buy_limit_offset" if side == "BUY" else "sell_limit_offset"])
        price = number(close, positive=True) * (1 + offset if side == "BUY" else 1 - offset)
        mode = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
        return max(Decimal("0.01"), price.quantize(Decimal("0.01"), rounding=mode))


def buy_size(code, budget_fen, price, fees, target_session, *, cap=MAX_ORDER_SHARES):
    quantity, evidence = intent_quantity(
        {"code": code, "side": "BUY", "price": price, "budget_fen": budget_fen},
        [], fees, target_session)
    quantity = min(quantity, integer(cap) // 100 * 100, MAX_ORDER_SHARES)
    cost = -fee_breakdown(price, quantity, "BUY", fees, target_session)["net_cash_fen"] \
        if quantity else 0
    return quantity, {**evidence, "quantity": quantity, "maximum_quantity": cap,
                      "committed_cash_fen": cost}


def sell_size(code, position, cap, price, fees, target_session):
    available = integer(position["sellable_shares"])
    cap = min(integer(cap), available, MAX_ORDER_SHARES)
    quantity = bounded_sell_quantity(available, cap)
    if quantity:
        _, evidence = intent_quantity(
            {"code": code, "side": "SELL", "price": price, "quantity": quantity},
            [position], fees, target_session)
    else:
        evidence = {"method": "quantity", "sellable_shares": available}
    return quantity, {**evidence, "unrounded_quantity": str(cap), "floor_quantity": cap,
                      "quantity": quantity, "rounding_reason": "LEGAL_LOT_FLOOR"}
