"""Repair must cover every raw kind without destroying valid parent evidence."""

import sqlite3
from pathlib import Path

import pytest
from test_market_service import bar, source
from test_market_update import Clock, FakeSource, collect, request

from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.repository import KINDS, CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.update import MarketUpdater


def root_with_config(tmp_path):
    root = tmp_path / "isolated"
    (root / "configs").mkdir(parents=True)
    template = Path(__file__).resolve().parents[1] / "configs/preparation.toml"
    (root / "configs/preparation.toml").write_bytes(template.read_bytes())
    return root


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_repair_quarantines_all_object_kinds_and_only_corrupt_leaf(tmp_path, kind):
    root = root_with_config(tmp_path)
    repository = MarketRepository(root)
    leaf = repository.save_raw(source([{"evidence": kind}]))
    parent = {**source([]), "dependencies": [leaf]}
    repository.put(kind, [{"code": "SH600000", "date": "2024-02-08"}], parent)
    parent_reference = repository.save_raw(parent)
    (root / leaf).write_text("broken original")
    updater = MarketUpdater(root, providers={}, repair=True)
    updater.job = {}
    updater._repair_objects()
    assert (root / parent_reference).is_file()
    assert not (root / leaf).exists()
    assert len(updater.job["repairs"]) == 1
    saved = root / updater.job["repairs"][0]["quarantine"]
    assert saved.read_text() == "broken original"


def test_repair_recovers_security_and_stock_prices_without_account(tmp_path):
    root = root_with_config(tmp_path)
    clock = Clock()
    calls = []
    sources = {name: FakeSource(name, clock, calls) for name in ("baostock", "tencent")}
    assert collect(root, sources, clock)["state"] == "COMPLETE"
    repository = MarketRepository(root)
    for kind in ("bar", "security"):
        for reference in {r["raw_ref"] for r in repository.select(kind)}:
            (root / reference).write_text("interrupted disk write")
    clock.advance(1)
    result = collect(root, sources, clock, wanted=request(), repair=True)
    assert result["state"] == "COMPLETE"
    assert len(result["repairs"]) == 2
    assert MarketDataService(root).security("SH600000").ready
    assert not (root / "data/state/ledger.sqlite3").exists()


def test_timezone_equivalent_source_time_remains_valid(tmp_path):
    repository = MarketRepository(tmp_path)
    records = [bar()]
    identities = repository.put("bar", records, source(records, fetched="2024-02-08T10:11:00Z"))
    assert repository.by_ids(identities)[0]["payload"]["close"] == "10.00"


def test_unknown_database_schema_is_not_silently_overwritten(tmp_path):
    repository = MarketRepository(tmp_path)
    repository.path.parent.mkdir(parents=True)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(CorruptData, match="格式"):
        repository.put("bar", [bar()], source([bar()]))
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_revisions_expose_changed_fields_and_previous_identity(tmp_path):
    repository = MarketRepository(tmp_path)
    original = [bar()]
    first = repository.put("bar", original, source(original))
    correction = [bar(close="10.01")]
    repository.put("bar", correction, source(correction, fetched="2024-02-09T18:00:00+08:00"))
    revisions = MarketDataService(tmp_path).revisions("bar", "SH600000")
    latest = revisions.data[-1]["_revision"]
    assert latest["previous_observation_id"] == first[0]
    assert latest["changed_fields"] == {"close": {"before": "10.00", "after": "10.01"}}


def test_gap_filling_response_conflict_is_preserved_and_isolated(tmp_path):
    root = root_with_config(tmp_path)
    clock, calls = Clock(), []
    primary = FakeSource("baostock", clock, calls)
    primary.omitted_prices = {("SH600000", "2026-10-23"), ("SH600000", "2026-10-27")}

    class ConflictingBackup(FakeSource):
        def fetch(self, query, params):
            response = super().fetch(query, params)
            for row in response["rows"]:
                if row["date"] == "2026-10-26":
                    row["close"] = "10.01"
            return response

    backup = ConflictingBackup("tencent", clock, calls)
    result = collect(root, {"baostock": primary, "tencent": backup}, clock)
    assert result["state"] == "PARTIAL"
    service = MarketDataService(root)
    disputed = service.prices("SH600000", "2026-10-26", "2026-10-26", knowledge_cutoff=clock())
    assert disputed.state == "CONFLICT"
    assert disputed.data[0]["conflicts"][0]["fields"]["close"] == {
        "primary": "10.00", "backup": "10.01"}
    assert service.prices("SH600000", "2026-10-27", "2026-10-27", knowledge_cutoff=clock()).ready
    repository = MarketRepository(root)
    fact = repository.select("bar", code="SH600000", start="2026-10-26", end="2026-10-26")[0]
    repository.validate_raw(fact["raw_ref"])


@pytest.mark.parametrize("mode,expected_primary_queries", [
    ("resume", 1), ("refresh", 2), ("repair", 2), ("next_day", 2)])
def test_partial_event_collection_reuses_only_valid_same_day_success(
    tmp_path, mode, expected_primary_queries
):
    root = root_with_config(tmp_path)
    clock, calls = Clock(), []

    class EventSource(FakeSource):
        def fetch(self, query, params):
            if query not in {"dividends", "events"}:
                return super().fetch(query, params)
            self.calls.append((self.provider_id, query, dict(params)))
            failures = self.failures.get(query, [])
            if failures:
                raise failures.pop(0)
            return {"provider": self.provider_id, "adapter_version": self.adapter_version,
                    "fetched_at": self.clock().isoformat(), "rows": [],
                    "fields": ["code", "dividOperateDate", "dividCashPsBeforeTax"]}

    providers = {name: EventSource(name, clock, calls) for name in ("baostock", "eastmoney")}
    providers["eastmoney"].failures["events"] = [ProviderError("temporarily offline", retryable=True)]
    wanted = request(include_events=True)
    assert collect(root, providers, clock, wanted)["state"] == "PARTIAL"
    if mode == "repair":
        repository = MarketRepository(root)
        batch = repository.select("event_batch")[0]
        (root / batch["raw_ref"]).write_text("broken cached event source")
    clock.advance(86400 if mode == "next_day" else 60)
    result = collect(root, providers, clock, wanted, refresh=mode == "refresh", repair=mode == "repair")
    assert result["state"] == "COMPLETE"
    assert sum(query == "dividends" for _, query, _ in calls) == expected_primary_queries
    assert sum(query == "events" for _, query, _ in calls) == 2
    assert MarketDataService(root).event_coverage("SH600000", "2026-10-23", "2026-10-27").ready
