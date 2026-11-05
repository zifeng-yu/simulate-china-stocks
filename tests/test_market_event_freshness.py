"""An evening distribution check cannot be laundered from morning evidence."""

from dataclasses import replace
from datetime import datetime

import pytest
from test_market_pending_event_ranges import CODE, run, wanted
from test_market_pending_event_ranges import environment as environment

from china_stock_sim.market.contracts import PendingEventRange
from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

DAY = "2026-10-28"
OLD = PendingEventRange(CODE, "2024-01-01", "2024-12-31")


def evening(environment):
    environment[1].now = datetime.fromisoformat(DAY + "T20:00:00+08:00")


def morning(environment):
    environment[1].now = datetime.fromisoformat(DAY + "T13:00:00+08:00")


def event_calls(environment):
    return [call for call in environment[2] if call[1] in {"events", "dividends"}]


def test_same_natural_day_new_session_reacquires_sources_then_reuses_evening(environment):
    morning(environment)
    yesterday = wanted(candidates=(CODE,))
    assert run(environment, yesterday)["state"] == "COMPLETE"
    assert len(event_calls(environment)) == 2
    evening(environment)
    today = replace(yesterday, as_of=DAY)
    assert run(environment, today)["state"] == "COMPLETE"
    assert len(event_calls(environment)) == 4
    assert run(environment, today)["state"] == "COMPLETE"
    assert len(event_calls(environment)) == 4
    coverage = MarketDataService(environment[0]).event_coverage(CODE, DAY, DAY,
                                                              knowledge_cutoff=environment[1]())
    assert coverage.ready and coverage.data["refreshed_as_of"] == DAY


def test_old_pending_ranges_use_request_day_and_share_fresh_source_responses(environment):
    morning(environment)
    request = wanted(OLD, PendingEventRange(CODE, "2024-02-01", "2024-02-29"))
    assert run(environment, request)["state"] == "COMPLETE"
    assert len(event_calls(environment)) == 2
    evening(environment)
    assert run(environment, replace(request, as_of=DAY))["state"] == "COMPLETE"
    assert len(event_calls(environment)) == 4
    assert run(environment, replace(request, as_of=DAY))["state"] == "COMPLETE"
    assert len(event_calls(environment)) == 4


@pytest.mark.parametrize("provider", ["baostock", "eastmoney"])
@pytest.mark.parametrize("bad_time", ["13:00:00", "21:00:00"])
def test_new_network_response_requires_fresh_nonfuture_acquisition(environment, monkeypatch,
                                                                provider, bad_time):
    evening(environment)
    source = environment[3][provider]
    original = source.fetch

    def stale(query, params):
        return {**original(query, params), "fetched_at": DAY + "T" + bad_time + "+08:00"}

    monkeypatch.setattr(source, "fetch", stale)
    result = run(environment, replace(wanted(OLD), as_of=DAY))
    assert result["state"] == "PARTIAL"
    assert not result["securities"][0]["event_refresh_complete"]
    repository = MarketRepository(environment[0])
    assert repository.select("coverage") == []
    failed = [r for r in repository.states("requests").values() if r["state"] == "FAILED"]
    assert len(failed) == 1 and failed[0]["category"] == "UNPUBLISHED"
    assert failed[0]["rejected_raw_reference"]
    assert all(r["provider"] != provider for r in repository.select("event_batch"))


def test_offline_old_range_cannot_claim_new_daily_refresh_from_morning_coverage(environment):
    morning(environment)
    request = wanted(OLD)
    assert run(environment, request)["state"] == "COMPLETE"
    evening(environment)
    before = list(environment[2])
    result = run(environment, replace(request, as_of=DAY), download=False)
    assert result["state"] == "PARTIAL"
    assert any(issue["kind"] == "EVENT_COVERAGE_STALE" for issue in result["issues"])
    assert environment[2] == before
    assert run(environment, request, download=False)["state"] == "COMPLETE"


def test_failed_evening_request_preserves_valid_history_without_creating_today_coverage(environment):
    morning(environment)
    previous = wanted(candidates=(CODE,))
    assert run(environment, previous)["state"] == "COMPLETE"
    evening(environment)
    environment[3]["baostock"].failures["dividends"] = [ProviderError("offline", retryable=True)]
    result = run(environment, replace(previous, as_of=DAY))
    assert result["state"] == "PARTIAL"
    service = MarketDataService(environment[0])
    assert service.event_coverage(CODE, previous.as_of, previous.as_of,
                                  knowledge_cutoff=environment[1]()).ready
    assert not service.event_coverage(CODE, DAY, DAY, knowledge_cutoff=environment[1]()).ready


@pytest.mark.parametrize("leaf_time,expected", [
    ("2026-10-28T13:00:00+08:00", "WAITING_DATA"),
    ("2026-10-28T18:09:59+08:00", "WAITING_DATA"),
    ("2026-10-28T18:10:00+08:00", "READY"),
    ("2026-10-28T10:10:00+00:00", "READY"),
])
def test_legacy_synthetic_coverage_checks_nested_original_times(environment, leaf_time, expected):
    evening(environment)
    repository = MarketRepository(environment[0])
    leaf = repository.save_raw({"provider": "baostock", "fetched_at": leaf_time, "rows": []})
    intermediate = repository.save_raw({"provider": "combined", "fetched_at": DAY + "T19:00:00+08:00",
                                        "dependencies": [leaf]})
    raw = {"provider": "distribution_sources", "fetched_at": DAY + "T20:00:00+08:00",
           "dependencies": [intermediate]}
    row = {"code": CODE, "date": DAY, "purpose": "events", "start": "2026-04-30", "end": DAY,
           "complete": True, "scope": "published_cash_bonus_capital_distributions"}
    repository.put("coverage", [row], raw)
    service = MarketDataService(environment[0])
    result = service.event_coverage(CODE, DAY, DAY, knowledge_cutoff=environment[1]())
    assert result.state == expected
    if expected == "WAITING_DATA":
        assert result.issues[0]["raw_reference"] == leaf
        assert run(environment, replace(wanted(), as_of=DAY,
                   pending_event_ranges=(PendingEventRange(CODE, DAY, DAY),)),
                   download=False)["state"] == "PARTIAL"
    # A real morning fetch still supports yesterday, not a blanket invalidation.
    old_raw = {**raw, "fetched_at": DAY + "T19:30:00+08:00"}
    repository.put("coverage", [{**row, "date": "2026-10-27", "end": "2026-10-27"}], old_raw)
    assert service.event_coverage(CODE, "2026-10-27", "2026-10-27",
                                  knowledge_cutoff=DAY + "T19:30:00+08:00").ready
