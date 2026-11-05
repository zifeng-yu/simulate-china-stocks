"""Unfinished actions refresh independently of price history and account holdings."""
from dataclasses import asdict

import pytest
from test_market_recovery import root_with_config
from test_market_update import AS_OF, Clock, FakeSource

from china_stock_sim.market.contracts import DataRequest, PendingEventRange
from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.update import MarketUpdater

CODE = "SH600000"
EXITED = "SZ000001"


class EventSource(FakeSource):
    def __init__(self, *args):
        super().__init__(*args)
        self.fail_years = {}

    def fetch(self, query, params):
        if query not in {"dividends", "events"}:
            return super().fetch(query, params)
        self.calls.append((self.provider_id, query, dict(params)))
        failures = self.fail_years.get(params.get("year"), self.failures.get(query, []))
        if failures:
            raise failures.pop(0)
        return {"provider": self.provider_id, "adapter_version": self.adapter_version,
                "fetched_at": self.clock().isoformat(), "rows": [],
                "fields": ["code", "dividOperateDate", "dividCashPsBeforeTax"]}


@pytest.fixture
def environment(tmp_path):
    clock, calls = Clock(), []
    sources = {name: EventSource(name, clock, calls) for name in ("baostock", "eastmoney")}
    return root_with_config(tmp_path), clock, calls, sources


def wanted(*ranges, candidates=(), **kwargs):
    return DataRequest(AS_OF, candidates=candidates, pending_event_ranges=tuple(ranges),
                       history_sessions=3, include_indices=False, **kwargs)


def run(environment, request, **options):
    root, clock, _, sources = environment
    return MarketUpdater(root, clock=clock, providers=sources, **options).run(request)


def test_contract_normalization_priority_and_serialization():
    old = PendingEventRange("sh.600000", "2024-01-01", "2024-12-31", ["event", "event"])
    assert old.code == CODE and old.event_ids == ("event",)
    request = wanted(old, old, candidates=(CODE, "SH600001"), holdings=(EXITED,))
    assert request.pending_event_ranges == (old,)
    assert request.ordered_codes == (EXITED, CODE, "SH600001")
    assert asdict(request)["pending_event_ranges"][0]["start"] == "2024-01-01"
    with pytest.raises(ValueError, match="include_events"):
        wanted(old, include_events=False)
    with pytest.raises(ValueError, match="as_of"):
        wanted(PendingEventRange(CODE, AS_OF, "2026-10-28"))
    with pytest.raises(ValueError, match="PendingEventRange"):
        wanted({"code": CODE, "start": "2024-01-01", "end": "2024-12-31"})


@pytest.mark.parametrize("changes", [
    {"code": "bad"}, {"start": "2025-01-01"}, {"start": "2024-1-1"},
    {"end": "2024-02-30"}, {"event_ids": "event"}, {"event_ids": [None]},
    {"event_ids": [""]}, {"event_ids": [" "]}, {"event_ids": ["x" * 257]},
])
def test_contract_rejects_invalid_ranges(changes):
    values = dict(code=CODE, start="2024-01-01", end="2024-12-31")
    values.update(changes)
    with pytest.raises((ValueError, TypeError)):
        PendingEventRange(**values)


def test_exited_stock_range_is_event_only_without_account_or_calendar(environment):
    old = PendingEventRange(EXITED, "2017-06-01", "2017-07-31", ("unpaid-source-id",))
    result = run(environment, wanted(old))
    root, clock, calls, _ = environment
    assert result["state"] == "COMPLETE"
    assert [query for _, query, _ in calls] == ["dividends", "events"]
    assert result["sessions"] == []
    stock = result["securities"][0]
    assert stock["prices"] == stock["event_coverage"] == "NOT_REQUESTED"
    assert stock["pending_event_ranges"][0]["state"] == "READY"
    assert stock["pending_event_ranges"][0]["event_ids"] == old.event_ids
    assert MarketDataService(root).event_coverage(EXITED, old.start, old.end,
                                                 knowledge_cutoff=clock()).ready
    assert not (root / "data/state/ledger.sqlite3").exists()
    assert not (root / ".env").exists()
    assert MarketRepository(root).select("bar") == []
    saved = list(MarketRepository(root).states("jobs").values())[-1]
    assert saved["pending_event_ranges"][0]["code"] == EXITED


def test_old_ranges_add_only_distribution_years_preserving_price_observations(environment):
    assert run(environment, wanted(candidates=(CODE,)))["state"] == "COMPLETE"
    root, clock, calls, _ = environment
    repository = MarketRepository(root)
    original_prices = repository.select("bar", latest=False)
    before = len(calls)
    old = PendingEventRange(CODE, "2023-12-20", "2024-02-15")
    result = run(environment, wanted(old, candidates=(CODE,)))
    assert result["state"] == "COMPLETE"
    assert calls[before:] == [
        ("baostock", "dividends", {"code": CODE, "year": 2023}),
        ("baostock", "dividends", {"code": CODE, "year": 2024}),
    ]
    assert repository.select("bar", latest=False) == original_prices
    assert MarketDataService(root).event_coverage(CODE, old.start, old.end,
                                                 knowledge_cutoff=clock()).ready
    before = len(calls)
    assert run(environment, wanted(old, candidates=(CODE,)), download=False)["state"] == "COMPLETE"
    assert len(calls) == before


def test_overlapping_ranges_share_requests_but_preserve_each_range_coverage(environment):
    first = PendingEventRange(CODE, "2023-06-01", "2024-01-31")
    second = PendingEventRange(CODE, "2024-01-01", "2024-01-31")
    third = PendingEventRange(CODE, "2024-01-01", "2025-07-01")
    result = run(environment, wanted(first, second, third))
    root, _, calls, _ = environment
    assert [p["year"] for _, q, p in calls if q == "dividends"] == [2023, 2024, 2025]
    assert sum(q == "events" for _, q, _ in calls) == 1
    assert result["state"] == "COMPLETE"
    coverage = MarketRepository(root).select("coverage", code=CODE)
    assert {(r["payload"]["start"], r["payload"]["end"]) for r in coverage} == {
        (item.start, item.end) for item in (first, second, third)}
    assert len(result["securities"][0]["pending_event_ranges"]) == 3


def test_failed_refresh_cannot_hide_behind_older_successful_coverage(environment):
    old = PendingEventRange(CODE, "2024-01-01", "2024-12-31")
    request = wanted(old)
    assert run(environment, request)["state"] == "COMPLETE"
    root, clock, calls, sources = environment
    clock.advance(86400)
    sources["baostock"].fail_years[2024] = [ProviderError("offline", retryable=True)]
    failed = run(environment, request)
    assert failed["state"] == "PARTIAL"
    scope = failed["securities"][0]["pending_event_ranges"][0]
    assert scope["coverage"]["state"] == "READY"
    assert not scope["refresh_complete"] and scope["state"] == "WAITING_DATA"
    assert any(issue["kind"] == "NETWORK" for issue in failed["issues"])
    before = len(calls)
    assert run(environment, request)["state"] == "PARTIAL"
    assert len(calls) == before
    offline = run(environment, request, download=False)
    assert offline["state"] == "PARTIAL"
    assert any(issue["kind"] == "EVENT_REFRESH_INCOMPLETE" for issue in offline["issues"])
    clock.advance(3600)
    assert run(environment, request)["state"] == "COMPLETE"
    assert calls[before:] == [("baostock", "dividends", {"code": CODE, "year": 2024})]
    assert MarketDataService(root).event_coverage(CODE, old.start, old.end,
                                                 knowledge_cutoff=clock()).ready


def test_same_day_failed_explicit_refresh_is_not_masked_by_coverage(environment):
    request = wanted(PendingEventRange(CODE, "2024-01-01", "2024-12-31"))
    assert run(environment, request)["state"] == "COMPLETE"
    _, clock, _, sources = environment
    clock.advance(1)
    sources["baostock"].fail_years[2024] = [ProviderError("retry", retryable=True)]
    assert run(environment, request, refresh=True)["state"] == "PARTIAL"
    assert run(environment, request)["state"] == "PARTIAL"


def test_budget_pause_resume_reuses_successful_year(environment):
    old = PendingEventRange(CODE, "2023-06-01", "2024-01-31")
    first = run(environment, wanted(old), max_requests=1)
    assert first["state"] == "PARTIAL"
    assert first["securities"][0]["pending_event_ranges"][0]["state"] == "WAITING_DATA"
    assert any(issue["kind"] == "BUDGET_PAUSED" for issue in first["issues"])
    assert run(environment, wanted(old))["state"] == "COMPLETE"
    calls = environment[2]
    assert sum(q == "dividends" and p["year"] == 2023 for _, q, p in calls) == 1
    assert sum(q == "dividends" and p["year"] == 2024 for _, q, p in calls) == 1


def test_cache_only_missing_range_never_uses_nearby_coverage(environment):
    old = PendingEventRange(CODE, "2024-01-01", "2024-12-31")
    assert run(environment, wanted(old))["state"] == "COMPLETE"
    before = len(environment[2])
    missing = PendingEventRange(CODE, "2022-01-01", "2022-12-31")
    result = run(environment, wanted(missing), download=False)
    assert result["state"] == "PARTIAL"
    scope = result["securities"][0]["pending_event_ranges"][0]
    assert scope["coverage"]["state"] == "WAITING_DATA"
    assert len(environment[2]) == before


def test_calendar_failure_does_not_block_explicit_historical_action_refresh(environment):
    root, clock, calls, sources = environment
    sources["baostock"].omitted_calendar.add(AS_OF)
    old = PendingEventRange(EXITED, "2017-06-01", "2017-07-31")
    request = DataRequest(AS_OF, candidates=(CODE,), history_sessions=3,
                          pending_event_ranges=(old,), include_indices=True)
    result = run(environment, request)
    assert result["state"] == "PARTIAL"
    stocks = {item["code"]: item for item in result["securities"]}
    assert stocks[CODE]["prices"] == "WAITING_DATA"
    assert stocks[EXITED]["pending_event_ranges"][0]["state"] == "READY"
    assert all(item["prices"] == "WAITING_DATA" for item in result["indices"])
    assert MarketDataService(root).event_coverage(EXITED, old.start, old.end,
                                                 knowledge_cutoff=clock()).ready
    assert not any(query in {"history", "prices"} for _, query, _ in calls)


def test_budget_paused_refresh_survives_offline_recheck_of_old_coverage(environment):
    old = PendingEventRange(CODE, "2023-12-01", "2024-02-01")
    request = wanted(old)
    assert run(environment, request)["state"] == "COMPLETE"
    root, clock, calls, _ = environment
    clock.advance(86400)
    paused = run(environment, request, max_requests=1)
    assert paused["state"] == "PARTIAL"
    requests = MarketRepository(root).states("requests").values()
    unfinished = [item for item in requests if item["state"] == "PAUSED"]
    assert len(unfinished) == 2
    assert all(item["attempts"] == 1 for item in unfinished)
    before = len(calls)
    offline = run(environment, request, download=False)
    assert offline["state"] == "PARTIAL"
    assert not offline["securities"][0]["pending_event_ranges"][0]["refresh_complete"]
    assert len(calls) == before
    assert run(environment, request)["state"] == "COMPLETE"
    assert len(calls) == before + 2
    assert run(environment, request, download=False)["state"] == "COMPLETE"


def test_unavailable_source_refresh_remains_pending_until_provider_returns(environment):
    request = wanted(PendingEventRange(CODE, "2024-01-01", "2024-12-31"))
    assert run(environment, request)["state"] == "COMPLETE"
    root, clock, calls, sources = environment
    clock.advance(86400)
    saved = sources.pop("eastmoney")
    assert run(environment, request)["state"] == "PARTIAL"
    assert run(environment, request, download=False)["state"] == "PARTIAL"
    blocked = [item for item in MarketRepository(root).states("requests").values()
               if item.get("pause_reason") == "SOURCE_UNAVAILABLE"]
    assert len(blocked) == 1 and blocked[0]["state"] == "PAUSED"
    sources["eastmoney"] = saved
    before = len(calls)
    assert run(environment, request)["state"] == "COMPLETE"
    assert calls[before:] == [("eastmoney", "events", {"code": CODE})]
    assert run(environment, request, download=False)["state"] == "COMPLETE"


def test_restored_source_same_day_cache_clears_adapter_unavailable_marker(environment):
    request = wanted(PendingEventRange(CODE, "2024-01-01", "2024-12-31"))
    assert run(environment, request)["state"] == "COMPLETE"
    _, clock, calls, sources = environment
    clock.advance(1)
    saved = sources.pop("eastmoney")
    assert run(environment, request)["state"] == "PARTIAL"
    sources["eastmoney"] = saved
    before = len(calls)
    assert run(environment, request)["state"] == "COMPLETE"
    assert run(environment, request, download=False)["state"] == "COMPLETE"
    assert len(calls) == before
