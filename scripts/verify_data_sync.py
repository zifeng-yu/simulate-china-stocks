"""Read-only market-cache acceptance; writes a JSON receipt, never collects or trades."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.market.service import MarketDataService


def brief(result):
    return {"state": result.state, "issues": list(result.issues)}


def verify(root, day, baseline=None):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    market = MarketDataService(root)
    pool = market.pool(day, knowledge_cutoff=now)
    window = market.calendar_operation("window", day, size=121, knowledge_cutoff=now)
    result = {"verified_at": now, "as_of": day, "network_calls": 0, "llm_calls": 0,
              "pool": brief(pool), "window": brief(window), "details": [],
              "scope": "saved_market_facts_and_purpose_readiness_not_trades"}
    codes = [row["code"] for row in pool.data["selected"]] if pool.ready else []
    sessions = window.data if window.ready else []
    result.update(pool_count=len(codes), history_sessions=len(sessions),
                  first_session=sessions[0] if sessions else None,
                  last_session=sessions[-1] if sessions else None)
    fields = ("preclose", "volume", "amount", "tradestatus", "isST")
    for code in codes:
        today = market.prices(code, day, day, knowledge_cutoff=now)
        bars = market.prices(code, sessions[0], sessions[-1], knowledge_cutoff=now) if sessions else today
        current = next((row for row in today.data or [] if row["date"] == day), {})
        events = market.events(code, knowledge_cutoff=now)
        coverage = market.event_coverage(code, sessions[0], day, knowledge_cutoff=now) if sessions else window
        result["details"].append({"code": code, "today": brief(today),
            "today_provider": current.get("_provenance", {}).get("provider"),
            "missing_full_fields": [field for field in fields if current.get(field) is None],
            "history": {**brief(bars), "rows": len(bars.data or [])},
            "coverage": brief(coverage), "events": {**brief(events), "rows": len(events.data or [])},
            "purposes": {purpose: brief(market.readiness(code, day, purpose, knowledge_cutoff=now))
                         for purpose in ("valuation", "llm", "rules", "event_settlement")}})
    result["indices"] = {code: brief(market.index_quote(code, day, knowledge_cutoff=now))
                         for code in ("SH000001", "SZ399001", "SH000300")}
    details = result["details"]
    result["summary"] = {
        "today_price_ready": sum(row["today"]["state"] == "READY" for row in details),
        "today_full_fields": sum(row["today"]["state"] == "READY" and not row["missing_full_fields"]
                                 for row in details),
        "history_rows": sum(row["history"]["rows"] for row in details),
        "history_ready": sum(row["history"]["state"] == "READY" for row in details),
        "coverage_states": dict(Counter(row["coverage"]["state"] for row in details)),
        "event_states": dict(Counter(row["events"]["state"] for row in details)),
        "event_rows": sum(row["events"]["rows"] for row in details),
        "purpose_states": {purpose: dict(Counter(row["purposes"][purpose]["state"] for row in details))
                           for purpose in ("valuation", "llm", "rules", "event_settlement")},
    }
    result["database_integrity"], result["database_hashes"] = {}, {}
    for name in ("market", "ledger"):
        path = root / "data/state" / f"{name}.sqlite3"
        if not path.exists():
            result["database_integrity"][name] = "ABSENT"
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            result["database_integrity"][name] = db.execute("pragma integrity_check").fetchone()[0]
            if name == "market":
                jobs = [json.loads(row[0]) for row in db.execute("select detail from jobs")]
                jobs = [job for job in jobs if job.get("as_of") == day]
                result["jobs"] = [{key: job.get(key) for key in (
                    "job_id", "state", "started_at", "updated_at", "requests_this_run", "issues")}
                    for job in sorted(jobs, key=lambda job: job["started_at"])]
                matching = [job for job in jobs if codes and set(codes) <= set(job.get("requested_codes", []))]
                latest = max(matching, key=lambda job: job["started_at"], default={})
                result["latest_collection_job"] = {key: latest.get(key) for key in (
                    "job_id", "state", "started_at", "updated_at")}
                security_jobs = {row["code"]: row for row in latest.get("securities", [])}
                result["latest_refresh_complete"] = bool(latest) and all(
                    security_jobs.get(code, {}).get("event_refresh_complete") is True
                    and security_jobs[code].get("event_coverage") == "READY"
                    for code in codes)
                later_incomplete = []
                for code in codes:
                    later = [job for job in jobs if code in job.get("requested_codes", [])
                             and job["started_at"] > latest.get("started_at", "")]
                    if not later:
                        continue
                    last = max(later, key=lambda job: job["started_at"])
                    row = next((row for row in last.get("securities", []) if row["code"] == code), {})
                    if last["state"] != "COMPLETE" or (
                            row.get("events") != "NOT_REQUESTED"
                            and row.get("event_refresh_complete") is not True):
                        later_incomplete.append({"code": code, "job_id": last["job_id"], "state": last["state"]})
                result["later_scope_incomplete"] = later_incomplete
                requests = [json.loads(row[0]) for row in db.execute("select detail from requests")]
                start = (baseline or {}).get("captured_at", day + "T00:00:00+08:00")
                requests = [request for request in requests if request.get("started_at", "") >= start]
                result["request_states"] = dict(Counter(row["state"] for row in requests))
                result["pending_requests"] = [row for row in requests if row["state"] not in ("OK", "SUPERSEDED")]
                result["observation_counts"] = dict(db.execute("select kind,count(*) from observations group by kind"))
                if baseline:
                    original = set(baseline["databases"]["market"]["original_observation_ids"])
                    current = {row[0] for row in db.execute("select id from observations")}
                    result["original_observation_ids_retained"] = original <= current
                result["future_price_count"] = db.execute(
                    "select count(*) from observations where kind in ('bar','index') and session>?", (day,)).fetchone()[0]
        result["database_hashes"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if baseline and "ledger" in result["database_hashes"]:
        result["ledger_unchanged"] = result["database_hashes"]["ledger"] == baseline["databases"]["ledger"]["sha256"]
    result["fact_coverage_state"] = "COMPLETE" if (
        bool(codes) and len(sessions) == 121
        and all(row["today"]["state"] == row["history"]["state"] == row["coverage"]["state"] == "READY"
                and row["events"]["state"] in ("READY", "CONFLICT")
                for row in details)
        and all(index["state"] == "READY" for index in result["indices"].values())
    ) else "PARTIAL"
    result["collection_state"] = "COMPLETE" if (
        result["fact_coverage_state"] == "COMPLETE"
        and result.get("latest_collection_job", {}).get("state") == "COMPLETE"
        and result.get("latest_refresh_complete") is True
        and not result.get("later_scope_incomplete")
        and result["database_integrity"].get("market") == "ok"
    ) else "PARTIAL"
    result["note"] = "价格READY不表示全部字段齐全；采集COMPLETE不表示所有用途READY，亦不表示已生成计划或成交。"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    result = verify(args.root.resolve(), args.as_of, baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("as_of", "collection_state", "summary", "indices")}, ensure_ascii=False))
    return 0 if result["collection_state"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
