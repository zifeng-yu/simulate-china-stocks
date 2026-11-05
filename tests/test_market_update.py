"""Incremental collection tests with a real isolated repository and fake sources."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from china_stock_sim.market.contracts import DataRequest
from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.update import MarketUpdater

REPOSITORY = Path(__file__).resolve().parents[1]
AS_OF = "2026-10-27"
STOCKS = ("SH600000", "SZ000001", "SH600001", "SZ000002")


def fixture_days(start, end):
    """These explicit synthetic calendar flags are source fixtures, not a fallback."""
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    rows = []
    while day <= last:
        rows.append(
            {"calendar_date": day.isoformat(), "is_trading_day": str(int(day.weekday() < 5))}
        )
        day += timedelta(days=1)
    return rows


def fixture_sessions(start, end):
    return [
        row["calendar_date"] for row in fixture_days(start, end) if row["is_trading_day"] == "1"
    ]


class Clock:
    def __init__(self):
        self.now = datetime.fromisoformat("2026-10-27T20:00:00+08:00")

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeSource:
    adapter_version = "fixture-1"

    def __init__(self, name, clock, calls):
        self.provider_id, self.clock, self.calls = name, clock, calls
        self.failures = {}
        self.omitted_prices = set()
        self.omitted_calendar = set()
        self.invalid_history = False
        self.empty_prices = False

    def fetch(self, query, params):
        self.calls.append((self.provider_id, query, dict(params)))
        failures = self.failures.get(query, [])
        if failures:
            raise failures.pop(0)
        if query == "calendar":
            rows = [
                row
                for row in fixture_days(params["start_date"], params["end_date"])
                if row["calendar_date"] not in self.omitted_calendar
            ]
            fields = ["calendar_date", "is_trading_day"]
        elif query == "securities":
            rows = [
                {
                    "code": code[:2].lower() + "." + code[2:],
                    "code_name": "正常股票",
                    "ipoDate": "2000-01-01",
                    "outDate": "",
                    "type": "1",
                    "status": "1",
                }
                for code in STOCKS
            ]
            fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        elif query in {"history", "prices"}:
            rows = []
            for day in fixture_sessions(params["start_date"], params["end_date"]):
                if self.empty_prices or (params["code"], day) in self.omitted_prices:
                    continue
                row = {
                    "code": params["code"],
                    "date": day,
                    "open": "10.00",
                    "high": "11.00",
                    "low": "9.00",
                    "close": "10.00",
                }
                if query == "history":
                    row.update(
                        preclose="9.90",
                        volume="10000",
                        amount="100000.00",
                        tradestatus="1",
                        isST="0",
                        adjustflag="3",
                    )
                    if self.invalid_history:
                        row["volume"] = "0"
                rows.append(row)
            fields = list(rows[0]) if rows else ["code", "date", "open", "high", "low", "close"]
        else:
            raise AssertionError(f"Unexpected fake query: {self.provider_id}/{query}")
        response = {
            "provider": self.provider_id,
            "adapter_version": self.adapter_version,
            "fetched_at": self.clock().isoformat(),
            "fields": fields,
            "rows": rows,
        }
        if query == "prices":
            response["capabilities"] = ["unadjusted_prices"]
        return response


@pytest.fixture
def market_root(tmp_path):
    root = (tmp_path / "data_service_only").resolve()
    (root / "configs").mkdir(parents=True)
    (root / "configs/preparation.toml").write_bytes(
        (REPOSITORY / "configs/preparation.toml").read_bytes()
    )
    return root


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sources(clock):
    calls = []
    return {name: FakeSource(name, clock, calls) for name in ("baostock", "tencent")}


def request(as_of=AS_OF, **changes):
    values = {
        "as_of": as_of,
        "candidates": ("SH600000",),
        "history_sessions": 3,
        "include_events": False,
        "include_indices": False,
    }
    values.update(changes)
    return DataRequest(**values)


def collect(root, sources, clock, wanted=None, **options):
    return MarketUpdater(root, providers=sources, clock=clock, **options).run(wanted or request())


def history_calls(sources):
    return [
        params
        for source, query, params in sources["baostock"].calls
        if source == "baostock" and query == "history"
    ]


def saved_request(root, source, query):
    entries = [
        entry
        for entry in MarketRepository(root).states("requests").values()
        if entry["identity"]["source"] == source and entry["identity"]["query"] == query
    ]
    assert len(entries) == 1
    return entries[0]


def test_incremental_update_reuses_previous_days_and_only_requests_new_session(
    market_root,
    sources,
    clock,
):
    first = collect(market_root, sources, clock)
    assert first["state"] == "COMPLETE"
    assert history_calls(sources) == [
        {"code": "SH600000", "start_date": "2026-10-23", "end_date": AS_OF}
    ]
    before = list(sources["baostock"].calls)
    assert collect(market_root, sources, clock)["state"] == "COMPLETE"
    assert sources["baostock"].calls == before

    clock.advance(86400)
    second = collect(market_root, sources, clock, request("2026-10-28"))
    assert second["state"] == "COMPLETE"
    assert history_calls(sources)[-1] == {
        "code": "SH600000",
        "start_date": "2026-10-28",
        "end_date": "2026-10-28",
    }
    rows = MarketRepository(market_root).select("bar", code="SH600000", latest=False)
    assert len(rows) == 4
    assert {row["session"] for row in rows} == {"2026-10-23", "2026-10-26", AS_OF, "2026-10-28"}
    assert not (market_root / ".env").exists()
    assert not (market_root / "configs/default.toml").exists()


def test_partial_source_window_preserves_good_days_then_fetches_only_the_gap(
    market_root,
    sources,
    clock,
):
    primary = sources["baostock"]
    primary.omitted_prices.add(("SH600000", "2026-10-26"))
    primary_only = {"baostock": primary}
    first = collect(market_root, primary_only, clock)
    assert first["state"] == "PARTIAL"
    facts = MarketDataService(market_root).prices(
        "SH600000", "2026-10-23", AS_OF, knowledge_cutoff=clock()
    )
    assert facts.state == "PARTIAL"
    assert {issue["date"] for issue in facts.issues if issue["kind"] == "MISSING_BAR"} == {
        "2026-10-26"
    }
    assert len(facts.data) == 2
    primary.omitted_prices.clear()
    second = collect(market_root, primary_only, clock)
    assert second["state"] == "COMPLETE"
    assert history_calls(sources)[-1] == {
        "code": "SH600000",
        "start_date": "2026-10-26",
        "end_date": "2026-10-26",
    }
    assert len(MarketRepository(market_root).select("bar", code="SH600000", latest=False)) == 3


def test_holdings_orders_and_actions_are_covered_before_optional_candidates(
    market_root, sources, clock
):
    wanted = request(
        candidates=("SH600000", "SZ000001"),
        holdings=("SZ000001",),
        pending_orders=("SH600001",),
        pending_actions=("SZ000002",),
        history_sessions=1,
    )
    state = collect(market_root, sources, clock, wanted)
    expected = ["SZ000001", "SH600001", "SZ000002", "SH600000"]
    assert state["state"] == "COMPLETE"
    assert state["requested_codes"] == expected
    assert [params["code"] for params in history_calls(sources)] == expected
    assert [row["code"] for row in state["securities"]] == expected
    assert all(row["prices"] == "READY" for row in state["securities"])


def test_request_budget_prioritizes_holding_and_resume_does_not_refetch_completed_prices(
    market_root,
    sources,
    clock,
):
    wanted = request(holdings=("SZ000001",), history_sessions=1)
    first = collect(market_root, sources, clock, wanted, max_requests=3)
    assert first["state"] == "PARTIAL"
    assert first["requests_this_run"] == 3
    assert history_calls(sources) == [{"code": "SZ000001", "start_date": AS_OF, "end_date": AS_OF}]
    assert any(issue["kind"] == "BUDGET_PAUSED" for issue in first["issues"])
    second = collect(market_root, sources, clock, wanted, max_requests=3)
    assert second["state"] == "COMPLETE"
    assert second["requests_this_run"] == 1
    assert [row["code"] for row in history_calls(sources)] == ["SZ000001", "SH600000"]


def test_time_budget_stops_network_work_and_resume_finishes(
    market_root, sources, clock, monkeypatch
):
    from china_stock_sim.market import update

    with monkeypatch.context() as patch:
        patch.setattr(
            update.time, "monotonic", lambda: 0.0 if not sources["baostock"].calls else 10.0
        )
        first = collect(market_root, sources, clock, budget_seconds=5)
    assert first["state"] == "PARTIAL"
    assert first["requests_this_run"] == 1
    assert history_calls(sources) == []
    assert collect(market_root, sources, clock)["state"] == "COMPLETE"
    assert len(history_calls(sources)) == 1


def test_network_retry_after_cooldown_is_persisted_and_bounded(market_root, sources, clock):
    failure = ProviderError("fixture HTTP 429", retryable=True)
    failure.retry_after_seconds = 120
    sources["baostock"].failures["history"] = [failure]
    primary_only = {"baostock": sources["baostock"]}
    first = collect(market_root, primary_only, clock)
    assert first["state"] == "PARTIAL"
    failed = saved_request(market_root, "baostock", "history")
    assert failed["category"] == "NETWORK"
    assert failed["attempts"] == 1
    assert datetime.fromisoformat(failed["next_retry_at"]) == clock() + timedelta(seconds=120)
    clock.advance(119)
    waiting = collect(market_root, primary_only, clock)
    assert waiting["state"] == "PARTIAL"
    assert len(history_calls(sources)) == 1
    assert any(issue["kind"] == "COOLDOWN" for issue in waiting["issues"])
    clock.advance(1)
    assert collect(market_root, primary_only, clock)["state"] == "COMPLETE"
    assert len(history_calls(sources)) == 2
    assert saved_request(market_root, "baostock", "history")["attempts"] == 2


def test_invalid_history_is_format_failure_and_corrected_source_can_retry_later(
    market_root, sources, clock
):
    sources["baostock"].invalid_history = True
    primary_only = {"baostock": sources["baostock"]}
    first = collect(market_root, primary_only, clock)
    assert first["state"] == "PARTIAL"
    failed = saved_request(market_root, "baostock", "history")
    assert failed["category"] == "FORMAT"
    assert "rejected_raw_reference" in failed
    assert MarketRepository(market_root).select("bar") == []
    sources["baostock"].invalid_history = False
    collect(market_root, primary_only, clock)
    assert len(history_calls(sources)) == 1
    clock.advance(3600)
    assert collect(market_root, primary_only, clock)["state"] == "COMPLETE"
    assert len(history_calls(sources)) == 2


def test_provider_declared_format_category_is_preserved(market_root, sources, clock):
    sources["baostock"].failures["history"] = [
        ProviderError("source schema changed", category="FORMAT")
    ]
    state = collect(market_root, {"baostock": sources["baostock"]}, clock)
    assert state["state"] == "PARTIAL"
    assert saved_request(market_root, "baostock", "history")["category"] == "FORMAT"


def test_unpublished_prices_are_never_reported_as_complete(market_root, sources, clock):
    for source in sources.values():
        source.empty_prices = True
    state = collect(market_root, sources, clock)
    assert state["state"] == "PARTIAL"
    assert state["securities"][0]["prices"] == "MISSING"
    assert saved_request(market_root, "baostock", "history")["category"] == "UNPUBLISHED"
    assert saved_request(market_root, "tencent", "prices")["category"] == "UNPUBLISHED"
    assert MarketRepository(market_root).select("bar") == []


def test_calendar_gap_stops_price_collection_without_guessing_closed_days(
    market_root, sources, clock
):
    sources["baostock"].omitted_calendar.add("2026-10-25")
    state = collect(market_root, sources, clock)
    assert state["state"] == "WAITING_DATA"
    assert [query for _, query, _ in sources["baostock"].calls] == ["calendar"]
    assert any(issue.get("date") == "2026-10-25" for issue in state["issues"])


def test_indices_only_source_failure_is_not_complete(market_root, sources, clock):
    sources["tencent"].empty_prices = True
    wanted = request(candidates=(), history_sessions=1, include_indices=True)
    state = collect(market_root, sources, clock, wanted)
    assert state["state"] == "PARTIAL"
    assert state["securities"] == []
    assert state["indices_complete"] is False
    assert len(state["indices"]) == 3
    assert all(row["prices"] != "READY" for row in state["indices"])


def test_index_failure_keeps_stock_usable_but_requested_collection_partial(
    market_root, sources, clock
):
    sources["tencent"].empty_prices = True
    state = collect(market_root, sources, clock, request(include_indices=True))
    assert state["state"] == "PARTIAL"
    assert state["state_scope"] == "requested_fact_collection_not_trading_readiness"
    assert state["indices_complete"] is False
    assert state["securities"][0]["prices"] == "READY"
    assert state["securities"][0]["events"] == "NOT_REQUESTED"
    assert state["securities"][0]["event_coverage"] == "NOT_REQUESTED"
    assert state["event_conflicts"] == []
    assert MarketDataService(market_root).readiness(
        "SH600000", AS_OF, "valuation", knowledge_cutoff=clock()
    ).ready
    saved = MarketRepository(market_root).states("jobs")[state["job_id"]]
    assert saved["state"] == "PARTIAL"
    assert saved["securities"][0]["prices"] == "READY"


def test_complete_event_coverage_records_conflict_without_hiding_or_selecting_value(
    market_root, clock
):
    class EventSource(FakeSource):
        def fetch(self, query, params):
            if query not in {"dividends", "events"}:
                return super().fetch(query, params)
            self.calls.append((self.provider_id, query, dict(params)))
            rows = []
            fields = ["code", "dividOperateDate", "dividCashPsBeforeTax"]
            if query == "dividends":
                rows = [{"code": "sh.600000", "dividPlanAnnounceDate": "2026-03-31",
                         "dividPlanDate": "2026-04-20", "dividRegistDate": "2026-04-28",
                         "dividOperateDate": "2026-04-29", "dividPayDate": "2026-04-29",
                         "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.3",
                         "dividStocksPs": "0", "dividReserveToStockPs": "0",
                         "dividCashStock": "10派2元（含税）"}]
                fields = list(rows[0])
            return {"provider": self.provider_id, "adapter_version": self.adapter_version,
                    "fetched_at": self.clock().isoformat(), "fields": fields, "rows": rows}

    calls = []
    sources = {name: EventSource(name, clock, calls) for name in ("baostock", "eastmoney")}
    state = collect(market_root, sources, clock, request(include_events=True))
    assert state["state"] == "COMPLETE"
    assert state["state_scope"] == "requested_fact_collection_not_trading_readiness"
    stock = state["securities"][0]
    assert stock["prices"] == stock["event_coverage"] == "READY"
    assert stock["events"] == "CONFLICT"
    assert stock["event_issues"][0]["kind"] == "EVENT_CONFLICT"
    service = MarketDataService(market_root)
    facts = service.events("SH600000", knowledge_cutoff=clock())
    assert facts.state == "CONFLICT"
    assert facts.data[0]["cash_per_share"] is None
    assert state["event_conflicts"] == [{"code": "SH600000",
        "event_id": facts.data[0]["event_id"], "details": ["CONFLICT_CASH_PER_SHARE"]}]
    saved = MarketRepository(market_root).states("jobs")[state["job_id"]]
    assert saved["event_conflicts"] == state["event_conflicts"]
    assert saved["securities"][0]["event_issues"] == stock["event_issues"]
    before = list(calls)
    resumed = collect(market_root, sources, clock, request(include_events=True), download=False)
    assert calls == before
    assert resumed["state"] == "COMPLETE"
    assert resumed["event_conflicts"] == state["event_conflicts"]


def test_fallback_allows_valuation_but_complete_primary_fields_are_recovered(
    market_root,
    sources,
    clock,
):
    sources["baostock"].failures["history"] = [ProviderError("primary timeout", retryable=True)]
    wanted = request(history_sessions=1)
    first = collect(market_root, sources, clock, wanted)
    assert first["state"] == "COMPLETE"
    service = MarketDataService(market_root)
    prices = service.readiness("SH600000", AS_OF, "valuation", knowledge_cutoff=clock())
    assert prices.ready
    assert prices.data[-1]["_provenance"]["provider"] == "tencent"
    assert prices.data[-1]["tradestatus"] is None
    assert prices.data[-1]["isST"] is None
    assert not service.execution_facts("SH600000", AS_OF, knowledge_cutoff=clock()).ready
    clock.advance(60)
    second = collect(market_root, sources, clock, wanted)
    assert second["state"] == "COMPLETE"
    assert len(history_calls(sources)) == 2
    recovered = service.prices("SH600000", AS_OF, AS_OF, knowledge_cutoff=clock())
    assert recovered.ready
    assert recovered.data[-1]["_provenance"]["provider"] == "baostock"
    assert recovered.data[-1]["tradestatus"] == 1
    assert recovered.data[-1]["volume"] == 10000
    before = list(sources["baostock"].calls)
    assert collect(market_root, sources, clock, wanted)["state"] == "COMPLETE"
    assert sources["baostock"].calls == before


def test_long_window_is_segmented_and_budget_resume_never_repeats_completed_days(
    market_root,
    sources,
    clock,
):
    wanted = request(history_sessions=500)
    first = collect(market_root, sources, clock, wanted, max_requests=3)
    assert first["state"] == "PARTIAL"
    for _ in range(6):
        state = collect(market_root, sources, clock, wanted, max_requests=3)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE"
    calls = history_calls(sources)
    assert len(calls) > 1
    fetched = [
        day
        for params in calls
        for day in fixture_sessions(params["start_date"], params["end_date"])
    ]
    assert len(fetched) == 500
    assert len(set(fetched)) == 500
    assert all(
        len(fixture_sessions(params["start_date"], params["end_date"])) < 500 for params in calls
    )
    assert len(MarketRepository(market_root).select("bar", code="SH600000")) == 500
