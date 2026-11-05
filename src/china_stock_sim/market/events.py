"""Corporate-action facts, never account entitlements or cash postings.

BaoStock's ``*Ps`` values are per share. Eastmoney's three distribution ratios
are per TEN shares; the provider's full plan text is an additional unit check.
Missing values stay unknown unless a fully parsed simple plan proves a zero.
"""

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .quality import DataQualityError, normalize_code, normalize_date
from .storage import digest
from .tax_facts import merge_event_tax_facts, normalize_event_tax_facts

SHANGHAI = ZoneInfo("Asia/Shanghai")
AMOUNTS = ("cash_per_share", "bonus_per_share", "capital_per_share")
DATES = ("record_date", "ex_date", "payment_date", "share_listing_date")
AMOUNT_PRECISION_TOLERANCE = Decimal("0.000001")


def aware_time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise DataQualityError("事实时间必须含时区")
    return result.astimezone(SHANGHAI)


def _date(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", value):
        raise DataQualityError("权益日期格式无效")
    # Source datetime columns contain date-only midnight, not a publication time.
    return normalize_date(value[:10])


def _number(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise DataQualityError("权益比例不是数值") from exc
    if isinstance(value, bool) or not number.is_finite() or number < 0:
        raise DataQualityError("权益比例必须是非负有限数")
    return number


def _plan_values(plan: str) -> dict | None:
    """Only accept a complete simple 10-share plan, not arbitrary prose."""
    compact = re.sub(r"\s+", "", plan)
    if compact in {"不分配不转增", "不分配", "不分红不转增"}:
        return dict.fromkeys(AMOUNTS, Decimal(0))
    body = re.split(r"[（(]", compact, maxsplit=1)[0]
    if not body.startswith("10"):
        return None
    tail, position, seen = body[2:], 0, set()
    values = dict.fromkeys(AMOUNTS, Decimal(0))
    mapping = {"派": "cash_per_share", "送": "bonus_per_share",
               "转": "capital_per_share", "转增": "capital_per_share"}
    for match in re.finditer(r"(转增|转|送|派)(\d+(?:\.\d+)?)(?:股|元)?", tail):
        if match.start() != position or mapping[match[1]] in seen:
            return None
        field = mapping[match[1]]
        values[field] = Decimal(match[2]) / 10
        seen.add(field)
        position = match.end()
    return values if seen and position == len(tail) else None


def _finish(event: dict, values: dict, plan: str, fetched_at: str,
            source_identity: object, raw_reference: str | None) -> dict:
    fetched = aware_time(fetched_at)
    parsed = _plan_values(plan)
    inferred, conflicts, conflict_evidence = [], [], []
    for field in AMOUNTS:
        if parsed is not None:
            if values[field] is None:
                values[field] = parsed[field]
                inferred.append(field)
            elif abs(values[field] - parsed[field]) > AMOUNT_PRECISION_TOLERANCE:
                conflicts.append(f"CONFLICT_{field.upper()}")
                conflict_evidence.append({"field": field, "kind": "SOURCE_PLAN_MISMATCH",
                    "structured_value": format(values[field], "f"),
                    "plan_value": format(parsed[field], "f"), "source_plan": plan})
                values[field] = None
        event[field] = format(values[field], "f") if values[field] is not None else None
    publication = event["published_date"]
    event.update(
        event_id=digest({"code": event["code"], "provider": event["provider"],
                         "identity": source_identity}),
        announced_at=None,
        publication_precision="date" if publication else "unknown",
        fetched_at=fetched.isoformat(),
        available_at=fetched.isoformat(),
        raw_reference=raw_reference,
        source_plan=plan,
        inferred_fields=inferred,
        inference_basis="complete_simple_distribution_plan" if inferred else None,
        conflict_evidence=conflict_evidence,
        unit="CNY/share and shares/share",
    )
    # Consolidated financial statements are not a merger. Preserve the source
    # text and any actual action elsewhere in it, including explicit mergers.
    action_text = plan.replace("合并报表", "")
    if (any(word in plan for word in ("吸收合并", "换股合并"))
            or any(word in action_text for word in ("配股", "合并", "缩股", "重组", "差异化"))):
        event["kind"] = "COMPLEX"
    elif parsed is not None and not any(parsed.values()) and not conflicts:
        event["kind"] = "NO_DISTRIBUTION"
    else:
        event["kind"] = "DISTRIBUTION"
    unknown = [field for field in (*AMOUNTS, "published_date", "record_date", "ex_date")
               if event.get(field) is None]
    if values["cash_per_share"] is None or values["cash_per_share"] > 0:
        if event.get("payment_date") is None:
            unknown.append("payment_date")
    if (values["bonus_per_share"] is None or values["capital_per_share"] is None
            or values["bonus_per_share"] + values["capital_per_share"] > 0):
        if event.get("share_listing_date") is None:
            unknown.append("share_listing_date")
    event["unknown_fields"] = sorted(set(unknown))
    event["conflicts"] = conflicts + (["PUBLICATION_DATE_AFTER_FETCH"] if publication
                          and publication > fetched.date().isoformat() else [])
    event["terms_ready"] = all(event[field] is not None for field in AMOUNTS)
    if event.get("record_date") and event.get("ex_date"):
        if event["record_date"] >= event["ex_date"]:
            raise DataQualityError("权益登记日必须早于除权日")
    for field in ("payment_date", "share_listing_date"):
        if event.get(field) and event.get("ex_date") and event[field] < event["ex_date"]:
            raise DataQualityError(f"{field} 早于除权日")
    return normalize_event_tax_facts(event)


def normalize_baostock_events(rows: list[dict], code: str, fetched_at: str, *,
                              source: str = "baostock",
                              raw_reference: str | None = None) -> list[dict]:
    """Retain announced future/undated events; do not filter by ex-date."""
    code, output = normalize_code(code), []
    for row in rows:
        if normalize_code(row.get("code")) != code:
            raise DataQualityError("权益证券代码与请求不符")
        publication = _date(row.get("dividPlanDate"))
        proposal = _date(row.get("dividPlanAnnounceDate"))
        event = {
            "code": code, "provider": source,
            "published_date": publication or proposal or _date(row.get("dividPreNoticeDate")),
            "proposal_date": proposal,
            "record_date": _date(row.get("dividRegistDate")),
            "ex_date": _date(row.get("dividOperateDate")),
            "payment_date": _date(row.get("dividPayDate")),
            "share_listing_date": _date(row.get("dividStockMarketDate")),
            "status": "IMPLEMENTED" if publication else "PROPOSED" if proposal else "UNKNOWN",
        }
        values = dict(zip(AMOUNTS, (_number(row.get(key)) for key in (
            "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")), strict=True))
        finished = _finish(event, values, str(row.get("dividCashStock") or ""), fetched_at,
                           proposal or publication or digest(row), raw_reference)
        finished["source_evidence"] = [{"status": finished["status"],
            "published_date": finished["published_date"], "source_row": dict(row),
            "raw_reference": raw_reference}]
        output.append(finished)
    return _unique(output)


def normalize_eastmoney_events(rows: list[dict], code: str, fetched_at: str, *,
                               raw_reference: str | None = None) -> list[dict]:
    code, output = normalize_code(code), []
    for row in rows:
        if row.get("SECURITY_CODE") != code[2:]:
            raise DataQualityError("权益证券代码与请求不符")
        source_code = row.get("SECUCODE")
        if source_code and source_code != f"{code[2:]}.{code[:2]}":
            raise DataQualityError("权益证券交易所不符")
        progress = str(row.get("ASSIGN_PROGRESS") or "")
        status = ("CANCELLED" if progress == "股东大会否决"
                  or any(s in progress for s in ("取消", "终止")) else
                  "IMPLEMENTED" if progress in {"实施分配", "实施方案"} else
                  "PROPOSED" if progress in {"董事会预案", "董事会决议", "董事会决议通过",
                                              "股东大会通过", "股东大会预案",
                                              "股东大会决议通过", "预披露"} else "UNKNOWN")
        event = {
            "code": code, "provider": "eastmoney",
            "published_date": _date(row.get("NOTICE_DATE")) or _date(row.get("PLAN_NOTICE_DATE")),
            "proposal_date": _date(row.get("PLAN_NOTICE_DATE")),
            "record_date": _date(row.get("EQUITY_RECORD_DATE")),
            "ex_date": _date(row.get("EX_DIVIDEND_DATE")),
            # This public report does not supply payment / new-share listing dates.
            "payment_date": None, "share_listing_date": None, "status": status,
        }
        values = {}
        for field, key in zip(AMOUNTS, ("PRETAX_BONUS_RMB", "BONUS_RATIO", "IT_RATIO"), strict=True):
            number = _number(row.get(key))
            values[field] = number / 10 if number is not None else None
        output.append(_finish(event, values, str(row.get("IMPL_PLAN_PROFILE") or ""), fetched_at,
                              {"report_date": row.get("REPORT_DATE"),
                               "proposal_date": event["proposal_date"],
                               "ex_date": event["ex_date"]}, raw_reference))
    return _unique(output)


def _amount_rounding(field, observations):
    """Accept a finer quoted amount only when every complete plan proves it.

    Closeness alone is not agreement. The chosen value must occur in a source
    numeric field, match all parsed plans, and round to every other quoted value
    at that value's decimal precision. Unknown or conflicted terms stay unknown.
    """
    marker = f"CONFLICT_{field.upper()}"
    if any(item.get(field) is None or marker in item.get("conflicts", [])
           for item in observations):
        return None
    plans = [_plan_values(item.get("source_plan", "")) for item in observations]
    if any(plan is None for plan in plans):
        return None
    agreed = {plan[field] for plan in plans}
    values = [Decimal(item[field]) for item in observations]
    if len(agreed) != 1:
        return None
    value = next(iter(agreed))
    difference = max(abs(quoted - value) for quoted in values)
    if value not in values or difference > AMOUNT_PRECISION_TOLERANCE:
        return None
    try:
        if any(value.quantize(Decimal(1).scaleb(quoted.as_tuple().exponent),
                              rounding=ROUND_HALF_UP) != quoted for quoted in values):
            return None
    except InvalidOperation:
        return None
    resolved = format(value.normalize(), "f")
    note = {"field": field, "basis": "matching_complete_plans_and_decimal_rounding",
            "resolved_value": resolved, "plan_value": resolved,
            "absolute_tolerance": format(AMOUNT_PRECISION_TOLERANCE, "f"),
            "max_absolute_difference": format(difference, "f"),
            "observations": sorted([
                {"provider": item["provider"], "event_id": item["event_id"],
                 "value": item[field], "raw_reference": item.get("raw_reference")}
                for item in observations],
                key=lambda item: (item["provider"], item["event_id"], item["value"]))}
    return resolved, note


def _unique(events):
    groups, output = {}, []
    for event in events:
        groups.setdefault(event["event_id"], {})[digest(event)] = event
    for group in groups.values():
        observations = sorted(group.values(), key=lambda item: (
            item["status"] == "IMPLEMENTED", item.get("published_date") or "", digest(item)))
        if len(observations) == 1:
            output.append(observations[0])
            continue
        combined = dict(observations[-1])
        statuses = {item["status"] for item in observations}
        progression = statuses == {"PROPOSED", "IMPLEMENTED"}
        conflicts = [issue for item in observations for issue in item.get("conflicts", [])]
        evidence = [item for event in observations for item in event.get("conflict_evidence", [])]
        precision_notes = [note for item in observations for note in item.get("precision_notes", [])]
        for field in (*AMOUNTS, *DATES):
            values = {item[field] for item in observations if item.get(field) is not None}
            if field in AMOUNTS:
                values = {format(Decimal(value).normalize(), "f") for value in values}
            marker = f"CONFLICT_{field.upper()}"
            rounding = (_amount_rounding(field, observations)
                        if field in AMOUNTS and len(values) > 1 else None)
            if rounding is not None:
                values = {rounding[0]}
                precision_notes.append(rounding[1])
            if len(values) > 1:
                conflicts.append(marker)
                evidence.append({"field": field, "kind": "SOURCE_STAGE_MISMATCH",
                    "observations": [{"status": item["status"], "value": item.get(field),
                        "published_date": item.get("published_date")} for item in observations]})
            incomplete_terms = progression and field in AMOUNTS and any(
                item.get(field) is None for item in observations)
            if incomplete_terms:
                conflicts.append("STAGE_TERMS_INCOMPLETE")
            combined[field] = (None if marker in conflicts or incomplete_terms else
                               next(iter(values)) if values else None)
        if len(statuses) > 1:
            if progression and not conflicts:
                combined["status"] = "IMPLEMENTED"
            else:
                combined["status"] = "UNKNOWN"
                if not progression:
                    conflicts.append("CONFLICT_EVENT_STATUS")
        if any(item["kind"] == "COMPLEX" for item in observations):
            combined["kind"] = "COMPLEX"
        elif conflicts and combined["kind"] == "NO_DISTRIBUTION":
            combined["kind"] = "DISTRIBUTION"
        combined.update(
            source_evidence=[item for event in observations for item in event.get(
                "source_evidence", [{"status": event["status"],
                    "published_date": event.get("published_date"),
                    "source_plan": event.get("source_plan"),
                    "raw_reference": event.get("raw_reference")}])],
            conflict_evidence=evidence, conflicts=sorted(set(conflicts)),
            precision_notes=precision_notes,
            unknown_fields=sorted({field for item in observations
                for field in item.get("unknown_fields", []) if combined.get(field) is None}
                | {field for field in (*AMOUNTS, *DATES) if combined.get(field) is None}),
            terms_ready=all(combined[field] is not None for field in AMOUNTS),
        )
        combined.update(merge_event_tax_facts(observations))
        output.append(combined)
    return sorted(output, key=lambda e: (e["ex_date"] or "9999-12-31", e["event_id"]))


def event_readiness(event: dict, knowledge_cutoff: str | datetime, *,
                    purpose: str = "adjustment") -> dict:
    """Availability is purpose-specific; an unknown payment date needn't block signals."""
    if purpose not in {"adjustment", "settlement"}:
        raise ValueError("未知权益用途")
    issues = []
    issues.extend(event.get("conflicts", []))
    if aware_time(event["available_at"]) > aware_time(knowledge_cutoff):
        issues.append("EVENT_NOT_YET_AVAILABLE")
    if event.get("status") != "IMPLEMENTED":
        issues.append("EVENT_NOT_IMPLEMENTED")
    if event.get("kind") == "COMPLEX":
        issues.append("COMPLEX_ACTION")
    required = [*AMOUNTS, "ex_date"]
    if purpose == "settlement":
        required.append("record_date")
        if event.get("cash_per_share") is None or Decimal(event["cash_per_share"]) > 0:
            required.append("payment_date")
        if any(event.get(f) is None for f in AMOUNTS[1:]) or any(
                Decimal(event[f]) > 0 for f in AMOUNTS[1:] if event.get(f) is not None):
            required.append("share_listing_date")
    issues.extend(f"UNKNOWN_{key.upper()}" for key in required if event.get(key) is None)
    return {"state": "READY" if not issues else "WAITING_DATA", "issues": sorted(set(issues)),
            "event_id": event["event_id"], "purpose": purpose}


def merge_events(events: list[dict]) -> list[dict]:
    """Join cross-source descriptions, retaining evidence and explicit conflicts.

    Same-day differing distributions require investigation rather than automatic
    summation. Undated proposals remain separate because equivalence is unknown.
    """
    groups = {}
    for event in events:
        event = normalize_event_tax_facts(event)
        key = (event["code"], event.get("ex_date") or event["event_id"])
        groups.setdefault(key, []).append(event)
    output = []
    for key, observations in groups.items():
        combined = dict(observations[0])
        conflicts = [issue for event in observations for issue in event.get("conflicts", [])]
        precision_notes = [note for event in observations for note in event.get("precision_notes", [])]
        for field in (*AMOUNTS, *DATES):
            values = {event[field] for event in observations if event.get(field) is not None}
            if field in AMOUNTS:
                values = {format(Decimal(value).normalize(), "f") for value in values}
            rounding = (_amount_rounding(field, observations)
                        if field in AMOUNTS and len(values) > 1 else None)
            if rounding is not None:
                values = {rounding[0]}
                precision_notes.append(rounding[1])
            if len(values) > 1:
                conflicts.append(f"CONFLICT_{field.upper()}")
            combined[field] = (None if f"CONFLICT_{field.upper()}" in conflicts else
                               next(iter(values)) if values else None)
        statuses = {event["status"] for event in observations}
        if "CANCELLED" in statuses and "IMPLEMENTED" in statuses:
            conflicts.append("CONFLICT_EVENT_STATUS")
        if "IMPLEMENTED" in statuses:
            combined["status"] = "IMPLEMENTED"
        if any(event["kind"] == "COMPLEX" for event in observations):
            combined["kind"] = "COMPLEX"
        elif conflicts and combined["kind"] == "NO_DISTRIBUTION":
            combined["kind"] = "DISTRIBUTION"
        combined.update(
            event_id=digest({"code": key[0], "ex_or_identity": key[1]}),
            provider="merged" if len(observations) > 1 else combined["provider"],
            available_at=max(aware_time(event["available_at"]) for event in observations).isoformat(),
            fetched_at=max(aware_time(event["fetched_at"]) for event in observations).isoformat(),
            evidence=[{"event_id": event["event_id"], "provider": event["provider"],
                       "raw_reference": event.get("raw_reference"),
                       "fetched_at": event["fetched_at"],
                       "terms": {field: event.get(field) for field in (*AMOUNTS, *DATES)},
                       "tax_facts": event["tax_facts"]}
                      for event in observations],
            source_evidence=[item for event in observations
                             for item in event.get("source_evidence", [])],
            conflict_evidence=[item for event in observations
                               for item in event.get("conflict_evidence", [])],
            precision_notes=precision_notes,
            conflicts=sorted(set(conflicts)),
            unknown_fields=sorted({field for event in observations
                                   for field in event.get("unknown_fields", [])
                                   if combined.get(field) is None}
                                  | {field for field in (*AMOUNTS, *DATES)
                                     if combined.get(field) is None}),
        )
        combined["terms_ready"] = all(combined[field] is not None for field in AMOUNTS)
        combined.update(merge_event_tax_facts(observations))
        output.append(combined)
    return sorted(output, key=lambda e: (e["code"], e["ex_date"] or "9999-12-31", e["event_id"]))
