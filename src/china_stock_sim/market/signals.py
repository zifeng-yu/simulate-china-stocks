"""Deterministic causal price inputs and execution-day facts; no trading decisions."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext

from .events import AMOUNTS, aware_time, event_readiness
from .quality import DataQualityError, normalize_bars, normalize_code, normalize_date

METHOD = "causal_ex_reference_chain_v1"


def _available(item):
    provenance = item.get("_provenance", {})
    return (item.get("available_at") or item.get("fetched_at")
            or provenance.get("available_at") or provenance.get("fetched_at"))


def _bar_problem(exc):
    return "INVALID_OR_INCOMPLETE_BAR: " + str(exc)


def _events_for(events, code, start, end, cutoff):
    chosen, issues, by_day = [], [], {}
    for event in events:
        if normalize_code(event["code"]) != code:
            continue
        if event.get("status") == "CANCELLED" or event.get("kind") == "NO_DISTRIBUTION":
            continue
        ex_date = event.get("ex_date")
        if ex_date is None:
            # An undated implemented or complex action cannot be assumed harmless.
            if (event.get("status") == "IMPLEMENTED" or event.get("kind") == "COMPLEX"
                    or event.get("conflicts")):
                issues.append("UNDATED_ACTION")
                issues.extend(event.get("conflicts", []))
            continue
        if not start <= ex_date <= end:
            continue
        readiness = event_readiness(event, cutoff)
        if readiness["state"] != "READY":
            issues.extend(readiness["issues"])
            continue
        signature = tuple(Decimal(event[field]) for field in AMOUNTS)
        if ex_date in by_day:
            previous = by_day[ex_date]
            if signature != previous:
                issues.append("CONFLICTING_SAME_DAY_ACTIONS")
            # Two providers describing the same economics must not double-adjust.
            continue
        by_day[ex_date] = signature
        chosen.append(event)
    return chosen, issues


def _reference(previous_close, events):
    if not previous_close.is_finite() or previous_close <= 0:
        raise DataQualityError("实际前收盘价必须为正的有限数")
    cash = sum((Decimal(event["cash_per_share"]) for event in events), Decimal(0))
    shares = sum((Decimal(event["bonus_per_share"]) + Decimal(event["capital_per_share"])
                  for event in events), Decimal(0))
    reference = (previous_close - cash) / (1 + shares)
    if not reference.is_finite() or reference <= 0:
        raise DataQualityError("权益调整后参考价不为正")
    return reference


def adjusted_series(bars: list[dict], events: list[dict], knowledge_cutoff: str | datetime,
                    *, events_complete: bool = False) -> dict:
    """As-of signal prices, chained using only known distributions in the window.

    Caller attests coverage through the requested sessions with events_complete;
    this is not a claim that a dividend endpoint proves absence of all actions.
    No latest forward-adjustment factor can retroactively change this output.
    """
    cutoff = aware_time(knowledge_cutoff)
    if not bars:
        return {"state": "WAITING_DATA", "rows": [], "issues": ["NO_BARS"], "method": METHOD}
    try:
        code = normalize_code(bars[0]["code"])
        checked = normalize_bars(bars, expected_code=code)
    except (DataQualityError, KeyError, TypeError) as exc:
        return {"state": "WAITING_DATA", "rows": [], "issues": [_bar_problem(exc)],
                "method": METHOD, "knowledge_cutoff": cutoff.isoformat()}
    issues = [] if events_complete is True else ["EVENT_COVERAGE_UNKNOWN"]
    for raw in bars:
        available = _available(raw)
        if available and aware_time(available) > cutoff:
            issues.append("BAR_NOT_YET_AVAILABLE")
        if raw["date"] > cutoff.date().isoformat():
            issues.append("FUTURE_BAR")
    selected, event_issues = _events_for(events, code, checked[0]["date"],
                                        checked[-1]["date"], cutoff)
    issues.extend(event_issues)
    by_date = {}
    for event in selected:
        by_date.setdefault(event["ex_date"], []).append(event)
    sessions = {row["date"] for row in checked}
    if set(by_date) - sessions:
        issues.append("ACTION_ON_MISSING_SESSION")
    # Missing daily observations must not be silently bridged over.
    for previous, current in zip(checked, checked[1:]):
        if current["date"] not in by_date:
            if abs(Decimal(current["preclose"]) - Decimal(previous["close"])) > Decimal("0.011"):
                issues.append("UNEXPLAINED_REFERENCE_CHANGE")
        else:
            try:
                expected = _reference(Decimal(previous["close"]), by_date[current["date"]])
                if abs(Decimal(current["preclose"]) - expected) > Decimal("0.011"):
                    issues.append("EX_REFERENCE_SOURCE_CONFLICT")
            except (DataQualityError, DecimalException, KeyError, TypeError) as exc:
                issues.append("INVALID_ACTION_ADJUSTMENT: " + str(exc))
    if issues:
        return {"state": "WAITING_DATA", "rows": [], "issues": sorted(set(issues)),
                "method": METHOD, "knowledge_cutoff": cutoff.isoformat()}
    rows, value = [], Decimal(checked[0]["close"])
    for index, bar in enumerate(checked):
        close = Decimal(bar["close"])
        action_ids = []
        if index:
            day_events = by_date.get(bar["date"], [])
            reference = _reference(Decimal(checked[index - 1]["close"]), day_events)
            value *= close / reference
            action_ids = [event["event_id"] for event in day_events]
        rows.append({"code": code, "date": bar["date"], "raw_close": bar["close"],
                     "adjusted_close": format(value, "f"), "event_ids": action_ids})
    return {"state": "READY", "rows": rows, "issues": [], "method": METHOD,
            "knowledge_cutoff": cutoff.isoformat(), "anchor": "first_raw_close"}


def _execution_limits(reference, tick, rate):
    """Round final limits once; an unproved low-price rounding order stays unknown."""
    upper = (reference * (1 + rate) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    lower = (reference * (1 - rate) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    if upper - reference < tick or reference - lower < tick:
        if reference % tick:
            return None
        if upper - reference < tick:
            upper = reference + tick
        if reference - lower < tick:
            lower = reference - tick
    return upper, max(tick, lower)


def execution_facts(bar: dict, security: dict, events: list[dict],
                    knowledge_cutoff: str | datetime, *, events_complete: bool = False,
                    rule_context: dict | None = None) -> dict:
    """Return target-day reference/limit facts, never BUY/SELL or cash arithmetic.

    rule_context is an effective, versioned rule supplied by the rule layer.
    It must state limit_rate (or no_price_limit), price_tick, rule_id and
    effective_from. special_status_unknown explicitly prevents a normal-day guess.
    reference_price is the compatible alias of reference_price_selected. The
    formula's unrounded result and the source preclose remain distinct evidence;
    neither an unsupported precision claim nor a rule-context flag resolves them.
    """
    cutoff = aware_time(knowledge_cutoff)
    try:
        checked = normalize_bars([bar])[0]
    except (DataQualityError, KeyError, TypeError) as exc:
        return {"state": "WAITING_DATA", "issues": [_bar_problem(exc)],
                "code": bar.get("code"), "date": bar.get("date"),
                "reference_price": None, "limit_up": None, "limit_down": None,
                "reference_price_exact": None, "reference_price_source": None,
                "reference_price_selected": None, "limit_rate": None,
                "knowledge_cutoff": cutoff.isoformat()}
    code, session = checked["code"], checked["date"]
    issues = [] if events_complete is True else ["EVENT_COVERAGE_UNKNOWN"]
    if normalize_code(security.get("code")) != code:
        raise DataQualityError("证券身份与执行行情不一致")
    for item in (bar, security):
        available = _available(item)
        if available and aware_time(available) > cutoff:
            issues.append("FACT_NOT_YET_AVAILABLE")
    if session > cutoff.date().isoformat():
        issues.append("FUTURE_BAR")
    if not security.get("listed") or not security.get("board"):
        issues.append("SECURITY_IDENTITY_INCOMPLETE")
    if security.get("eligibility_known") is not True:
        issues.append("SECURITY_ELIGIBILITY_UNKNOWN")
    if security.get("listed") and normalize_date(security["listed"]) > session:
        issues.append("SECURITY_NOT_LISTED_ON_SESSION")
    if security.get("delisted") and normalize_date(security["delisted"]) <= session:
        issues.append("SECURITY_DELISTED_ON_SESSION")
    source_status = security.get("source_status")
    if (type(source_status) is not int or source_status not in {0, 1}
            or (source_status == 0 and not security.get("delisted"))):
        issues.append("SECURITY_STATUS_UNKNOWN")
    context = rule_context or {}
    if (security.get("special_status_unknown") is True
            or context.get("special_status_unknown") is True):
        issues.append("SPECIAL_STATUS_UNKNOWN")
    complex_events = [event for event in events if event.get("kind") == "COMPLEX"
                      and normalize_code(event["code"]) == code
                      and event.get("status") != "CANCELLED"
                      and aware_time(event["available_at"]) <= cutoff
                      and (event.get("ex_date") is None or event["ex_date"] >= session)]
    known_complex = [event["event_id"] for event in complex_events]
    # Future risks are facts for the frozen plan's buy policy, not a reason to
    # invalidate today's price reference or prevent an otherwise legal exit.
    if any(event.get("ex_date") is None or event["ex_date"] == session
           for event in complex_events):
        issues.append("KNOWN_COMPLEX_ACTION")
    relevant, event_issues = _events_for(events, code, session, session, cutoff)
    issues.extend(event_issues)
    source_reference = Decimal(checked["preclose"])
    exact_reference = None
    reference = source_reference
    reference_basis = ("source_unadjusted_preclose" if bar.get("previous_close") is not None
                       else "source_unadjusted_preclose_only")
    if relevant:
        previous_close = bar.get("previous_close")
        if previous_close is None:
            issues.append("PREVIOUS_CLOSE_REQUIRED_FOR_EX_DATE")
        else:
            try:
                with localcontext() as arithmetic:
                    arithmetic.prec = 50
                    exact_reference = _reference(Decimal(str(previous_close)), relevant)
                reference = exact_reference
                reference_basis = "known_cash_and_stock_distribution"
                if abs(reference - source_reference) > Decimal("0.011"):
                    issues.append("EX_REFERENCE_SOURCE_CONFLICT")
            except (DataQualityError, DecimalException, KeyError, TypeError) as exc:
                issues.append("INVALID_ACTION_ADJUSTMENT: " + str(exc))
    elif bar.get("previous_close") is not None:
        try:
            previous_close = Decimal(str(bar["previous_close"]))
            if not previous_close.is_finite() or previous_close <= 0:
                raise DataQualityError("实际前收盘价必须为正的有限数")
            if abs(reference - previous_close) > Decimal("0.011"):
                issues.append("UNEXPLAINED_REFERENCE_CHANGE")
        except (DataQualityError, DecimalException, TypeError) as exc:
            issues.append("INVALID_PREVIOUS_CLOSE: " + str(exc))
    required = {"rule_id", "effective_from", "price_tick"}
    if not required.issubset(context):
        issues.append("PRICE_RULE_CONTEXT_MISSING")
    else:
        if normalize_date(context["effective_from"]) > session:
            issues.append("PRICE_RULE_NOT_EFFECTIVE")
        if context.get("effective_to") and normalize_date(context["effective_to"]) < session:
            issues.append("PRICE_RULE_EXPIRED")
    upper = lower = rate = None
    no_limit = context.get("no_price_limit") is True
    if not no_limit and "limit_rate" not in context:
        issues.append("LIMIT_RATE_UNKNOWN")
    if not issues and not no_limit:
        tick, rate = Decimal(str(context["price_tick"])), Decimal(str(context["limit_rate"]))
        if not tick.is_finite() or tick <= 0 or not rate.is_finite() or not 0 < rate < 1:
            raise DataQualityError("价格规则参数无效")
        with localcontext() as arithmetic:
            arithmetic.prec = 50
            limits = _execution_limits(reference, tick, rate)
            source_limits = _execution_limits(source_reference, tick, rate)
        if limits is None or (exact_reference is not None and limits != source_limits):
            issues.append("PRICE_REFERENCE_PRECISION_PENDING")
            reference = None
            reference_basis = "unresolved_reference_precision"
        else:
            upper, lower = limits
            if upper < lower or any(not lower <= Decimal(checked[field]) <= upper
                                    for field in ("open", "high", "low", "close")):
                issues.append("PRICE_OUTSIDE_EFFECTIVE_LIMITS")
    return {
        "state": "READY" if not issues else "WAITING_DATA", "issues": sorted(set(issues)),
        "code": code, "date": session, "open": checked["open"], "close": checked["close"],
        "tradestatus": checked["tradestatus"], "isST": checked["isST"],
        "security": {field: security.get(field) for field in (
            "name", "listed", "delisted", "board", "board_basis", "as_of", "metadata_scope",
            "source_status", "eligibility_known")},
        "reference_price": format(reference, "f") if reference is not None else None,
        "reference_price_selected": format(reference, "f") if reference is not None else None,
        "reference_price_exact": (format(exact_reference, "f")
                                  if exact_reference is not None else None),
        "reference_price_source": format(source_reference, "f"),
        "reference_basis": reference_basis,
        "limit_up": format(upper, "f") if upper is not None else None,
        "limit_down": format(lower, "f") if lower is not None else None,
        "limit_rate": format(rate, "f") if rate is not None and not issues else None,
        "no_price_limit": no_limit, "rule_id": context.get("rule_id"),
        "event_ids": [event["event_id"] for event in relevant],
        "known_complex_event_ids": known_complex,
        "knowledge_cutoff": cutoff.isoformat(),
    }
