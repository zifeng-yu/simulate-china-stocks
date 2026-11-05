"""Evidence-backed supplementary tax facts, without tax law or account decisions.

The configured free distribution reports do not supply these fields. Reading an
older cached event adds explicit unknowns; it never changes the original record.
A future source adapter can supply canonical ``tax_facts`` with cited evidence.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .quality import DataQualityError, normalize_date
from .storage import digest

TAX_FIELDS = ("share_face_value", "capital_source", "tax_basis_note")
CAPITAL_SOURCES = frozenset({"STOCK_PREMIUM", "OTHER"})


def tax_field_capabilities(provider: str) -> dict:
    """Adapter capabilities, not a statement about all information on a website."""
    status = ("UNSUPPORTED" if provider in {"baostock", "eastmoney", "eastmoney_events"}
              else "UNKNOWN")
    return {field: status for field in TAX_FIELDS}


def normalize_tax_value(field, value):
    """Canonicalize one directly located source value, without inferring tax law."""
    if field not in TAX_FIELDS:
        raise DataQualityError("未知税基事实字段")
    if field == "share_face_value":
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise DataQualityError("股份面值必须是有证据的正数") from exc
        if isinstance(value, (bool, float)) or not number.is_finite() or number <= 0:
            raise DataQualityError("股份面值必须是精确的正有限数")
        return format(number.normalize(), "f")
    if field == "capital_source":
        if not isinstance(value, str) or value not in CAPITAL_SOURCES:
            raise DataQualityError("资本来源只能为 STOCK_PREMIUM 或 OTHER，未知须用 null")
        return value
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise DataQualityError("税基说明必须是非空的来源文本")
    return value.strip()


def _sources(field, sources):
    if not isinstance(sources, list) or not sources:
        raise DataQualityError("已知税基事实必须有字段来源和适用日期")
    output = {}
    for source in sources:
        if not isinstance(source, dict) or any(
            not isinstance(source.get(key), str) or not source[key].strip()
            for key in ("provider", "source_field", "raw_reference", "event_id", "applicable_date")
        ):
            raise DataQualityError("税基事实来源必须含提供方、字段、原件、事件及适用日期")
        if type(source.get("row_index")) is not int or source["row_index"] < 0:
            raise DataQualityError("税基事实必须明确原件 rows 中的非负整数行号")
        item = {key: source[key] for key in (
            "provider", "source_field", "raw_reference", "event_id", "applicable_date",
            "row_index")}
        item["applicable_date"] = normalize_date(item["applicable_date"])
        item["value"] = normalize_tax_value(field, source.get("value"))
        output[digest(item)] = item
    return [output[key] for key in sorted(output)]


def _finish(event, facts):
    event["tax_facts"] = facts
    issues, conflicts = [], []
    for field, fact in facts.items():
        event[field] = fact["value"]
        if fact["state"] != "READY":
            issue = {"kind": f"TAX_{fact['state']}", "field": field, "reason": fact["reason"]}
            issues.append(issue)
        if fact["state"] == "CONFLICT":
            conflicts.append(f"CONFLICT_{field.upper()}")
    event["tax_issues"], event["tax_conflicts"] = issues, conflicts
    event["tax_readiness"] = {
        "state": "CONFLICT" if conflicts else "WAITING_DATA" if issues else "READY",
        "scope": "all_supplementary_tax_fields", "issues": issues,
    }
    return event


def normalize_event_tax_facts(event: dict) -> dict:
    """Normalize a canonical event without guessing or accepting uncited values.

    Known/conflicting fields carry ``sources`` with per-source values and an exact
    ``row_index``/``source_field`` locator in their immutable raw ``rows``. The
    repository verifies the located value, code, event and applicability date;
    this pure function alone does not certify an external document. This
    function is also the compatibility reader for cached pre-extension events.
    Tax uncertainty stays separate from distribution terms and cash readiness.
    """
    result = deepcopy(event)
    supplied = result.get("tax_facts", {})
    if not isinstance(supplied, dict) or set(supplied) - set(TAX_FIELDS):
        raise DataQualityError("税基事实字段无效")
    capabilities = tax_field_capabilities(result.get("provider", ""))
    facts = {}
    for field in TAX_FIELDS:
        item = supplied.get(field)
        if item is None:
            if result.get(field) is not None:
                raise DataQualityError("税基值缺少字段证据")
            facts[field] = {"state": "UNKNOWN", "value": None, "sources": [],
                            "reason": "SOURCE_UNSUPPORTED" if capabilities[field] == "UNSUPPORTED"
                            else "SOURCE_NOT_DISCLOSED"}
            continue
        if not isinstance(item, dict) or item.get("state") not in {"READY", "UNKNOWN", "CONFLICT"}:
            raise DataQualityError("税基事实状态无效")
        if item["state"] == "UNKNOWN":
            if (item.get("value") is not None or item.get("sources")
                    or result.get(field) is not None):
                raise DataQualityError("未知税基事实不能含已知值或隐藏来源")
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise DataQualityError("未知税基事实必须说明原因")
            facts[field] = {"state": "UNKNOWN", "value": None, "sources": [],
                            "reason": item["reason"]}
        else:
            sources = _sources(field, item.get("sources"))
            if result.get("ex_date") and any(
                source["applicable_date"] != result["ex_date"] for source in sources
            ):
                raise DataQualityError("税基事实适用日期与当前事件除权日期不符")
            values = {source["value"] for source in sources}
            conflict = len(values) > 1
            if (item["state"] == "CONFLICT") != conflict:
                raise DataQualityError("税基状态与来源值不符")
            value = None if conflict else next(iter(values))
            if conflict:
                if item.get("value") is not None:
                    raise DataQualityError("冲突税基值必须保持未知")
            elif normalize_tax_value(field, item.get("value")) != value:
                raise DataQualityError("税基值与字段来源不符")
            if (result.get(field) is not None
                    and normalize_tax_value(field, result[field]) != value):
                raise DataQualityError("税基规范字段与证据不符")
            facts[field] = {"state": "CONFLICT" if conflict else "READY", "value": value,
                            "sources": sources, "reason": "SOURCE_VALUE_CONFLICT" if conflict else None}
    result["tax_field_capabilities"] = capabilities
    return _finish(result, facts)


def merge_event_tax_facts(observations: list[dict]) -> dict:
    """Merge supplemental facts; conflicts never become an arbitrary chosen value."""
    observations = [normalize_event_tax_facts(event) for event in observations]
    merged = {}
    for field in TAX_FIELDS:
        sources = [source for event in observations
                   for source in event["tax_facts"][field]["sources"]]
        if sources:
            sources = _sources(field, sources)
            values = {source["value"] for source in sources}
            conflict = len(values) > 1
            merged[field] = {"state": "CONFLICT" if conflict else "READY",
                             "value": None if conflict else next(iter(values)), "sources": sources,
                             "reason": "SOURCE_VALUE_CONFLICT" if conflict else None}
        else:
            reasons = {event["tax_facts"][field]["reason"] for event in observations}
            merged[field] = {"state": "UNKNOWN", "value": None, "sources": [],
                             "reason": "SOURCE_UNSUPPORTED" if reasons == {"SOURCE_UNSUPPORTED"}
                             else "SOURCE_NOT_DISCLOSED"}
    result = _finish({}, merged)
    result["tax_field_capabilities"] = {
        provider: tax_field_capabilities(provider)
        for provider in sorted({event["provider"] for event in observations})}
    return result
