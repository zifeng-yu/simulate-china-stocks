"""Public data API tests use isolated local facts, never accounts, models or network."""

import json
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import DataRequest, aware_time, session_date
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService


def source(records, *, fetched="2024-02-08T18:11:00+08:00", provider="fixture"):
    return {
        "provider": provider,
        "adapter_version": "test",
        "fetched_at": fetched,
        "fields": sorted(set().union(*(row.keys() for row in records))) if records else [],
        "rows": records,
    }


def bar(day="2024-02-08", *, close="10.00", **extra):
    return {
        "code": "SH600000",
        "date": day,
        "open": "10.00",
        "high": "11.00",
        "low": "9.00",
        "close": close,
        "preclose": "9.90",
        "volume": 10000,
        "amount": "100000.00",
        "tradestatus": 1,
        "isST": 0,
        "adjustflag": 3,
        **extra,
    }


def calendar_days(start, end, trading):
    current, end = date.fromisoformat(start), date.fromisoformat(end)
    rows = []
    while current <= end:
        day = current.isoformat()
        rows.append({"date": day, "is_trading_day": "1" if day in trading else "0"})
        current += timedelta(days=1)
    return rows


@pytest.fixture
def repository(tmp_path):
    # Deliberately no configs/default.toml, model configuration, .env or ledger.
    return MarketRepository(tmp_path / "facts_only")


def populate(repository, bars=None):
    days = calendar_days("2024-02-07", "2024-02-19", {"2024-02-07", "2024-02-08", "2024-02-19"})
    repository.put("calendar", days, source(days, fetched="2024-02-01T18:00:00+08:00"))
    bars = [bar()] if bars is None else bars
    return repository.put("bar", bars, source(bars))


def forbid_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("a read-only market query attempted subprocess/network access")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)


def test_constructing_and_querying_missing_repository_create_nothing(repository, monkeypatch):
    forbid_network(monkeypatch)
    service = MarketDataService(repository.root)
    assert not repository.root.exists()
    results = [
        service.calendar(),
        service.calendar_operation("next_session", "2024-02-08"),
        service.prices("SH600000", "2024-02-08", "2024-02-08"),
        service.security("SH600000"),
        service.events("SH600000"),
        service.pool("2024-02-08"),
        service.revisions("bar", "SH600000"),
        service.event_coverage("SH600000", "2024-02-08", "2024-02-08"),
    ]
    assert all(not result.ready for result in results)
    assert repository.states("jobs") == {} and repository.states("requests") == {}
    assert repository.by_ids([]) == []
    assert not repository.root.exists()


def test_known_queries_are_read_only_and_need_no_model_or_account(repository, monkeypatch):
    populate(repository)
    service = MarketDataService(repository.root)
    before = {
        path.relative_to(repository.root): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    forbid_network(monkeypatch)
    result = service.prices("sh.600000", "2024-02-08", "2024-02-08")
    assert result.ready and result.data[0]["close"] == "10.00"
    assert result.data[0]["_provenance"]["provider"] == "fixture"
    assert result.data[0]["_provenance"]["raw_ref"].startswith("data/raw/market/")
    assert service.calendar_operation("next_session", "2024-02-08").data == "2024-02-19"
    after = {
        path.relative_to(repository.root): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert not (repository.root / "configs").exists()
    assert not list(repository.root.rglob("ledger.sqlite3"))


def test_calendar_uses_explicit_holidays_and_requires_every_intervening_day():
    days = calendar_days("2024-02-08", "2024-02-19", {"2024-02-08", "2024-02-19"})
    calendar = TradingCalendar(days)
    assert calendar.next_session("2024-02-08").data == "2024-02-19"
    assert calendar.previous_session("2024-02-19").data == "2024-02-08"
    assert calendar.between("2024-02-08", "2024-02-19").data == ["2024-02-08", "2024-02-19"]
    assert calendar.window("2024-02-19", 2).data == ["2024-02-08", "2024-02-19"]
    missing = TradingCalendar([day for day in days if day["date"] != "2024-02-12"])
    result = missing.next_session("2024-02-08")
    assert not result.ready and result.issues[0]["date"] == "2024-02-12"
    assert not missing.between("2024-02-08", "2024-02-19").ready


def test_calendar_month_end_is_next_session_boundary_not_calendar_last_day():
    rows = calendar_days("2024-08-29", "2024-09-02", {"2024-08-29", "2024-08-30", "2024-09-02"})
    calendar = TradingCalendar(rows)
    assert calendar.is_month_end("2024-08-29").data is False
    assert calendar.is_month_end("2024-08-30").data is True
    assert calendar.is_month_end("2024-08-31").data is False
    assert not TradingCalendar(rows[:-1]).is_month_end("2024-08-30").ready


@pytest.mark.parametrize(
    "rows",
    [
        [{"date": "2024-02-08", "is_trading_day": "unknown"}],
        [{"date": "2024-02-30", "is_trading_day": "1"}],
        [{"date": "2024-02-08", "is_trading_day": "1"}] * 2,
    ],
)
def test_calendar_rejects_unknown_flags_bad_dates_and_duplicates(rows):
    with pytest.raises(ValueError):
        TradingCalendar(rows)


def test_price_completeness_does_not_require_holiday_bars_but_rejects_missing_sessions(repository):
    populate(repository, [bar(), bar("2024-02-19")])
    service = MarketDataService(repository.root)
    assert service.prices("SH600000", "2024-02-08", "2024-02-19").ready
    missing = service.prices("SH600000", "2024-02-07", "2024-02-19")
    assert missing.state == "PARTIAL"
    assert {issue["date"] for issue in missing.issues} == {"2024-02-07"}
    unknown = service.prices("SZ000001", "2024-02-08", "2024-02-19")
    assert unknown.state == "MISSING" and not unknown.ready
    assert not service.prices("SH600000", "2024-02-08", "2024-02-20").ready


def test_observation_available_after_cutoff_is_not_visible(repository):
    populate(repository)
    service = MarketDataService(repository.root)
    before = service.prices(
        "SH600000", "2024-02-08", "2024-02-08", knowledge_cutoff="2024-02-08T18:10:59+08:00"
    )
    after = service.prices(
        "SH600000", "2024-02-08", "2024-02-08", knowledge_cutoff="2024-02-08T18:11:00+08:00"
    )
    assert not before.ready and before.data == []
    assert after.ready
    # The UTC representation of the same instant must give identical membership.
    assert (
        service.prices(
            "SH600000", "2024-02-08", "2024-02-08", knowledge_cutoff="2024-02-08T10:11:00+00:00"
        ).data
        == after.data
    )


def test_default_queries_do_not_admit_future_observation_availability(repository):
    populate(repository, [])
    future = [bar()]
    repository.put("bar", future, source(future, fetched="2099-01-01T00:00:00+08:00"))
    result = MarketDataService(repository.root).prices("SH600000", "2024-02-08", "2024-02-08")
    assert not result.ready and result.data == []


def test_future_market_session_is_not_ready_at_an_earlier_knowledge_cutoff(repository):
    populate(repository, [bar("2024-02-19")])
    result = MarketDataService(repository.root).prices(
        "SH600000", "2024-02-19", "2024-02-19", knowledge_cutoff="2024-02-08T20:00:00+08:00"
    )
    assert not result.ready


def test_revisions_append_and_frozen_snapshot_retains_original_facts(repository, monkeypatch):
    populate(repository)
    service = MarketDataService(repository.root)
    first = service.freeze(["SH600000"], "2024-02-08", knowledge_cutoff="2024-02-08T20:00:00+08:00")
    before = (repository.snapshots / f"{first['snapshot_id']}.json").read_bytes()
    changed = [bar(close="10.50")]
    repository.put("bar", changed, source(changed, fetched="2024-02-20T18:11:00+08:00"))
    assert len(service.revisions("bar", "SH600000").data) == 2
    assert service.prices("SH600000", "2024-02-08", "2024-02-08").data[0]["close"] == "10.50"
    forbid_network(monkeypatch)
    snapshot = service.read_snapshot(first["snapshot_id"])
    assert (
        snapshot["body"]["content"]["results"]["SH600000"]["valuation"]["data"][0]["close"]
        == "10.00"
    )
    assert (repository.snapshots / f"{first['snapshot_id']}.json").read_bytes() == before
    old = service.prices(
        "SH600000", "2024-02-08", "2024-02-08", knowledge_cutoff="2024-02-08T20:00:00+08:00"
    )
    assert old.data[0]["close"] == "10.00"


def test_same_response_is_idempotent_and_new_fetch_retains_revision(repository):
    records = [bar()]
    ids = repository.put("bar", records, source(records))
    assert repository.put("bar", records, source(records)) == ids
    assert len(repository.select("bar", latest=False)) == 1
    new = repository.put("bar", records, source(records, fetched="2024-02-09T18:11:00+08:00"))
    assert new != ids and len(repository.select("bar", latest=False)) == 2


@pytest.mark.parametrize(
    "corruption", ["raw_changed", "raw_deleted", "index_changed", "snapshot_changed"]
)
def test_snapshot_rejects_corrupt_source_index_or_envelope(repository, corruption):
    ids = populate(repository)
    service = MarketDataService(repository.root)
    snapshot = service.freeze(
        ["SH600000"], "2024-02-08", knowledge_cutoff="2024-02-08T20:00:00+08:00"
    )
    raw_path = repository.root / repository.by_ids(ids)[0]["raw_ref"]
    if corruption == "raw_changed":
        raw_path.write_text('{"modified":true}', encoding="utf-8")
    elif corruption == "raw_deleted":
        raw_path.unlink()
    elif corruption == "index_changed":
        with repository.connection(write=True) as connection:
            connection.execute(
                "UPDATE observations SET payload=? WHERE id=?",
                (json.dumps(bar(close="999")), ids[0]),
            )
    else:
        path = repository.snapshots / f"{snapshot['snapshot_id']}.json"
        envelope = json.loads(path.read_text())
        envelope["body"]["content"]["as_of"] = "2099-01-01"
        path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(CorruptData):
        service.read_snapshot(snapshot["snapshot_id"])
    if corruption != "snapshot_changed":
        assert service.prices("SH600000", "2024-02-08", "2024-02-08").state == "CORRUPT"


def test_data_conflicts_never_report_ready(repository):
    populate(repository, [bar(conflicts=[{"field": "close", "primary": "10", "other": "12"}])])
    result = MarketDataService(repository.root).prices("SH600000", "2024-02-08", "2024-02-08")
    assert result.state == "CONFLICT" and not result.ready
    assert result.issues[0]["kind"] == "DATA_CONFLICT"


def test_update_requires_explicit_entry_and_passes_request_without_account_context(
    repository, monkeypatch
):
    calls = []

    class Updater:
        def __init__(self, root, **kwargs):
            calls.append(("init", root, kwargs))

        def run(self, request):
            calls.append(("run", request))
            return {"state": "STUB_UPDATE"}

    monkeypatch.setitem(
        sys.modules, "china_stock_sim.market.update", SimpleNamespace(MarketUpdater=Updater)
    )
    service = MarketDataService(repository.root)
    service.calendar()
    service.prices("SH600000", "2024-02-08", "2024-02-08")
    assert calls == []
    request = DataRequest("2024-02-08", holdings=("SH600000",))
    assert service.update(request, budget_seconds=5) == {"state": "STUB_UPDATE"}
    assert calls == [("init", repository.root, {"budget_seconds": 5}), ("run", request)]


def test_request_prioritizes_out_of_pool_holdings_and_preserves_unsettled_work():
    request = DataRequest(
        "2024-02-08",
        holdings=("sh.600000", "SH600000"),
        pending_orders=("SZ000001", "SH600000"),
        pending_actions=("SH601398",),
        candidates=("SZ000002", "SZ000001"),
    )
    assert request.holdings == ("SH600000",)
    assert request.ordered_codes == ("SH600000", "SZ000001", "SH601398", "SZ000002")
    assert "SH600000" not in request.candidates and "SH600000" in request.ordered_codes


@pytest.mark.parametrize("size", [True, 0, 501, "121"])
def test_request_rejects_invalid_history_window(size):
    with pytest.raises(ValueError):
        DataRequest("2024-02-08", history_sessions=size)


def test_temporal_contracts_reject_ambiguous_dates_and_naive_knowledge():
    with pytest.raises(ValueError):
        aware_time("2024-02-08T18:00:00")
    with pytest.raises(ValueError):
        session_date("2024-2-8")
    with pytest.raises(ValueError):
        session_date(datetime(2024, 2, 8, tzinfo=timezone.utc))


def test_future_knowledge_snapshot_is_rejected_before_creating_files(repository):
    with pytest.raises(ValueError, match="未来"):
        MarketDataService(repository.root).freeze(
            ["SH600000"], "2024-02-08", knowledge_cutoff="2099-01-01T00:00:00+08:00"
        )
    assert not repository.root.exists()
