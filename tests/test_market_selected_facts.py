"""Only consumed evidence gates a read; historical snapshots stay independently strict."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from china_stock_sim.market.contracts import DataRequest
from china_stock_sim.market.pools import prepare_pool
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.update import MarketUpdater


def put(repository, kind, rows, fetched="2024-03-04T20:00:00+08:00"):
    return repository.put(kind, rows, {"provider": "fixture", "adapter_version": "1",
        "fetched_at": fetched, "rows": rows, "fields": list(rows[0])})


def damage(repository, identities):
    reference = repository.by_ids(identities)[0]["raw_ref"]
    (repository.root / reference).write_text("broken source bytes")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "configs").mkdir()
    project = Path(__file__).resolve().parents[1]
    (tmp_path / "configs/preparation.toml").write_bytes(
        (project / "configs/preparation.toml").read_bytes())
    return tmp_path


def test_security_ignores_old_raw_but_selected_and_frozen_facts_remain_strict(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    old = put(repository, "security", [{"code": "SH600000", "date": "2024-02-01", "name": "旧名"}],
              "2024-02-01T20:00:00+08:00")
    frozen = repository.freeze({}, old)
    current = put(repository, "security", [{"code": "SH600000", "date": "2024-03-04", "name": "新名"}])
    damage(repository, old)
    assert service.security("SH600000").data["name"] == "新名"
    assert service.security("SH600000", knowledge_cutoff="2024-02-02T20:00:00+08:00").state == "CORRUPT"
    with pytest.raises(CorruptData):
        service.read_snapshot(frozen["snapshot_id"])
    damage(repository, current)
    assert service.security("SH600000").state == "CORRUPT"


def pool(day, effective):
    return {"date": day, "effective_from": effective, "selected": [{"code": "SH600000"}]}


def test_pool_ignores_inactive_raw_and_not_yet_effective_raw(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    old = put(repository, "pool", [pool("2024-02-01", "2024-02-02")], "2024-02-01T20:00:00+08:00")
    current = put(repository, "pool", [pool("2024-02-29", "2024-03-01")])
    upcoming = put(repository, "pool", [pool("2024-03-04", "2024-03-05")])
    damage(repository, old)
    damage(repository, upcoming)
    result = service.pool("2024-03-04")
    assert result.ready and result.observation_ids == tuple(current)
    assert service.pool("2024-02-02").state == "CORRUPT"
    assert service.pool("2024-03-05").state == "CORRUPT"


@pytest.mark.parametrize("force", [False, True])
def test_corrupt_current_pool_cannot_be_treated_as_first_pool(root, monkeypatch, force):
    repository = MarketRepository(root)
    current = put(repository, "pool", [pool("2024-02-29", "2024-03-01")])
    damage(repository, current)

    def forbidden(*args, **kwargs):
        pytest.fail("corrupt active pool must be repaired before selecting a replacement")

    monkeypatch.setattr("china_stock_sim.market.pools.Preparation", forbidden)
    result = prepare_pool(root, "2024-03-04", download=True, repair=True, force=force)
    assert result["state"] == "NEEDS_DATA_REVIEW"
    assert result["active_pool"]["state"] == "CORRUPT"


def calendar_rows(start, end):
    # Explicit synthetic source fixture, never a production weekday inference.
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    rows = []
    while current <= last:
        rows.append({"date": current.isoformat(), "is_trading_day": str(int(current.weekday() < 5))})
        current += timedelta(days=1)
    return rows


def test_calendar_and_prices_validate_only_the_consumed_interval(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    old = put(repository, "calendar", calendar_rows("2020-01-01", "2020-01-02"))
    put(repository, "calendar", calendar_rows("2024-02-28", "2024-03-06"))
    put(repository, "bar", [{"code": "SH600000", "date": "2024-03-04", "close": "10"}])
    damage(repository, old)
    assert service.calendar().state == "CORRUPT"
    assert service.calendar(start="2024-03-01", end="2024-03-04").ready
    assert service.calendar_operation("window", "2024-03-04", size=2).data == ["2024-03-01", "2024-03-04"]
    assert service.calendar_operation("next_session", "2024-03-01").data == "2024-03-04"
    assert service.calendar_operation("previous_session", "2024-03-04").data == "2024-03-01"
    assert service.calendar_operation("is_month_end", "2024-02-29").data is True
    assert service.prices("SH600000", "2024-03-04", "2024-03-04").ready


def test_calendar_validates_intervening_closed_days_and_reports_actual_gap(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    put(repository, "calendar", [{"date": "2024-03-01", "is_trading_day": "1"}])
    closed = put(repository, "calendar", [{"date": "2024-03-02", "is_trading_day": "0"}])
    put(repository, "calendar", [{"date": "2024-03-04", "is_trading_day": "1"}])
    missing = service.calendar_operation("window", "2024-03-04", size=2)
    assert missing.state == "WAITING_DATA" and missing.issues[0]["date"] == "2024-03-03"
    put(repository, "calendar", [{"date": "2024-03-03", "is_trading_day": "0"}])
    assert service.calendar_operation("next_session", "2024-03-01").ready
    damage(repository, closed)
    assert service.calendar_operation("window", "2024-03-04", size=2).state == "CORRUPT"
    assert service.calendar_operation("next_session", "2024-03-01").state == "CORRUPT"
    assert service.calendar_operation("previous_session", "2024-03-04").state == "CORRUPT"
    assert service.calendar_operation("window", "2024-03-04", size=1).ready


def test_coverage_ignores_old_outside_range_evidence_and_preserves_frozen_integrity(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    def coverage(start, end):
        return {"code": "SH600000", "date": end, "purpose": "events", "start": start,
                "end": end, "complete": True}
    old = put(repository, "coverage", [coverage("2020-01-01", "2020-01-31")])
    frozen = repository.freeze({}, old)
    current = put(repository, "coverage", [coverage("2024-02-01", "2024-03-04")])
    damage(repository, old)
    result = service.event_coverage("SH600000", "2024-02-29", "2024-03-04")
    assert result.ready and result.observation_ids == tuple(current)
    assert service.event_coverage("SH600000", "2020-01-01", "2020-01-31").state == "CORRUPT"
    with pytest.raises(CorruptData):
        service.read_snapshot(frozen["snapshot_id"])
    damage(repository, current)
    assert service.event_coverage("SH600000", "2024-03-04", "2024-03-04").state == "CORRUPT"


@pytest.mark.parametrize("kind", ["security", "pool", "coverage"])
def test_selected_corrupt_fact_never_falls_back_to_valid_older_fact(root, kind):
    repository, service = MarketRepository(root), MarketDataService(root)
    if kind == "security":
        old = {"code": "SH600000", "date": "2024-02-01", "name": "旧名"}
        current = {**old, "date": "2024-03-04", "name": "新名"}
        def query():
            return service.security("SH600000")
    elif kind == "pool":
        old, current = pool("2024-02-01", "2024-02-02"), pool("2024-02-29", "2024-03-01")
        def query():
            return service.pool("2024-03-04")
    else:
        old = {"code": "SH600000", "date": "2024-03-04", "purpose": "events", "complete": True,
               "start": "2024-02-01", "end": "2024-03-04"}
        current = {**old, "start": "2024-01-01"}
        def query():
            return service.event_coverage("SH600000", "2024-03-04", "2024-03-04")
    original = put(repository, kind, [old], "2024-03-04T18:11:00+08:00")
    selected = put(repository, kind, [current])
    damage(repository, selected)
    assert repository.by_ids(original)
    assert query().state == "CORRUPT"


def test_malformed_database_returns_corrupt_without_mutating_or_rebuilding(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    repository.path.parent.mkdir(parents=True)
    original = b"malformed SQLite database"
    repository.path.write_bytes(original)
    queries = [service.calendar, lambda: service.security("SH600000"),
               lambda: service.pool("2024-03-04"),
               lambda: service.calendar_operation("next_session", "2024-03-04"),
               lambda: service.event_coverage("SH600000", "2024-03-04", "2024-03-04")]
    assert all(query().state == "CORRUPT" for query in queries)
    assert repository.path.read_bytes() == original


class CurrentSource:
    provider_id, adapter_version = "baostock", "fixture"

    def fetch(self, query, params):
        if query == "calendar":
            rows = [{"calendar_date": row["date"], "is_trading_day": row["is_trading_day"]}
                    for row in calendar_rows(params["start_date"], params["end_date"])]
        elif query == "securities":
            rows = [{"code": "sh.600000", "code_name": "正常股票", "ipoDate": "2000-01-01",
                     "outDate": "", "type": "1", "status": "1"}]
        elif query == "history":
            rows = [{"code": params["code"], "date": "2024-03-04", "open": "10", "close": "10",
                     "high": "11", "low": "9", "preclose": "10", "volume": "10000",
                     "amount": "100000", "tradestatus": "1", "isST": "0", "adjustflag": "3"}]
        else:
            pytest.fail(f"unexpected source query {query}")
        return {"provider": self.provider_id, "adapter_version": self.adapter_version,
                "fetched_at": "2024-03-04T20:00:00+08:00", "rows": rows, "fields": list(rows[0])}


def test_updater_repairs_current_span_without_requiring_old_year_raw(root):
    repository, service = MarketRepository(root), MarketDataService(root)
    old = put(repository, "calendar", calendar_rows("2020-01-01", "2020-01-02"),
              "2020-01-02T20:00:00+08:00")
    frozen = repository.freeze({}, old)
    damage(repository, old)
    updater = MarketUpdater(root, providers={"baostock": CurrentSource()}, repair=True,
                            clock=lambda: datetime.fromisoformat("2024-03-04T20:00:00+08:00"))
    result = updater.run(DataRequest("2024-03-04", candidates=("SH600000",), history_sessions=1,
                                    include_events=False, include_indices=False))
    assert result["state"] == "COMPLETE"
    assert result["repairs"]
    assert service.calendar_operation("window", "2024-03-04", size=1).ready
    assert service.prices("SH600000", "2024-03-04", "2024-03-04").ready
    with pytest.raises(CorruptData):
        service.read_snapshot(frozen["snapshot_id"])
