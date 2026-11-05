"""Explicit offline classification repair; immutable evidence and old cutoffs survive.

This deliberately supports only kind/status corrections from existing, complete
source batches. It is not a general migration, refresh, or cache repair facility.
"""

from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import aware_time, session_date
from .events import normalize_baostock_events, normalize_eastmoney_events
from .quality import normalize_code
from .repository import CorruptData, MarketRepository, timestamp
from .storage import digest, read_json

REVISION = "event_classification_20261101"
CLASSIFICATION_FIELDS = {"kind", "status"}


def _comparable(payload):
    result = dict(payload)
    # Repair availability is not source acquisition time. Repeated application
    # compares source-derived facts, not this operation's audit metadata.
    result.pop("available_at", None)
    metadata = result.pop("normalization", None)
    if metadata is not None and (
            not isinstance(metadata, dict) or metadata.get("revision") != REVISION):
        raise CorruptData("未知的权益规范化修订，不能自动重建")
    return result


def _inspect(repository, codes, as_of, now):
    basis, revisions = [], []
    for code in codes:
        batches = repository.select("event_batch", code=code)
        if not batches:
            raise CorruptData(f"{code} 缺少当前完整权益批次")
        for batch in batches:
            payload = batch["payload"]
            if session_date(payload["date"]) > as_of:
                raise ValueError(f"{code} 当前批次晚于指定截止日，不能回退旧批次")
            if aware_time(batch["available_at"]) > now:
                raise ValueError("当前批次可用时间晚于重建时点")
            _comparable(payload)
            if payload.get("scope") != "reported_distributions":
                raise CorruptData("不支持的权益批次范围")
            raw = read_json(repository.root / batch["raw_ref"])
            if (raw.get("adapter_version") != "1" or not isinstance(raw.get("rows"), list)
                    or any(not isinstance(row, dict) for row in raw["rows"])
                    or not isinstance(raw.get("fields"), list)):
                raise CorruptData("不支持或不完整的权益原件结构/适配器版本")
            key = payload.get("batch_key")
            if raw["provider"] == "eastmoney_events" and key == "eastmoney:all":
                coverage = raw.get("coverage", {})
                if (not isinstance(coverage, dict) or coverage.get("complete") is not True
                        or coverage.get("code") != code
                        or coverage.get("scope") != "reported_cash_bonus_capital_distributions"):
                    raise CorruptData("东方财富原件未确认该证券完整来源覆盖")
                normalize = normalize_eastmoney_events
            elif (raw["provider"] == "baostock" and type(payload.get("year")) is int
                  and key == f"baostock:{payload['year']}"):
                normalize = normalize_baostock_events
            else:
                raise CorruptData("不支持的权益来源或批次身份")
            members = payload.get("members")
            if (not isinstance(members, list) or any(not isinstance(i, str) for i in members)
                    or len(members) != len(set(members))):
                raise CorruptData("权益批次成员无效或重复")
            old = repository.by_ids(members)
            if any(item["kind"] != "event" or item["code"] != code
                   or item["raw_ref"] != batch["raw_ref"]
                   or item["provider"] != batch["provider"]
                   or aware_time(item["available_at"]) > aware_time(batch["available_at"])
                   for item in old):
                raise CorruptData("权益批次成员与来源、证券或时点不一致")
            previous = {item["payload"]["event_id"]: item for item in old}
            fresh = normalize(raw["rows"], code, raw["fetched_at"],
                              raw_reference=batch["raw_ref"])
            current = {item["event_id"]: item for item in fresh}
            if (len(previous) != len(old) or len(current) != len(fresh)
                    or previous.keys() != current.keys()):
                raise CorruptData("原件重建的完整事件身份与当前批次不一致")
            changes = []
            for event_id, rebuilt in current.items():
                prior = previous[event_id]
                left, right = _comparable(prior["payload"]), _comparable(rebuilt)
                fields = {field for field in left.keys() | right.keys()
                          if left.get(field) != right.get(field)
                          or (field in left) != (field in right)}
                if fields - CLASSIFICATION_FIELDS:
                    raise CorruptData("重建发现分类以外的变化，拒绝发布: "
                                      + code + ":" + ",".join(sorted(fields)))
                if fields:
                    changes.append({"event_id": event_id, "previous_observation_id": prior["id"],
                        "fields": {field: {"before": left[field], "after": right[field]}
                                   for field in sorted(fields)},
                        "rebuilt": rebuilt})
            basis.append({"code": code, "batch_id": batch["id"], "raw_reference": batch["raw_ref"]})
            if changes:
                if aware_time(batch["available_at"]) >= now:
                    raise ValueError("新修正可用时点必须晚于当前批次")
                revisions.append({"batch": batch, "source": raw, "changes": changes})
    public_changes = [{"code": item["batch"]["code"], "batch_id": item["batch"]["id"],
                       "raw_reference": item["batch"]["raw_ref"],
                       "changes": [{key: value for key, value in change.items() if key != "rebuilt"}
                                   for change in item["changes"]]} for item in revisions]
    body = {"revision": REVISION, "as_of": as_of, "codes": list(codes),
            "basis": basis, "changes": public_changes}
    return {"state": "REBUILD_REQUIRED" if revisions else "UNCHANGED",
            "plan_id": digest(body), **body, "inspected_batches": len(basis),
            "affected_batches": len(revisions),
            "affected_events": sum(len(item["changes"]) for item in revisions),
            "network_calls": 0, "applied": False}, revisions


def rebuild_event_classification(root: Path, *, codes, as_of: str, apply=False,
                                 expected_plan=None, clock=timestamp) -> dict:
    """Preview by default. Explicit application publishes all changes atomically.

    Unchanged input is a no-op even with the plan ID from a prior successful run;
    a changed plan is never accepted when there are new mutations to publish.
    """
    if not isinstance(codes, (list, tuple)) or not codes or len(codes) > 1000:
        raise ValueError("重建必须显式指定1到1000个证券代码")
    codes = tuple(sorted(set(normalize_code(code) for code in codes)))
    as_of = session_date(as_of)
    if type(apply) is not bool:
        raise ValueError("apply必须是布尔值")
    if apply and (not isinstance(expected_plan, str) or len(expected_plan) != 64
                  or any(c not in "0123456789abcdef" for c in expected_plan)):
        raise ValueError("应用重建必须指定有效的--expected-plan计划摘要")
    if not apply and expected_plan is not None:
        raise ValueError("--expected-plan仅用于显式--apply")
    repository = MarketRepository(root)

    def inspect():
        now = aware_time(clock())
        if as_of > now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
            raise ValueError("重建截止日不能在未来")
        plan, revisions = _inspect(repository, codes, as_of, now)
        return now, plan, revisions

    if not apply:
        _, plan, _ = inspect()
        return plan
    with repository.writer():
        now, plan, revisions = inspect()
        if not revisions:
            return plan
        if plan["plan_id"] != expected_plan:
            raise ValueError("重建计划已变化，请重新检查计划摘要")
        normalized_at = now.isoformat()
        published = []
        with repository.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in revisions:
                batch, raw = item["batch"], item["source"]
                replaced = {}
                for change in item["changes"]:
                    previous_id = change["previous_observation_id"]
                    record = {**change["rebuilt"], "available_at": normalized_at,
                        "normalization": {"revision": REVISION, "normalized_at": normalized_at,
                            "previous_observation_id": previous_id, "plan_id": plan["plan_id"]}}
                    identity = repository.put("event", [record], raw, connection=connection)[0]
                    replaced[previous_id] = identity
                payload = {**batch["payload"], "available_at": normalized_at,
                    "members": [replaced.get(identity, identity)
                                for identity in batch["payload"]["members"]],
                    "normalization": {"revision": REVISION, "normalized_at": normalized_at,
                        "previous_observation_id": batch["id"], "plan_id": plan["plan_id"],
                        "replaced_members": replaced}}
                identity = repository.put("event_batch", [payload], raw,
                    key_fields=["code", "batch_key"], connection=connection)[0]
                published.append({"code": batch["code"], "previous_batch_id": batch["id"],
                                  "batch_id": identity, "replaced_members": replaced})
        return {**plan, "state": "APPLIED", "applied": True,
                "normalized_at": normalized_at, "published": published}
