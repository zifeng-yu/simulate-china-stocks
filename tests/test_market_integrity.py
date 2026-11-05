"""Regression boundaries for source integrity, cancellations and server cooldowns."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_market_service import populate, source
from test_market_update import AS_OF, Clock, FakeSource, collect, request

from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.providers.http import HTTPProviderError
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService


@pytest.mark.parametrize("tampered", ["2024-02-07T10:00:00+00:00", "2024-02-08T11:00:00+00:00"])
def test_index_available_time_tampering_is_corrupt_not_ready(tmp_path, tampered):
    repository = MarketRepository(tmp_path / "facts")
    identities = populate(repository)
    with repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE observations SET available_at=? WHERE id=?", (tampered, identities[0])
        )
    result = MarketDataService(repository.root).prices(
        "SH600000", "2024-02-08", "2024-02-08", knowledge_cutoff="2024-02-08T20:00:00+08:00"
    )
    assert result.state == "CORRUPT" and not result.ready
    with pytest.raises(CorruptData, match="身份|知识时点"):
        repository.by_ids(identities)


def test_synthetic_coverage_recursively_requires_original_source_files(tmp_path):
    repository = MarketRepository(tmp_path / "facts")
    leaf = repository.save_raw(source([], fetched="2024-02-08T18:10:00+08:00"))
    intermediate = repository.save_raw(
        {**source([], fetched="2024-02-08T18:11:00+08:00"), "dependencies": [leaf]}
    )
    synthetic = {
        "provider": "combined_distributions",
        "fetched_at": "2024-02-08T18:12:00+08:00",
        "dependencies": [intermediate],
    }
    coverage = [
        {
            "code": "SH600000",
            "date": "2024-02-08",
            "start": "2024-01-01",
            "end": "2024-02-08",
            "purpose": "events",
            "complete": True,
        }
    ]
    repository.put("coverage", coverage, synthetic)
    service = MarketDataService(repository.root)
    cutoff = "2024-02-08T20:00:00+08:00"
    assert service.event_coverage(
        "SH600000", "2024-01-01", "2024-02-08", knowledge_cutoff=cutoff
    ).ready
    snapshot = service.freeze(
        ["SH600000"], "2024-02-08", knowledge_cutoff=cutoff, purposes=("events",)
    )
    assert snapshot["body"]["content"]["results"]["SH600000"]["events"]["state"] == "READY"
    (repository.root / leaf).unlink()
    assert not service.event_coverage(
        "SH600000", "2024-01-01", "2024-02-08", knowledge_cutoff=cutoff
    ).ready
    assert not service.events("SH600000", knowledge_cutoff=cutoff).ready
    with pytest.raises(CorruptData):
        service.read_snapshot(snapshot["snapshot_id"])


def test_empty_new_event_batch_retracts_current_members_without_erasing_history(tmp_path):
    repository = MarketRepository(tmp_path / "facts")
    raw = {
        "code": "sh.600000",
        "dividPlanAnnounceDate": "2024-02-01",
        "dividPlanDate": "2024-02-05",
        "dividRegistDate": "2024-02-07",
        "dividOperateDate": "2024-02-08",
        "dividPayDate": "2024-02-08",
        "dividStockMarketDate": "",
        "dividCashPsBeforeTax": "0.42",
        "dividStocksPs": "0",
        "dividReserveToStockPs": "0",
        "dividCashStock": "10派4.2元",
    }
    original = source([raw], provider="baostock")
    events = normalize_baostock_events([raw], "SH600000", original["fetched_at"])
    members = repository.put("event", events, original)
    old_batch = {
        "code": "SH600000",
        "date": "2024-02-08",
        "batch_key": "baostock:2024",
        "members": members,
        "year": 2024,
    }
    repository.put("event_batch", [old_batch], original, key_fields=["code", "batch_key"])
    service = MarketDataService(repository.root)
    old_cutoff = "2024-02-08T20:00:00+08:00"
    before = service.events("SH600000", knowledge_cutoff=old_cutoff)
    assert before.ready and len(before.data) == 1
    snapshot = service.freeze(
        ["SH600000"], "2024-02-08", knowledge_cutoff=old_cutoff, purposes=("events",)
    )
    replacement = source([], fetched="2024-02-09T18:00:00+08:00", provider="baostock")
    new_batch = {**old_batch, "date": "2024-02-09", "members": []}
    replacement_ids = repository.put(
        "event_batch", [new_batch], replacement, key_fields=["code", "batch_key"]
    )
    current = service.events("SH600000", knowledge_cutoff="2024-02-09T20:00:00+08:00")
    assert current.ready and current.data == []
    assert set(current.observation_ids) == set(replacement_ids)
    historical = service.events("SH600000", knowledge_cutoff=old_cutoff)
    assert historical.data == before.data
    assert len(repository.select("event", latest=False)) == 1
    frozen = service.read_snapshot(snapshot["snapshot_id"])
    assert len(frozen["body"]["content"]["results"]["SH600000"]["events"]["data"]) == 1


def test_refresh_does_not_bypass_server_retry_after(tmp_path):
    root = tmp_path / "updater"
    (root / "configs").mkdir(parents=True)
    (root / "configs/preparation.toml").write_bytes(
        (Path(__file__).resolve().parents[1] / "configs/preparation.toml").read_bytes()
    )
    clock, calls = Clock(), []
    sources = {name: FakeSource(name, clock, calls) for name in ("baostock", "tencent")}
    sources["tencent"].failures["prices"] = [
        HTTPProviderError(
            "server rate limited", retryable=True, retry_after_seconds=600, status_code=429
        )
    ]
    wanted = request(candidates=(), include_indices=True, history_sessions=1)
    collect(root, sources, clock, wanted)
    failed = [
        row
        for row in MarketRepository(root).states("requests").values()
        if row.get("state") == "FAILED" and row["identity"]["source"] == "tencent"
    ]
    assert len(failed) == 1
    identity = failed[0]["identity"]
    assert datetime.fromisoformat(failed[0]["server_retry_at"]) == clock() + timedelta(seconds=600)

    def limited_calls():
        return [item for item in calls if item == ("tencent", "prices", identity["params"])]

    assert len(limited_calls()) == 1
    for seconds in (0, 599):
        clock.advance(seconds)
        result = collect(root, sources, clock, wanted, refresh=True)
        assert len(limited_calls()) == 1
        assert any(
            issue.get("kind") == "COOLDOWN" and issue.get("origin") == "server"
            for issue in result["issues"]
        )
    clock.advance(1)
    collect(root, sources, clock, wanted, refresh=True)
    assert len(limited_calls()) == 2
    state = [
        row
        for row in MarketRepository(root).states("requests").values()
        if row["identity"] == identity
    ][0]
    assert state["state"] == "OK" and "server_retry_at" not in state
    assert identity["params"]["end_date"] == AS_OF


def test_nontrading_session_is_waiting_for_execution_without_using_signals(tmp_path, monkeypatch):
    repository = MarketRepository(tmp_path / "facts")
    day = [{"date": "2024-02-10", "is_trading_day": "0"}]
    repository.put("calendar", day, source(day, fetched="2024-02-01T18:00:00+08:00"))
    import china_stock_sim.market.signals as signals

    monkeypatch.setattr(
        signals,
        "execution_facts",
        lambda *a, **k: pytest.fail("non-trading day must not enter execution facts assembly"),
    )
    result = MarketDataService(repository.root).execution_facts(
        "SH600000", "2024-02-10", knowledge_cutoff="2024-02-10T20:00:00+08:00"
    )
    assert result.state == "WAITING_DATA" and not result.ready
    assert result.issues == ({"kind": "NON_TRADING_SESSION", "date": "2024-02-10"},)
