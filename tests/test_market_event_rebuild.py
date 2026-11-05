"""Offline repair publishes precise revisions without changing past knowledge."""

import copy
import json
import socket
import subprocess

import pytest

from china_stock_sim.market.cli import main
from china_stock_sim.market.event_rebuild import REVISION, rebuild_event_classification
from china_stock_sim.market.events import normalize_eastmoney_events
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.storage import read_json

FETCHED = "2026-11-01T15:00:00+08:00"
BEFORE = "2026-11-01T15:30:00+08:00"
NOW = "2026-11-01T16:00:00+08:00"
DAY = "2026-10-30"
CODE = "SH601166"


def row(code=CODE, *, progress="预披露", plan="现金分红占合并报表口径利润的30%", **changes):
    return {"SECURITY_CODE": code[2:], "SECUCODE": f"{code[2:]}.{code[:2]}",
            "REPORT_DATE": "2026-06-30", "PLAN_NOTICE_DATE": "2026-04-30",
            "NOTICE_DATE": "2026-04-30", "EQUITY_RECORD_DATE": None,
            "EX_DIVIDEND_DATE": None, "PRETAX_BONUS_RMB": None, "BONUS_RATIO": None,
            "IT_RATIO": None, "ASSIGN_PROGRESS": progress, "IMPL_PLAN_PROFILE": plan, **changes}


def seed(repository, code=CODE, *, rows=None, legacy=True, raw_changes=None, batch_changes=None):
    rows = [row(code)] if rows is None else rows
    raw = {"provider": "eastmoney_events", "adapter_version": "1", "fetched_at": FETCHED,
           "fields": sorted({key for item in rows for key in item}), "rows": rows,
           "coverage": {"complete": True, "code": code,
                        "scope": "reported_cash_bonus_capital_distributions"}}
    raw.update(raw_changes or {})
    reference = repository.save_raw(raw)
    records = normalize_eastmoney_events(rows, code, FETCHED, raw_reference=reference)
    if legacy:
        for record in records:
            if record["status"] in {"PROPOSED", "CANCELLED"}:
                record["status"] = "UNKNOWN"
            if "合并报表" in record["source_plan"]:
                record["kind"] = "COMPLEX"
    members = repository.put("event", records, raw)
    batch = {"code": code, "date": DAY, "batch_key": "eastmoney:all",
             "members": members, "scope": "reported_distributions", **(batch_changes or {})}
    batch_id = repository.put("event_batch", [batch], raw, key_fields=["code", "batch_key"])[0]
    return batch_id, members, reference


def repair(repository, codes=(CODE,), **options):
    return rebuild_event_classification(repository.root, codes=codes, as_of=DAY,
                                        clock=lambda: NOW, **options)


def database_rows(repository):
    with repository.connection() as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM observations ORDER BY id")]


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def repository(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline repair attempted network/provider/subprocess access")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return MarketRepository(tmp_path / "project")


def test_preview_is_read_only_and_missing_repository_stays_missing(repository):
    with pytest.raises(CorruptData, match="批次"):
        repair(repository)
    assert not repository.root.exists()
    seed(repository)
    before = files(repository.root)
    plan = repair(repository)
    assert (plan["state"], plan["affected_batches"], plan["affected_events"]) == (
        "REBUILD_REQUIRED", 1, 1)
    assert set(plan["changes"][0]["changes"][0]["fields"]) == {"kind", "status"}
    assert files(repository.root) == before


def test_atomic_repair_preserves_sources_history_snapshot_and_unchanged_members(repository):
    unchanged = row(progress="实施分配", plan="10派1元", REPORT_DATE="2025-12-31",
                    PLAN_NOTICE_DATE="2026-03-01", PRETAX_BONUS_RMB=1,
                    EQUITY_RECORD_DATE="2026-07-01", EX_DIVIDEND_DATE="2026-07-02")
    batch_id, members, reference = seed(repository, rows=[row(), unchanged])
    snapshot = repository.freeze({"scope": "frozen_before_repair"}, [batch_id, *members])
    old_rows = database_rows(repository)
    source_bytes = (repository.root / reference).read_bytes()
    service = MarketDataService(repository.root)
    before = service.events(CODE, knowledge_cutoff=BEFORE).as_dict()
    plan = repair(repository)
    result = repair(repository, apply=True, expected_plan=plan["plan_id"])
    assert result["state"] == "APPLIED"
    after_rows = database_rows(repository)
    assert len(after_rows) == len(old_rows) + 2
    assert set(old_rows).issubset(after_rows)
    assert service.events(CODE, knowledge_cutoff=BEFORE).as_dict() == before
    assert repository.read_snapshot(snapshot["snapshot_id"]) == snapshot
    assert (repository.root / reference).read_bytes() == source_bytes
    latest = repository.select("event_batch", code=CODE)[0]
    updated = repository.by_ids(latest["payload"]["members"])
    new = next(item for item in updated if item["id"] not in members)
    assert len(set(latest["payload"]["members"]) & set(members)) == 1
    assert new["fetched_at"] == FETCHED and new["raw_ref"] == reference
    assert new["payload"]["available_at"] == NOW
    assert new["payload"]["kind"] == "DISTRIBUTION"
    assert new["payload"]["status"] == "PROPOSED"
    assert new["payload"]["normalization"]["revision"] == REVISION
    assert new["payload"]["cash_per_share"] is None
    assert new["payload"]["ex_date"] is None
    assert latest["available_at"] == new["available_at"]
    assert repository.states("jobs") == repository.states("requests") == {}
    after_files = files(repository.root)
    assert repair(repository, apply=True, expected_plan=plan["plan_id"])["state"] == "UNCHANGED"
    assert repair(repository)["state"] == "UNCHANGED"
    assert files(repository.root) == after_files


def test_rejected_plan_leaves_daily_scope_but_remains_in_source_history(repository):
    seed(repository, rows=[row(progress="股东大会否决", plan="10派3.3元", PRETAX_BONUS_RMB=3.3)])
    service = MarketDataService(repository.root)
    assert len(service.event_settlement(CODE, DAY, knowledge_cutoff=BEFORE).data) == 1
    plan = repair(repository)
    repair(repository, apply=True, expected_plan=plan["plan_id"])
    assert service.events(CODE, knowledge_cutoff=NOW).data[0]["status"] == "CANCELLED"
    assert service.event_settlement(CODE, DAY, knowledge_cutoff=NOW).data == []
    assert len(service.event_settlement(CODE, DAY, knowledge_cutoff=BEFORE).data) == 1


def test_true_amount_conflict_and_non_classification_fields_are_preserved(repository):
    seed(repository, rows=[row(progress="股东大会决议通过", plan="10派2元",
                              PRETAX_BONUS_RMB=3)])
    previous = repository.select("event", code=CODE)[0]["payload"]
    plan = repair(repository)
    repair(repository, apply=True, expected_plan=plan["plan_id"])
    current = repository.select("event", code=CODE)[0]["payload"]
    assert current["conflicts"] == ["CONFLICT_CASH_PER_SHARE"]
    for field, value in previous.items():
        if field not in {"status", "available_at"}:
            assert current[field] == value
    assert MarketDataService(repository.root).events(CODE, knowledge_cutoff=NOW).state == "CONFLICT"


@pytest.mark.parametrize("raw_changes,batch_changes", [
    ({"adapter_version": "unknown"}, {}),
    ({"coverage": {"complete": False, "code": CODE}}, {}),
    ({"coverage": {"complete": True, "code": "SH600000"}}, {}),
    ({"coverage": None}, {}),
    ({"coverage": []}, {}),
    ({}, {"normalization": {"revision": "unknown"}}),
    ({}, {"scope": "unknown"}),
    ({}, {"batch_key": "eastmoney:partial"}),
    ({}, {"date": "2026-11-02"}),
    ({}, {"members": []}),
])
def test_unsupported_or_incomplete_batch_rejected_without_writes(repository, raw_changes,
                                                               batch_changes):
    seed(repository, raw_changes=raw_changes, batch_changes=batch_changes)
    before = files(repository.root)
    with pytest.raises((CorruptData, ValueError)):
        repair(repository)
    assert files(repository.root) == before


def test_corrupt_raw_blocks_whole_scope_before_any_publication(repository):
    seed(repository)
    _, _, reference = seed(repository, "SH600000")
    (repository.root / reference).write_text("{}")
    before = database_rows(repository)
    with pytest.raises(CorruptData):
        repair(repository, codes=(CODE, "SH600000"), apply=True, expected_plan="0" * 64)
    assert database_rows(repository) == before


def test_nonclassification_change_is_rejected_instead_of_silently_migrated(repository):
    batch_id, members, reference = seed(repository)
    record = repository.by_ids(members)[0]["payload"]
    record["cash_per_share"] = "123"
    raw = read_json(repository.root / reference)
    changed_id = repository.put("event", [record], raw)[0]
    batch = repository.by_ids([batch_id])[0]["payload"]
    batch["members"] = [changed_id]
    repository.put("event_batch", [batch], raw, key_fields=["code", "batch_key"])
    with pytest.raises(CorruptData, match="分类以外"):
        repair(repository)


@pytest.mark.parametrize("fail_on", [2, 4])
def test_failure_rolls_back_all_events_and_batches(repository, monkeypatch, fail_on):
    seed(repository)
    seed(repository, "SH600000")
    codes = (CODE, "SH600000")
    plan, before = repair(repository, codes=codes), database_rows(repository)
    put, calls = MarketRepository.put, []
    def fail(self, *args, **kwargs):
        calls.append(args[0])
        if len(calls) == fail_on:
            raise RuntimeError("injected publish failure")
        return put(self, *args, **kwargs)
    monkeypatch.setattr(MarketRepository, "put", fail)
    with pytest.raises(RuntimeError, match="injected"):
        repair(repository, codes=codes, apply=True, expected_plan=plan["plan_id"])
    assert database_rows(repository) == before


def test_stale_plan_and_concurrent_writer_rejected(repository):
    seed(repository)
    plan = repair(repository)
    with repository.writer(), pytest.raises(RuntimeError, match="更新任务"):
        repair(repository, apply=True, expected_plan=plan["plan_id"])
    with pytest.raises(ValueError, match="计划已变化"):
        repair(repository, apply=True, expected_plan="0" * 64)
    seed(repository, rows=[row(plan="每股现金分红尚待披露")])
    with pytest.raises(ValueError, match="计划已变化"):
        repair(repository, apply=True, expected_plan=plan["plan_id"])


def test_readers_see_all_old_batches_until_atomic_commit(repository, monkeypatch):
    seed(repository)
    seed(repository, "SH600000")
    codes = (CODE, "SH600000")
    service = MarketDataService(repository.root)
    before = {code: service.events(code, knowledge_cutoff=NOW).as_dict() for code in codes}
    plan = repair(repository, codes=codes)
    put, observations = MarketRepository.put, []
    def watch(self, *args, **kwargs):
        ids = put(self, *args, **kwargs)
        if args[0] == "event_batch":
            observations.append({code: service.events(code, knowledge_cutoff=NOW).as_dict()
                                 for code in codes})
        return ids
    monkeypatch.setattr(MarketRepository, "put", watch)
    repair(repository, codes=codes, apply=True, expected_plan=plan["plan_id"])
    assert observations == [before, before]
    assert all(service.events(code, knowledge_cutoff=NOW).data[0]["kind"] == "DISTRIBUTION"
               for code in codes)


def test_consistent_empty_batch_is_noop(repository):
    seed(repository, rows=[])
    before = files(repository.root)
    assert repair(repository)["state"] == "UNCHANGED"
    assert files(repository.root) == before


def test_cross_code_member_is_rejected(repository):
    _, other, _ = seed(repository, "SH600000")
    seed(repository, batch_changes={"members": other})
    with pytest.raises(CorruptData, match="成员"):
        repair(repository)


def test_duplicate_members_are_rejected(repository):
    batch_id, members, reference = seed(repository)
    batch = copy.deepcopy(repository.by_ids([batch_id])[0]["payload"])
    batch["members"] = members * 2
    repository.put("event_batch", [batch], read_json(repository.root / reference),
                   key_fields=["code", "batch_key"])
    with pytest.raises(CorruptData, match="重复"):
        repair(repository)


@pytest.mark.parametrize("options", [
    {"codes": []}, {"codes": "SH601166"}, {"codes": ["garbage"]},
    {"codes": [CODE], "apply": True},
    {"codes": [CODE], "apply": True, "expected_plan": "bad"},
    {"codes": [CODE], "expected_plan": "0" * 64},
    {"codes": [CODE], "apply": "yes"},
])
def test_invalid_explicit_scope_and_apply_options_create_nothing(repository, options):
    with pytest.raises(ValueError):
        rebuild_event_classification(repository.root, as_of=DAY, clock=lambda: NOW, **options)
    assert not repository.root.exists()


def test_cli_default_preview_and_explicit_apply(repository, capsys):
    seed(repository)
    args = ["--root", str(repository.root), "data-rebuild-events", "--as-of", DAY,
            "--codes", CODE]
    before = database_rows(repository)
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert database_rows(repository) == before
    assert main([*args, "--apply"]) == 2
    capsys.readouterr()
    assert main([*args, "--apply", "--expected-plan", plan["plan_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "APPLIED"
