"""Repeated successful source consumption must not manufacture coverage knowledge."""

import os
from datetime import datetime, timedelta

import pytest
from test_market_pending_event_ranges import CODE, run, wanted
from test_market_pending_event_ranges import environment as environment

from china_stock_sim.market.contracts import PendingEventRange
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.statement_fingerprint import fingerprint
from china_stock_sim.market.storage import read_json
from china_stock_sim.market.update import _put_event_coverage

DAY = "2026-10-27"
NOW = datetime.fromisoformat(DAY + "T20:00:00+08:00")
FIELDS = ["code", "purpose", "start", "end"]


def payload():
    return {"code": CODE, "date": "2026-10-26", "purpose": "events",
            "start": "2026-10-26", "end": "2026-10-26", "complete": True,
            "refreshed_as_of": "2026-10-26",
            "scope": "published_cash_bonus_capital_distributions",
            "complex_action_absence_proved": False}


def sources():
    return [{"provider": name, "adapter_version": "1",
             "fetched_at": DAY + "T18:30:00+08:00", "rows": []}
            for name in ("baostock", "eastmoney")]


def facts(repository):
    with repository.connection() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT * FROM observations ORDER BY id")]


def raw_files(repository):
    return {path.name: path.read_bytes() for path in repository.objects.glob("*.json")}


def dependency(repository, start, end, cutoff):
    result = fingerprint(repository, "event_coverage", {
        "code": CODE, "start": start, "end": end, "knowledge_cutoff": cutoff})
    assert result["state"] == "READY"
    return result["fingerprint"]


def test_three_cached_runs_reuse_exact_facts_sources_and_statement_dependencies(environment):
    root, clock, calls, _ = environment
    request = wanted(PendingEventRange(CODE, DAY, DAY))
    assert run(environment, request)["state"] == "COMPLETE"
    repository = MarketRepository(root)
    initial_facts, initial_raw = facts(repository), raw_files(repository)
    first = MarketDataService(root).event_coverage(CODE, DAY, DAY, knowledge_cutoff=clock())
    original_fingerprint = dependency(repository, DAY, DAY, clock())
    assert len(calls) == 2 and first.ready
    for _ in range(3):
        clock.advance(900)
        # run constructs another updater/repository: no process-local dedup authority.
        result = run(environment, request)
        assert result["state"] == "COMPLETE" and result["requests_this_run"] == 0
        assert len(calls) == 2
        assert facts(repository) == initial_facts and raw_files(repository) == initial_raw
        assert MarketDataService(root).event_coverage(
            CODE, DAY, DAY, knowledge_cutoff=clock()).as_dict() == first.as_dict()
        assert dependency(repository, DAY, DAY, clock()) == original_fingerprint


def test_real_same_value_acquisition_keeps_new_provenance_and_old_cutoff(environment):
    root, clock, calls, _ = environment
    request = wanted(PendingEventRange(CODE, DAY, DAY))
    assert run(environment, request)["state"] == "COMPLETE"
    repository, service = MarketRepository(root), MarketDataService(root)
    cutoff = clock()
    old_coverage = service.event_coverage(CODE, DAY, DAY, knowledge_cutoff=cutoff).as_dict()
    old_events = service.events(CODE, knowledge_cutoff=cutoff).as_dict()
    old_facts, old_raw = facts(repository), raw_files(repository)
    before = dependency(repository, DAY, DAY, cutoff)
    clock.advance(60)
    result = run(environment, request, refresh=True)
    assert result["state"] == "COMPLETE" and result["requests_this_run"] == 2
    assert len(calls) == 4
    current = service.event_coverage(CODE, DAY, DAY, knowledge_cutoff=clock()).as_dict()
    assert current["state"] == old_coverage["state"] == "READY"
    assert current["issues"] == old_coverage["issues"] == []
    assert current["observation_ids"] != old_coverage["observation_ids"]
    assert len(facts(repository)) == len(old_facts) + 3  # two complete batches and coverage
    assert len(raw_files(repository)) == len(old_raw) + 3
    assert set(old_facts) <= set(facts(repository))
    assert all(raw_files(repository)[name] == raw for name, raw in old_raw.items())
    assert dependency(repository, DAY, DAY, clock()) != before
    assert service.event_coverage(CODE, DAY, DAY, knowledge_cutoff=cutoff).as_dict() == old_coverage
    assert service.events(CODE, knowledge_cutoff=cutoff).as_dict() == old_events


@pytest.mark.parametrize("change", [
    {"start": "2026-10-25"}, {"refreshed_as_of": DAY}, {"complete": False},
    {"scope": "different_scope"}, {"complex_action_absence_proved": True},
    {"future_contract_field": "kept"},
])
def test_changed_coverage_contract_never_collapses_into_old_proof(tmp_path, change):
    repository = MarketRepository(tmp_path)
    first = _put_event_coverage(repository, payload(), sources(), assembled_at=NOW)
    updated = {**payload(), **change}
    second = _put_event_coverage(repository, updated, sources(),
                                 assembled_at=NOW + timedelta(seconds=1))
    assert second != first
    assert repository.by_ids([first])[0]["payload"] == payload()
    assert repository.by_ids([second])[0]["payload"] == updated


@pytest.mark.parametrize("change", [
    {"provider": "different_aggregator"}, {"adapter_version": "2"},
    {"unrecognized_contract": True},
])
def test_changed_synthetic_source_contract_is_not_reused(tmp_path, change):
    repository = MarketRepository(tmp_path)
    refs = [repository.save_raw(raw) for raw in sources()]
    old_raw = {"provider": "distribution_sources", "adapter_version": "1",
               "dependencies": refs, "fetched_at": NOW.isoformat(), **change}
    original = repository.put("coverage", [payload()], old_raw, key_fields=FIELDS)[0]
    current = _put_event_coverage(repository, payload(), sources(),
                                  assembled_at=NOW + timedelta(seconds=1))
    assert current != original
    assert repository.by_ids([original])[0]["raw_ref"] != repository.by_ids([current])[0]["raw_ref"]


def test_only_latest_proof_is_reusable_and_new_assembly_is_not_backdated(tmp_path):
    repository = MarketRepository(tmp_path)
    first = _put_event_coverage(repository, payload(), sources(), assembled_at=NOW)
    middle = _put_event_coverage(repository, {**payload(), "complete": False}, sources(),
                                 assembled_at=NOW + timedelta(seconds=1))
    latest_time = NOW + timedelta(seconds=2)
    third = _put_event_coverage(repository, payload(), sources(), assembled_at=latest_time)
    assert len({first, middle, third}) == 3
    current = repository.by_ids([third])[0]
    assert datetime.fromisoformat(current["available_at"]) == latest_time
    assert datetime.fromisoformat(current["fetched_at"]) == latest_time
    before = facts(repository), raw_files(repository)
    assert _put_event_coverage(repository, payload(), sources(),
                               assembled_at=NOW + timedelta(seconds=3)) == third
    assert (facts(repository), raw_files(repository)) == before
    with pytest.raises(CorruptData, match="组装时间"):
        _put_event_coverage(repository, payload(), sources(), assembled_at=NOW)
    assert (facts(repository), raw_files(repository)) == before


@pytest.mark.parametrize("target,damage", [
    ("coverage", "missing"), ("coverage", "same_size"),
    ("leaf", "missing"), ("leaf", "same_size"), ("row", "checksum"),
])
def test_damaged_latest_proof_fails_closed_without_fallback_or_new_facts(tmp_path, target, damage):
    repository = MarketRepository(tmp_path)
    original = _put_event_coverage(repository, payload(), sources(), assembled_at=NOW)
    row = repository.by_ids([original])[0]
    before = facts(repository)
    old_fingerprint = dependency(repository, "2026-10-26", "2026-10-26", NOW)
    if target == "row":
        with repository.connection(write=True) as connection:
            connection.execute("UPDATE observations SET checksum=? WHERE id=?", ("bad", original))
        before = facts(repository)
    else:
        reference = (row["raw_ref"] if target == "coverage" else
                     read_json(repository.root / row["raw_ref"])["dependencies"][0])
        path = repository.root / reference
        if damage == "missing":
            path.unlink()
        else:
            stat = path.stat()
            content = path.read_bytes()
            assert b"baostock" in content or b"distribution_sources" in content
            content = content.replace(b"baostock", b"baostocx").replace(
                b"distribution_sources", b"distribution_sourceX")
            assert len(content) == stat.st_size
            path.write_bytes(content)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    altered_raw = raw_files(repository)
    with pytest.raises(CorruptData):
        _put_event_coverage(repository, payload(), sources(),
                            assembled_at=NOW + timedelta(seconds=1))
    assert facts(repository) == before and raw_files(repository) == altered_raw
    current = fingerprint(repository, "event_coverage", {"code": CODE,
        "start": "2026-10-26", "end": "2026-10-26", "knowledge_cutoff": NOW})
    assert current["state"] == "CORRUPT" or current["fingerprint"] != old_fingerprint
    assert MarketDataService(tmp_path).event_coverage(
        CODE, "2026-10-26", "2026-10-26", knowledge_cutoff=NOW).state == "CORRUPT"


@pytest.mark.parametrize("bad_time", ["2026-10-26T18:09:59+08:00", "2026-10-27T20:00:01+08:00"])
def test_unfresh_or_future_leaves_cannot_create_coverage(tmp_path, bad_time):
    repository = MarketRepository(tmp_path)
    bad_sources = [{**sources()[0], "fetched_at": bad_time}, sources()[1]]
    with pytest.raises(CorruptData, match="新鲜度或知识时点"):
        _put_event_coverage(repository, payload(), bad_sources, assembled_at=NOW)
    assert repository.select("coverage") == []


def test_explicit_repair_reacquires_sources_and_appends_after_damaged_proof(environment):
    root, clock, calls, _ = environment
    request = wanted(PendingEventRange(CODE, DAY, DAY))
    assert run(environment, request)["state"] == "COMPLETE"
    repository = MarketRepository(root)
    original = repository.select("coverage")[0]
    path = root / original["raw_ref"]
    path.write_bytes(b"damaged immutable coverage")
    clock.advance(60)
    with pytest.raises(CorruptData):
        run(environment, request)
    assert len(calls) == 2
    assert len(repository.select("coverage", verify=False, latest=False)) == 1
    repaired = run(environment, request, repair=True)
    assert repaired["state"] == "COMPLETE" and repaired["requests_this_run"] == 2
    assert len(calls) == 4 and repaired["repairs"]
    assert len(repository.select("coverage", verify=False, latest=False)) == 2
    assert repository.select("coverage")[0]["id"] != original["id"]
    assert MarketDataService(root).event_coverage(CODE, DAY, DAY, knowledge_cutoff=clock()).ready
    with pytest.raises(CorruptData):
        repository.by_ids([original["id"]])
