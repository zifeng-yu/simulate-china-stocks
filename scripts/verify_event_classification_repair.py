"""Read-only production acceptance for the scoped event-classification repair.

Reads the preserved baseline, reviewed plan, apply/repeat receipts and regression
evidence. It never applies a repair; its only write is the requested JSON report.
"""

import argparse
import ast
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True

PREFIX = "event-classification-repair-2026-11-01"
OLD_CHANGES = {
    "src/china_stock_sim/market/events.py",
    "src/china_stock_sim/market/repository.py",
    "src/china_stock_sim/market/cli.py",
    "src/china_stock_sim/market/update.py",
    "tests/test_market_events.py",
}
NEW_FILES = {
    "src/china_stock_sim/market/event_rebuild.py",
    "tests/test_market_event_rebuild.py",
    "tests/test_market_event_rebuild_compat.py",
    "scripts/verify_event_classification_repair.py",
}
EXPECTED_PURPOSES = {
    "valuation": {"READY": 100},
    "llm": {"READY": 99, "WAITING_DATA": 1},
    "rules": {"READY": 97, "WAITING_DATA": 3},
    "event_settlement": {"READY": 80, "WAITING_DATA": 20},
}
SC_TESTS = {
    "SC01": {"tests/test_market_events.py": [
        "test_accounting_phrase_exemption_preserves_actual_complex_actions",
        "test_accounting_phrase_exemption_does_not_hide_a_source_plan_amount_conflict",
    ]},
    "SC02": {"tests/test_market_events.py": [
        "test_eastmoney_progress_mapping_is_exact_and_changes_no_other_facts",
        "test_shareholder_rejection_retains_complex_event_but_excludes_it_from_signals",
        "test_rejected_and_implemented_observations_remain_an_explicit_status_conflict",
    ]},
    "SC03": {"tests/test_market_events.py": [
        "test_accounting_predisclosure_allows_signals_without_inventing_settlement_facts",
        "test_shareholder_approval_without_ex_date_does_not_become_a_settleable_distribution",
        "test_accounting_phrase_exemption_does_not_hide_a_source_plan_amount_conflict",
    ]},
    # Every named test in these two modules is required, including compatibility
    # tests for both ordinary cached collection and preparation re-import.
    "SC04": {"tests/test_market_event_rebuild.py": None,
             "tests/test_market_event_rebuild_compat.py": None},
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(path.read_text())


def brief(result):
    return {"state": result.state, "issues": list(result.issues),
            "observation_ids": list(result.observation_ids)}


def guard(output):
    def audit(event, args):
        if event.startswith("socket.") or event == "subprocess.Popen":
            raise RuntimeError("Acceptance cannot access networks or start processes")
        if event == "sqlite3.connect" and "mode=ro" not in str(args[0]):
            raise RuntimeError("Acceptance permits only SQLite mode=ro")
        if event == "open":
            target, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and (not isinstance(target, (str, bytes, os.PathLike))
                            or Path(target).resolve() != output):
                raise RuntimeError("Only the acceptance JSON may be written")
        if event in {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.link", "os.symlink"}:
            raise RuntimeError("Acceptance cannot mutate other filesystem objects")
    return audit


def verify(args):
    from china_stock_sim.market.contracts import aware_time
    from china_stock_sim.market.event_rebuild import rebuild_event_classification
    from china_stock_sim.market.service import MarketDataService
    from china_stock_sim.market.storage import digest

    root = args.root.resolve()

    def local(reference):
        path = (root / reference).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Evidence must stay inside the project")
        return path

    paths = {key: local(getattr(args, key)) for key in (
        "baseline", "plan", "apply_receipt", "repeat_receipt", "run_receipt",
        "tested_manifest", "junit", "ruff", "output")}
    sys.addaudithook(guard(paths["output"]))
    baseline, plan, applied, repeated, run = (read(paths[key]) for key in (
        "baseline", "plan", "apply_receipt", "repeat_receipt", "run_receipt"))
    if applied.get("state") != "APPLIED":
        raise ValueError("Production apply receipt is not APPLIED; no final report written")
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    day, effective = baseline["as_of"], baseline["new_pool_effective_from"]
    service = MarketDataService(root)
    repository = service.repository
    start_hashes = {name: sha(root / "data/state" / f"{name}.sqlite3")
                    for name in ("market", "ledger")}
    report = {"verified_at": now, "as_of": day, "pool_effective_from": effective,
        "scope": "Production repair acceptance; historical preservation and current semantic readiness",
        "network_calls": 0, "model_calls": 0, "database_writes": 0,
        "browser_review_performed": False, "checks": {}, "failures": [],
        "evidence": {key: {"path": str(path.relative_to(root)), "sha256": sha(path)}
                     for key, path in paths.items() if key != "output"}}

    def check(name, passed, detail=None):
        report["checks"][name] = {"passed": bool(passed)}
        if detail is not None:
            report["checks"][name]["detail"] = detail
        if not passed:
            report["failures"].append(name)

    body = {key: plan[key] for key in ("revision", "as_of", "codes", "basis", "changes")}
    check("reviewed_plan_digest", digest(body) == plan["plan_id"])
    check("reviewed_12_code_24_batch_scope", len(plan["codes"]) == len(set(plan["codes"])) == 12
          and plan["inspected_batches"] == 24 and plan["affected_events"] == 12
          and plan["affected_batches"] == 12 and plan["as_of"] == day)
    check("apply_matches_reviewed_plan", applied.get("applied") is True
          and all(applied.get(key) == plan.get(key) for key in body)
          and applied.get("plan_id") == plan["plan_id"])
    normalized_at = aware_time(applied["normalized_at"])
    apply_run = run.get("apply", {})
    apply_started = run.get("apply_started_at", apply_run.get("started_at", run.get("started_at")))
    apply_finished = run.get("apply_finished_at", apply_run.get("finished_at", run.get("finished_at")))
    apply_exit = run.get("apply_exit_code", apply_run.get("exit_code", run.get("exit_code")))
    check("apply_exit_and_actual_repair_time", apply_exit == 0 and bool(apply_started)
          and bool(apply_finished) and aware_time(apply_started) <= normalized_at <= aware_time(apply_finished)
          and normalized_at > aware_time(baseline["completed_at"]))
    check("apply_began_from_preserved_baseline", run.get("database_sha256_before") == {
        name: baseline["databases"][name]["sha256"] for name in ("market", "ledger")})
    backup_path = local(run["backup_receipt"])
    backup = read(backup_path)
    check("pre_apply_backup_verified", backup.get("source_unchanged") is True
          and backup.get("source_sha256") == baseline["databases"]["market"]["sha256"]
          and backup.get("backup_sha256") == sha(local(backup["backup"]))
          and backup.get("integrity_check") == "ok"
          and backup.get("observation_count") == baseline["databases"]["market"]["observation_count"])
    report["evidence"]["backup_receipt"] = {
        "path": str(backup_path.relative_to(root)), "sha256": sha(backup_path)}
    original = {row[0]: row[1:] for row in baseline["databases"]["market"]["original_observations"]}
    seen, changed_originals, new_ids = set(), [], []
    database_integrity, counts = {}, {}
    with sqlite3.connect(repository.path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM observations ORDER BY id"):
            value = dict(row)
            if value["id"] in original:
                seen.add(value["id"])
                current = [hashlib.sha256(value["payload"].encode()).hexdigest(),
                           value["raw_ref"], digest(value)]
                if current != original[value["id"]]:
                    changed_originals.append(value["id"])
            else:
                new_ids.append(value["id"])
        counts = dict(db.execute("SELECT kind,count(*) FROM observations GROUP BY kind"))
        state_hashes = {table: digest([list(row) for row in db.execute(
            "SELECT * FROM " + table + " ORDER BY 1")]) for table in ("requests", "jobs")}
        database_integrity["market"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    check("all_original_observation_rows_unchanged", seen == set(original) and not changed_originals,
          {"baseline": len(original), "retained": len(seen), "changed_ids": changed_originals})
    check("source_refresh_jobs_and_requests_unchanged",
          state_hashes == baseline["databases"]["market"]["state_table_sha256"])
    additions = repository.by_ids(new_ids)
    added = {row["id"]: row for row in additions}
    check("exact_12_event_12_batch_append", Counter(row["kind"] for row in additions)
          == {"event": 12, "event_batch": 12}
          and {row["code"] for row in additions} == set(plan["codes"]))
    unchanged_kinds = {kind: total for kind, total in counts.items() if kind not in {"event", "event_batch"}}
    check("coverage_prices_pool_and_other_kinds_unchanged", unchanged_kinds == {
        kind: total for kind, total in baseline["databases"]["market"]["observation_counts"].items()
        if kind not in {"event", "event_batch"}})
    check("non_target_early_codes_not_rebuilt",
          not any(row["code"] in {"SH600000", "SZ000001"} for row in additions))
    expected_batches = {item["batch_id"]: item for item in plan["changes"]}
    published = applied.get("published", [])
    check("published_receipt_exact_scope", len(published) == 12
          and {item["previous_batch_id"] for item in published} == set(expected_batches))
    examined = []
    for publication in published:
        old_id, new_id = publication["previous_batch_id"], publication["batch_id"]
        old = repository.by_ids([old_id])[0]
        new = added[new_id]
        replacement = publication["replaced_members"]
        expected = {item["previous_observation_id"]: item for item in expected_batches[old_id]["changes"]}
        check("replacement_scope_" + old["code"], set(replacement) == set(expected)
              and len(set(replacement.values())) == len(replacement)
              and new["kind"] == "event_batch" and new["code"] == old["code"])
        check("complete_batch_members_" + old["code"], new["payload"]["members"] == [
            replacement.get(identity, identity) for identity in old["payload"]["members"]])
        batch_left = {key: value for key, value in old["payload"].items()
                      if key not in {"members", "available_at", "normalization"}}
        batch_right = {key: value for key, value in new["payload"].items()
                       if key not in {"members", "available_at", "normalization"}}
        check("batch_facts_unchanged_" + old["code"], batch_left == batch_right)
        pairs = [(old, new)]
        for previous_id, identity in replacement.items():
            previous = repository.by_ids([previous_id])[0]
            corrected = added[identity]
            left = {key: value for key, value in previous["payload"].items()
                    if key not in {"available_at", "normalization"}}
            right = {key: value for key, value in corrected["payload"].items()
                     if key not in {"available_at", "normalization"}}
            delta = {key: {"before": left.get(key), "after": right.get(key)}
                     for key in left.keys() | right.keys()
                     if left.get(key) != right.get(key) or (key in left) != (key in right)}
            check("only_reviewed_classification_changes_" + identity,
                  corrected["kind"] == "event" and delta == expected[previous_id]["fields"]
                  and set(delta) <= {"kind", "status"})
            pairs.append((previous, corrected))
            examined.append({"code": corrected["code"], "previous_id": previous_id,
                "id": identity, "event_id": corrected["payload"]["event_id"], "changes": delta,
                "fetched_at": corrected["fetched_at"], "available_at": corrected["available_at"],
                "raw_reference": corrected["raw_ref"]})
        for previous, corrected in pairs:
            metadata = corrected["payload"].get("normalization", {})
            check("repair_times_and_provenance_" + corrected["id"],
                  corrected["raw_ref"] == previous["raw_ref"]
                  and corrected["provider"] == previous["provider"]
                  and aware_time(corrected["fetched_at"]) == aware_time(previous["fetched_at"])
                  and aware_time(corrected["available_at"]) == normalized_at
                  and aware_time(corrected["payload"]["available_at"]) == normalized_at
                  and metadata.get("revision") == plan["revision"]
                  and metadata.get("plan_id") == plan["plan_id"]
                  and metadata.get("previous_observation_id") == previous["id"]
                  and aware_time(metadata["normalized_at"]) == normalized_at)
        current = repository.select("event_batch", code=old["code"])
        check("new_batch_is_current_" + old["code"], new_id in {row["id"] for row in current})
    report["repaired_events"] = examined
    report["added_observation_ids"] = sorted(new_ids)
    report["observation_counts"] = counts

    file_protection = {}
    for label, key in (("raw", "raw_file_sha256"), ("snapshots", "snapshot_file_sha256"),
                       ("preparation", "preparation_state_sha256")):
        mismatches = [ref for ref, expected in baseline[key].items()
                      if not local(ref).is_file() or sha(local(ref)) != expected]
        check(label + "_files_byte_preserved", not mismatches, mismatches)
        file_protection[label] = {"files_checked": len(baseline[key]), "mismatches": mismatches}
    actual_raw = {str(path.relative_to(root)) for path in (root / "data/raw").rglob("*") if path.is_file()}
    check("no_source_originals_created_or_deleted", actual_raw == set(baseline["raw_file_sha256"]))
    snapshot_results = {}
    for ref, old_status in baseline["snapshot_readability"].items():
        try:
            loaded = repository.read_snapshot(local(ref).stem)
            valid = loaded == read(local(ref))
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        snapshot_results[ref] = valid
        check("old_snapshot_readable_" + local(ref).stem, old_status["valid"] and valid)
    report["file_protection"] = file_protection
    report["snapshot_readability"] = snapshot_results

    current_events, purpose_details, unchanged_results = {}, [], []
    old_cutoff = baseline["captured_at"]
    for code in baseline["related_codes"]:
        historical = service.events(code, knowledge_cutoff=old_cutoff).as_dict()
        check("old_cutoff_events_" + code, historical == baseline["current_events"][code])
        old_purposes = {purpose: brief(service.readiness(code, day, purpose, knowledge_cutoff=old_cutoff))
                        for purpose in EXPECTED_PURPOSES}
        check("old_cutoff_purposes_" + code, old_purposes == baseline["current_purpose_results"][code])
        current_events[code] = service.events(code, knowledge_cutoff=now).as_dict()
        if code not in plan["codes"]:
            unchanged_results.append(current_events[code] == baseline["current_events"][code])
        if code in baseline["new_pool_codes"]:
            purposes = {purpose: brief(service.readiness(code, day, purpose, knowledge_cutoff=now))
                        for purpose in EXPECTED_PURPOSES}
            purpose_details.append({"code": code, "events": {
                "state": current_events[code]["state"], "issues": current_events[code]["issues"],
                "rows": len(current_events[code]["data"])}, "purposes": purposes})
    check("non_target_current_events_unchanged", all(unchanged_results))
    pools = {session: service.pool(session, knowledge_cutoff=now).as_dict()
             for session in baseline["pools_before"]}
    check("old_and_new_pools_unchanged", pools == baseline["pools_before"])
    summary = {purpose: dict(Counter(row["purposes"][purpose]["state"] for row in purpose_details))
               for purpose in EXPECTED_PURPOSES}
    check("expected_four_purpose_results", summary == EXPECTED_PURPOSES, summary)
    check("only_intended_purpose_states_change", all(
        row["purposes"][purpose]["state"] == baseline["current_purpose_results"][row["code"]][purpose]["state"]
        or (row["code"], purpose) in {("SH601166", "llm"), ("SH601166", "rules"),
                                     ("SZ000063", "event_settlement")}
        for row in purpose_details for purpose in EXPECTED_PURPOSES))
    conflicts = {code: value["issues"] for code, value in current_events.items()
                 if code in baseline["new_pool_codes"] and value["state"] == "CONFLICT"}
    check("real_cash_conflicts_preserved", set(conflicts) == {"SH600900", "SZ002709"}
          and all(conflicts[code] == baseline["current_events"][code]["issues"] for code in conflicts))
    rule_wait = {row["code"]: row["purposes"]["rules"]["issues"] for row in purpose_details
                 if row["purposes"]["rules"]["state"] != "READY"}
    check("remaining_signal_and_reference_limitations", set(rule_wait) == {"SZ002008", "SZ000333", "SH603629"}
          and all(rule_wait[code] == baseline["current_purpose_results"][code]["rules"]["issues"]
                  for code in rule_wait))
    report.update(pools=pools, purpose_summary=summary, details=purpose_details,
                  true_conflicts=conflicts, rule_waits=rule_wait)
    repaired_payloads = [row["payload"] for row in additions if row["kind"] == "event"]
    accounting = [row for row in repaired_payloads if row["code"] == "SH601166"]
    rejected = [row for row in repaired_payloads if row["code"] == "SZ000063"]
    check("SC01_accounting_phrase_correction", len(accounting) == 1
          and accounting[0]["kind"] == "DISTRIBUTION" and "合并报表" in accounting[0]["source_plan"])
    check("SC02_rejected_preserved_but_not_active", len(rejected) == 1
          and rejected[0]["status"] == "CANCELLED"
          and service.event_settlement("SZ000063", day, knowledge_cutoff=now).ready
          and any(row["status"] == "CANCELLED" for row in current_events["SZ000063"]["data"]))
    check("SC03_proposal_not_settleable", service.event_settlement(
        "SH601166", day, knowledge_cutoff=now).state == "WAITING_DATA")

    preview = rebuild_event_classification(root, codes=plan["codes"], as_of=day)
    check("post_apply_read_only_plan_unchanged", preview["state"] == "UNCHANGED"
          and preview["affected_events"] == preview["affected_batches"] == 0)
    repeat_run = run.get("repeat", {})
    repeat_exit = run.get("repeat_exit_code", repeat_run.get("exit_code"))
    repeat_before = run.get("repeat_database_sha256_before", repeat_run.get("database_sha256_before"))
    repeat_after = run.get("repeat_database_sha256_after", repeat_run.get("database_sha256_after"))
    check("production_repeat_unchanged", repeated.get("state") == "UNCHANGED"
          and repeated.get("affected_events") == repeated.get("affected_batches") == 0
          and repeated.get("applied") is False and repeat_exit == 0)
    current_repeat_hash = repeat_after.get("market") if isinstance(repeat_after, dict) else repeat_after
    check("production_repeat_database_byte_unchanged", bool(repeat_before)
          and repeat_before == repeat_after and current_repeat_hash == start_hashes["market"])
    report["idempotence"] = {"repeat_receipt": repeated, "post_apply_preview": preview,
                             "before_sha256": repeat_before, "after_sha256": repeat_after}

    tested = read(paths["tested_manifest"])
    tested_sources = tested["sha256"]
    original_sources = baseline["source_test_config_sha256"]
    changed = {ref for ref, old_sha in original_sources.items()
               if not local(ref).is_file() or sha(local(ref)) != old_sha}
    check("source_changes_exactly_scoped", changed == OLD_CHANGES, sorted(changed))
    check("tested_source_scope", set(tested_sources) == set(original_sources) | NEW_FILES)
    tested_mismatch = [ref for ref, expected in tested_sources.items()
                       if not local(ref).is_file() or sha(local(ref)) != expected]
    check("tested_sources_match_current", not tested_mismatch, tested_mismatch)
    check("all_original_configurations_unchanged", not any(ref.startswith("configs/") for ref in changed))
    xml = ElementTree.parse(paths["junit"]).getroot()
    cases = list(xml.iter("testcase"))
    bad_cases = [case.attrib for case in cases if any(case.find(tag) is not None
                 for tag in ("failure", "error", "skipped"))]
    check("full_regression_all_passed", bool(cases) and not bad_cases, {"tests": len(cases), "bad": bad_cases})
    check("ruff_passed", "All checks passed!" in paths["ruff"].read_text())
    evidence_mapping = {}
    for requirement, modules in SC_TESTS.items():
        evidence_mapping[requirement] = []
        for file, required in modules.items():
            if required is None:
                tree = ast.parse(local(file).read_text())
                required = [node.name for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name.startswith("test_")]
            check(requirement + "_test_module_has_cases_" + Path(file).stem, bool(required))
            for name in required:
                matching = [case for case in cases if case.get("name", "").split("[", 1)[0] == name
                            and case.get("classname", "").split(".")[-1] == Path(file).stem]
                passed = bool(matching) and all(not any(case.find(tag) is not None
                    for tag in ("failure", "error", "skipped")) for case in matching)
                check(requirement + "_test_" + name, passed)
                evidence_mapping[requirement].append({"file": file, "test": name,
                    "executed_cases": len(matching), "passed": passed})
    report["requirements"] = {
        "SC01": {"implementation": ["src/china_stock_sim/market/events.py"],
                 "production_checks": ["SC01_accounting_phrase_correction"],
                 "tests": evidence_mapping["SC01"]},
        "SC02": {"implementation": ["src/china_stock_sim/market/events.py"],
                 "production_checks": ["SC02_rejected_preserved_but_not_active"],
                 "tests": evidence_mapping["SC02"]},
        "SC03": {"implementation": ["src/china_stock_sim/market/events.py"],
                 "production_checks": ["expected_four_purpose_results", "only_intended_purpose_states_change",
                    "real_cash_conflicts_preserved", "remaining_signal_and_reference_limitations",
                    "SC03_proposal_not_settleable"], "tests": evidence_mapping["SC03"]},
        "SC04": {"implementation": ["src/china_stock_sim/market/event_rebuild.py",
                    "src/china_stock_sim/market/repository.py", "src/china_stock_sim/market/update.py",
                    "src/china_stock_sim/market/cli.py"],
                 "production_checks": ["all_original_observation_rows_unchanged", "exact_12_event_12_batch_append",
                    "raw_files_byte_preserved", "snapshots_files_byte_preserved", "preparation_files_byte_preserved",
                    "source_refresh_jobs_and_requests_unchanged", "old_and_new_pools_unchanged",
                    "production_repeat_unchanged", "production_repeat_database_byte_unchanged"],
                 "tests": evidence_mapping["SC04"],
                 "fault_injection_scope": "Failure atomicity, concurrent writer refusal and cached re-consumption are isolated regression tests, not production fault injection."}}
    with sqlite3.connect((root / "data/state/ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        database_integrity["ledger"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    check("both_databases_integrity_ok", set(database_integrity.values()) == {"ok"})
    end_hashes = {name: sha(root / "data/state" / f"{name}.sqlite3") for name in ("market", "ledger")}
    check("ledger_byte_unchanged", end_hashes["ledger"] == baseline["databases"]["ledger"]["sha256"])
    check("verification_itself_changed_no_database", end_hashes == start_hashes)
    # Include every concrete preservation/time/batch check in the SC04 mapping,
    # not only the small subset useful to display in a summary.
    semantic_checks = {
        "SC01_accounting_phrase_correction", "SC02_rejected_preserved_but_not_active",
        "SC03_proposal_not_settleable", "expected_four_purpose_results",
        "only_intended_purpose_states_change", "real_cash_conflicts_preserved",
        "remaining_signal_and_reference_limitations",
    }
    report["requirements"]["SC04"]["production_checks"] = [
        name for name in report["checks"]
        if name not in semantic_checks and not name.startswith(("SC01_test", "SC02_test", "SC03_test", "SC04_test"))]
    for requirement, entry in report["requirements"].items():
        checks = entry["production_checks"]
        entry["status"] = "PASS" if (all(report["checks"][name]["passed"] for name in checks)
            and all(test["passed"] for test in entry["tests"])) else "NOT_COMPLETE"
    report.update(database_integrity=database_integrity, database_hashes=end_hashes,
                  regression={"tests": len(cases), "failed_or_skipped_cases": len(bad_cases)},
                  source_changes=sorted(changed), new_source_files=sorted(NEW_FILES),
                  status="PASS" if not report["failures"] else "NOT_COMPLETE")
    report["note"] = "Classification repair changes neither original source knowledge nor ledger facts. READY remains purpose-specific; remaining missing dates and true conflicts are preserved."
    paths["output"].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "failures", "purpose_summary", "regression")},
                     ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    for option, suffix in (("baseline", "baseline.json"), ("plan", "plan.json"),
                           ("apply-receipt", "apply.json"), ("repeat-receipt", "repeat.json"),
                           ("run-receipt", "run.json"), ("tested-manifest", "tested-source-manifest.json"),
                           ("junit", "pytest.xml"), ("ruff", "ruff.txt"), ("output", "verification.json")):
        parser.add_argument("--" + option, type=Path, default=Path("logs") / f"{PREFIX}-{suffix}")
    return verify(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
