"""Action-specific corporate facts; due dates are not permission to post an account.

No balances, eligibility quantities or tax decisions are inferred here. A READY
action can be in the future, or inapplicable because its amount is explicitly zero.
Consumers must check ``applicable`` and ``operation_date`` before any posting.
"""

from decimal import Decimal, InvalidOperation

from china_stock_sim.market.contracts import aware_time, session_date

EVENT_OPERATIONS = (
    "settlement", "eligibility", "cash_accrual", "share_accrual",
    "cash_payment", "share_delivery",
)
_DATE_FIELD = {
    "eligibility": "record_date", "cash_accrual": "ex_date",
    "share_accrual": "ex_date", "cash_payment": "payment_date",
    "share_delivery": "share_listing_date",
}


def check_operation(operation: str) -> str:
    if operation not in EVENT_OPERATIONS:
        raise ValueError("未知权益动作")
    return operation


def operation_readiness(event: dict, knowledge_cutoff, *, operation: str) -> dict:
    """Return only the facts needed by an action, keeping legacy settlement intact."""
    check_operation(operation)
    if operation == "settlement":
        from china_stock_sim.market.events import event_readiness

        return event_readiness(event, knowledge_cutoff, purpose="settlement")

    issues = list(event.get("conflicts", []))
    if aware_time(event["available_at"]) > aware_time(knowledge_cutoff):
        issues.append("EVENT_NOT_YET_AVAILABLE")
    if event.get("status") != "IMPLEMENTED":
        issues.append("EVENT_NOT_IMPLEMENTED")
    if event.get("kind") not in {"DISTRIBUTION", "NO_DISTRIBUTION"}:
        issues.append("COMPLEX_ACTION" if event.get("kind") == "COMPLEX"
                      else "UNKNOWN_ACTION_KIND")

    amount_fields = (() if operation == "eligibility" else
                     ("cash_per_share",) if operation.startswith("cash_") else
                     ("bonus_per_share", "capital_per_share"))
    amounts = []
    for field in amount_fields:
        value = event.get(field)
        if value is None:
            issues.append(f"UNKNOWN_{field.upper()}")
            amounts.append(None)
            continue
        try:
            number = Decimal(str(value))
            if isinstance(value, bool) or not number.is_finite() or number < 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            issues.append(f"INVALID_{field.upper()}")
            amounts.append(None)
        else:
            amounts.append(number)

    applicable = (None if any(value is None for value in amounts) else
                  any(value > 0 for value in amounts) if amounts else
                  event.get("kind") != "NO_DISTRIBUTION")
    required = list(amount_fields)
    # Explicit zero needs no payment/listing date, and does not authorize a zero
    # posting. Unknown amounts must not take that shortcut.
    if applicable is not False:
        required.append("record_date")
        if operation != "eligibility":
            required.append("ex_date")
        required.append(_DATE_FIELD[operation])
    required = list(dict.fromkeys(required))
    dates = {}
    for field in required:
        if not field.endswith("_date"):
            continue
        value = event.get(field)
        if value is None:
            issues.append(f"UNKNOWN_{field.upper()}")
            continue
        try:
            dates[field] = session_date(value)
        except (ValueError, TypeError):
            issues.append(f"INVALID_{field.upper()}")
    if (dates.get("record_date") and dates.get("ex_date")
            and dates["record_date"] >= dates["ex_date"]):
        issues.append("INVALID_RECORD_EX_DATE_ORDER")
    for field in ("payment_date", "share_listing_date"):
        if dates.get(field) and dates.get("ex_date") and dates[field] < dates["ex_date"]:
            issues.append(f"INVALID_{field.upper()}_ORDER")
    return {
        "state": "READY" if not issues else "WAITING_DATA",
        "issues": sorted(set(issues)), "event_id": event["event_id"],
        "purpose": "event_settlement", "operation": operation,
        "operation_date": dates.get(_DATE_FIELD[operation]),
        "applicable": applicable, "required_fields": required,
    }
