"""Request retry streaks are independent of lifetime successful collection."""

from datetime import datetime, timedelta

import pytest
from test_market_pending_event_ranges import CODE, run, wanted
from test_market_pending_event_ranges import environment as environment

from china_stock_sim.market.contracts import PendingEventRange
from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.providers.http import HTTPProviderError
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.storage import read_json

REQUEST = wanted(PendingEventRange(CODE, "2024-01-01", "2024-12-31"))


def event_request(environment, code=CODE):
    return next((key, row) for key, row in MarketRepository(environment[0]).states("requests").items()
                if row["identity"]["source"] == "eastmoney"
                and row["identity"]["params"]["code"] == code)


def fail_next(environment, category="NETWORK"):
    environment[3]["eastmoney"].failures["events"] = [
        ProviderError("isolated source failure", retryable=category in {"NETWORK", "UNPUBLISHED"},
                      category=category)]


def delay(environment, row):
    return (datetime.fromisoformat(row["next_retry_at"]) - environment[1]()).total_seconds()


@pytest.mark.parametrize("successes", [1, 2, 5])
@pytest.mark.parametrize("category", ["NETWORK", "UNPUBLISHED"])
def test_successful_daily_refreshes_do_not_escalate_first_failure(environment, successes, category):
    for attempt in range(1, successes + 1):
        assert run(environment, REQUEST)["state"] == "COMPLETE"
        _, row = event_request(environment)
        assert row["attempts"] == attempt and row["retry_failures"] == 0
        environment[1].advance(86400)
    fail_next(environment, category)
    assert run(environment, REQUEST)["state"] == "PARTIAL"
    _, row = event_request(environment)
    assert row["attempts"] == successes + 1 and row["retry_failures"] == 1
    assert delay(environment, row) == 60


def test_retryable_failures_escalate_and_success_resets_streak(environment):
    for failure, seconds in enumerate((60, 300, 900, 900), 1):
        fail_next(environment, "NETWORK" if failure % 2 else "UNPUBLISHED")
        assert run(environment, REQUEST)["state"] == "PARTIAL"
        _, row = event_request(environment)
        assert row["attempts"] == row["retry_failures"] == failure
        assert delay(environment, row) == seconds
        before = list(environment[2])
        environment[1].advance(seconds - 1)
        assert run(environment, REQUEST)["state"] == "PARTIAL"
        assert environment[2] == before and event_request(environment)[1] == row
        environment[1].advance(1)
    assert run(environment, REQUEST)["state"] == "COMPLETE"
    _, recovered = event_request(environment)
    assert recovered["attempts"] == 5 and recovered["retry_failures"] == 0
    assert "next_retry_at" not in recovered
    environment[1].advance(86400)
    fail_next(environment)
    assert run(environment, REQUEST)["state"] == "PARTIAL"
    _, row = event_request(environment)
    assert row["attempts"] == 6 and row["retry_failures"] == 1
    assert delay(environment, row) == 60


@pytest.mark.parametrize("category", ["FORMAT", "CONFIGURATION"])
def test_nonretryable_failure_does_not_escalate_temporary_failure_streak(environment, category):
    fail_next(environment)
    run(environment, REQUEST)
    environment[1].advance(60)
    fail_next(environment, category)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["retry_failures"] == 0 and delay(environment, row) == 3600
    environment[1].advance(3600)
    fail_next(environment)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["attempts"] == 3 and row["retry_failures"] == 1
    assert delay(environment, row) == 60


def test_budget_pause_preserves_streak_without_consuming_attempt(environment):
    fail_next(environment)
    run(environment, REQUEST)
    environment[1].advance(60)
    assert run(environment, REQUEST, refresh=True, max_requests=1)["state"] == "PARTIAL"
    _, paused = event_request(environment)
    assert paused["pause_reason"] == "BUDGET_PAUSED"
    assert paused["attempts"] == paused["retry_failures"] == 1
    fail_next(environment)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["attempts"] == row["retry_failures"] == 2
    assert delay(environment, row) == 300


def test_source_cooldown_pause_does_not_count_as_failed_request(environment):
    codes = tuple(f"SH{600000 + number:06d}" for number in range(4))
    request = wanted(*(PendingEventRange(code, "2024-01-01", "2024-12-31") for code in codes))
    environment[3]["eastmoney"].failures["events"] = [
        ProviderError("isolated outage", retryable=True) for _ in range(3)]
    run(environment, request)
    _, paused = event_request(environment, codes[-1])
    assert paused["pause_reason"] == "SOURCE_COOLDOWN"
    assert paused["attempts"] == paused["retry_failures"] == 0
    environment[1].advance(60)
    fail_next(environment)
    run(environment, wanted(PendingEventRange(codes[-1], "2024-01-01", "2024-12-31")))
    _, row = event_request(environment, codes[-1])
    assert row["attempts"] == row["retry_failures"] == 1
    assert delay(environment, row) == 60


@pytest.mark.parametrize("failures", [0, 2])
def test_interrupted_attempt_keeps_streak_and_restart_only_counts_actual_failure(environment, failures):
    for seconds in (60, 300)[:failures]:
        fail_next(environment)
        run(environment, REQUEST)
        environment[1].advance(seconds)
    environment[3]["eastmoney"].failures["events"] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        run(environment, REQUEST)
    _, paused = event_request(environment)
    assert paused["state"] == "PAUSED" and paused["pause_reason"] == "INTERRUPTED"
    assert paused["attempts"] == failures + 1 and paused["retry_failures"] == failures
    fail_next(environment)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["attempts"] == failures + 2 and row["retry_failures"] == failures + 1
    assert delay(environment, row) == (60 if not failures else 900)


def test_stale_running_request_preserves_streak_and_saved_deadline(environment):
    fail_next(environment)
    run(environment, REQUEST)
    key, row = event_request(environment)
    deadline = (environment[1]() + timedelta(seconds=300)).isoformat()
    row.update(state="RUNNING", attempts=17, retry_failures=2, next_retry_at=deadline)
    MarketRepository(environment[0]).save_state("requests", key, row)
    before = list(environment[2])
    assert run(environment, REQUEST)["state"] == "PARTIAL"
    _, paused = event_request(environment)
    assert paused["pause_reason"] == "INTERRUPTED_PREVIOUS_RUN"
    assert paused["attempts"] == 17 and paused["retry_failures"] == 2
    assert paused["next_retry_at"] == deadline and environment[2] == before
    environment[1].advance(300)
    fail_next(environment)
    run(environment, REQUEST)
    _, retried = event_request(environment)
    assert retried["attempts"] == 18 and retried["retry_failures"] == 3
    assert delay(environment, retried) == 900


def test_legacy_success_starts_first_retry_at_one(environment):
    assert run(environment, REQUEST)["state"] == "COMPLETE"
    key, legacy = event_request(environment)
    legacy.pop("retry_failures")
    legacy["attempts"] = 40
    MarketRepository(environment[0]).save_state("requests", key, legacy)
    environment[1].advance(86400)
    fail_next(environment)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["attempts"] == 41 and row["retry_failures"] == 1
    assert delay(environment, row) == 60


def test_legacy_success_then_source_pause_starts_first_failure_at_one(environment):
    codes = tuple(f"SH{600000 + number:06d}" for number in range(4))
    request = wanted(*(PendingEventRange(code, "2024-01-01", "2024-12-31") for code in codes))
    for _ in range(2):
        assert run(environment, request)["state"] == "COMPLETE"
        environment[1].advance(86400)
    environment[3]["eastmoney"].failures["events"] = [
        ProviderError("isolated outage", retryable=True) for _ in range(3)]
    run(environment, request)
    key, legacy = event_request(environment, codes[-1])
    assert legacy["pause_reason"] == "SOURCE_COOLDOWN" and legacy["attempts"] == 2
    assert legacy["raw_reference"] and legacy["finished_at"]
    legacy.pop("retry_failures")
    MarketRepository(environment[0]).save_state("requests", key, legacy)
    target = wanted(PendingEventRange(codes[-1], "2024-01-01", "2024-12-31"))
    before = list(environment[2])
    assert run(environment, target)["state"] == "PARTIAL"
    assert environment[2] == before and event_request(environment, codes[-1])[1] == legacy
    environment[1].advance(60)
    fail_next(environment)
    run(environment, target)
    _, row = event_request(environment, codes[-1])
    assert row["attempts"] == 3 and row["retry_failures"] == 1
    assert delay(environment, row) == 60


@pytest.mark.parametrize("defect", [
    "missing_reference", "corrupt_raw", "wrong_provider", "wrong_adapter", "late_raw",
    "late_finish", "missing_finish", "error", "category", "rejected_raw_reference",
    "unknown_pause",
])
def test_legacy_pause_without_verified_success_keeps_conservative_tier(environment, defect):
    assert run(environment, REQUEST)["state"] == "COMPLETE"
    key, legacy = event_request(environment)
    legacy.pop("retry_failures")
    environment[1].advance(86400)
    legacy.update(state="PAUSED", pause_reason="SOURCE_COOLDOWN", attempts=5, retryable=True,
                  started_at=environment[1]().isoformat(),
                  next_retry_at=(environment[1]() + timedelta(seconds=60)).isoformat())
    repository = MarketRepository(environment[0])
    if defect == "missing_reference":
        legacy.pop("raw_reference")
    elif defect == "corrupt_raw":
        (environment[0] / legacy["raw_reference"]).write_text("broken original")
    elif defect in {"wrong_provider", "wrong_adapter", "late_raw"}:
        raw = read_json(environment[0] / legacy["raw_reference"])
        if defect == "wrong_provider":
            raw["provider"] = "unrelated_source"
        elif defect == "wrong_adapter":
            raw["adapter_version"] = "unrelated_adapter"
        else:
            raw["fetched_at"] = environment[1]().isoformat()
        legacy["raw_reference"] = repository.save_raw(raw)
    elif defect == "late_finish":
        legacy["finished_at"] = (environment[1]() + timedelta(seconds=1)).isoformat()
    elif defect == "missing_finish":
        legacy.pop("finished_at")
    elif defect == "unknown_pause":
        legacy["pause_reason"] = "UNKNOWN"
    else:
        legacy[defect] = "prior failure evidence"
    repository.save_state("requests", key, legacy)
    run(environment, REQUEST)
    assert event_request(environment)[1] == legacy
    environment[1].advance(60)
    fail_next(environment)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["attempts"] == row["retry_failures"] == 6
    assert delay(environment, row) == 900


@pytest.mark.parametrize("legacy_state", ["FAILED", "PAUSED"])
def test_legacy_failure_keeps_existing_deadline_and_conservative_retry_tier(environment, legacy_state):
    fail_next(environment)
    run(environment, REQUEST)
    key, legacy = event_request(environment)
    legacy.pop("retry_failures")
    legacy.update(state=legacy_state, attempts=5,
                  next_retry_at=(environment[1]() + timedelta(seconds=900)).isoformat())
    MarketRepository(environment[0]).save_state("requests", key, legacy)
    before = list(environment[2])
    run(environment, REQUEST)
    assert environment[2] == before and event_request(environment)[1] == legacy
    environment[1].advance(900)
    fail_next(environment)
    run(environment, REQUEST)
    _, row = event_request(environment)
    assert row["attempts"] == row["retry_failures"] == 6
    assert delay(environment, row) == 900
    environment[1].advance(900)
    assert run(environment, REQUEST)["state"] == "COMPLETE"
    assert event_request(environment)[1]["retry_failures"] == 0


@pytest.mark.parametrize("options", [{}, {"refresh": True}, {"repair": True}])
def test_server_retry_after_stays_nonbypassable_with_independent_counter(environment, options):
    for _ in range(2):
        assert run(environment, REQUEST)["state"] == "COMPLETE"
        environment[1].advance(86400)
    environment[3]["eastmoney"].failures["events"] = [
        HTTPProviderError("isolated rate limit", retryable=True,
                          retry_after_seconds=600, status_code=429)]
    run(environment, REQUEST)
    _, failed = event_request(environment)
    assert failed["attempts"] == 3 and failed["retry_failures"] == 1
    assert delay(environment, failed) == 600
    assert failed["next_retry_at"] == failed["server_retry_at"]
    count = sum(query == "events" for _, query, _ in environment[2])
    environment[1].advance(599)
    result = run(environment, REQUEST, **options)
    assert event_request(environment)[1] == failed
    assert sum(query == "events" for _, query, _ in environment[2]) == count
    assert any(issue.get("origin") == "server" for issue in result["issues"])
    environment[1].advance(1)
    assert run(environment, REQUEST, **options)["state"] == "COMPLETE"
    _, recovered = event_request(environment)
    assert recovered["retry_failures"] == 0 and "server_retry_at" not in recovered


@pytest.mark.parametrize("options", [{"refresh": True}, {"repair": True}])
def test_explicit_local_retry_override_keeps_existing_flag_semantics(environment, options):
    fail_next(environment)
    run(environment, REQUEST)
    fail_next(environment)
    run(environment, REQUEST, **options)
    _, row = event_request(environment)
    assert row["attempts"] == row["retry_failures"] == 2
    assert delay(environment, row) == 300
