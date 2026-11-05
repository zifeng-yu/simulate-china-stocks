"""Acceptance distinguishes saved facts from the latest actual collection attempt."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.repository import MarketRepository

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_data_sync.py"
SPEC = importlib.util.spec_from_file_location("verify_data_sync", SCRIPT)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
DAY, CODE = "2026-10-29", "SH600000"


class SavedFacts:
    def __init__(self, *, event_state="READY", full_fields=True):
        self.event_state, self.full_fields = event_state, full_fields
        end = date.fromisoformat(DAY)
        self.sessions = [(end - timedelta(days=n)).isoformat() for n in range(120, -1, -1)]

    def pool(self, *args, **kwargs):
        return DataResult("READY", {"selected": [{"code": CODE}]})

    def calendar_operation(self, *args, **kwargs):
        return DataResult("READY", self.sessions)

    def prices(self, code, start, end, **kwargs):
        rows = [{"code": code, "date": day, "close": "10.00",
                 "_provenance": {"provider": "fixture"}}
                for day in self.sessions if start <= day <= end]
        if self.full_fields:
            for row in rows:
                row.update(preclose="9.90", volume=10000, amount="100000.00", tradestatus=1, isST=0)
        return DataResult("READY", rows)

    def events(self, *args, **kwargs):
        issues = ({"kind": self.event_state},) if self.event_state != "READY" else ()
        return DataResult(self.event_state, [], issues)

    def event_coverage(self, *args, **kwargs):
        return DataResult("READY", {"complete": True})

    def readiness(self, code, day, purpose, **kwargs):
        state = "WAITING_DATA" if not self.full_fields and purpose in ("rules", "llm") else "READY"
        return DataResult(state, [])

    def index_quote(self, *args, **kwargs):
        return DataResult("READY", {"close": "1000.00"})


def add_job(repository, identity, hour, state="COMPLETE", refresh=True):
    repository.save_state("jobs", identity, {
        "job_id": identity, "as_of": DAY, "state": state, "requested_codes": [CODE],
        "started_at": f"{DAY}T{hour}:00:00+08:00", "updated_at": f"{DAY}T{hour}:05:00+08:00",
        "issues": [], "requests_this_run": 1,
        "securities": [{"code": CODE, "event_refresh_complete": refresh,
                        "event_coverage": "READY"}],
    })


def check(tmp_path, monkeypatch, facts):
    monkeypatch.setattr(VERIFY, "MarketDataService", lambda root: facts)
    database = tmp_path / "data/state/market.sqlite3"
    before = database.read_bytes()
    result = VERIFY.verify(tmp_path, DAY)
    assert database.read_bytes() == before
    assert result["network_calls"] == result["llm_calls"] == 0
    return result


@pytest.mark.parametrize("latest_state", ["PARTIAL", "PAUSED", "RUNNING", "ERROR"])
def test_later_failed_or_interrupted_refresh_cannot_be_hidden_by_ready_old_facts(
    tmp_path, monkeypatch, latest_state
):
    repository = MarketRepository(tmp_path)
    add_job(repository, "earlier-success", "19")
    add_job(repository, "latest-incomplete", "20", state=latest_state, refresh=False)
    result = check(tmp_path, monkeypatch, SavedFacts())
    assert result["fact_coverage_state"] == "COMPLETE"
    assert result["latest_collection_job"]["job_id"] == "latest-incomplete"
    assert result["latest_refresh_complete"] is False
    assert result["collection_state"] == "PARTIAL"


def test_corrupt_events_fail_acceptance_despite_ready_price_coverage_and_successful_job(
    tmp_path, monkeypatch
):
    add_job(MarketRepository(tmp_path), "success", "20")
    result = check(tmp_path, monkeypatch, SavedFacts(event_state="CORRUPT"))
    assert result["summary"]["coverage_states"] == {"READY": 1}
    assert result["summary"]["event_states"] == {"CORRUPT": 1}
    assert result["latest_refresh_complete"] is True
    assert result["fact_coverage_state"] == result["collection_state"] == "PARTIAL"


@pytest.mark.parametrize("event_state", ["READY", "CONFLICT"])
def test_price_only_success_reports_zero_complete_field_sets_without_claiming_strategy_ready(
    tmp_path, monkeypatch, event_state
):
    add_job(MarketRepository(tmp_path), "success", "20")
    result = check(tmp_path, monkeypatch, SavedFacts(event_state=event_state, full_fields=False))
    assert result["collection_state"] == result["fact_coverage_state"] == "COMPLETE"
    assert result["summary"]["today_price_ready"] == 1
    assert result["summary"]["today_full_fields"] == 0
    assert result["summary"]["history_rows"] == 121
    assert result["details"][0]["missing_full_fields"] == [
        "preclose", "volume", "amount", "tradestatus", "isST"]
    assert result["summary"]["purpose_states"]["rules"] == {"WAITING_DATA": 1}


def test_later_success_resolves_earlier_failed_job_without_erasing_history(tmp_path, monkeypatch):
    repository = MarketRepository(tmp_path)
    add_job(repository, "earlier-failure", "19", state="PARTIAL", refresh=False)
    add_job(repository, "latest-success", "20")
    # An unrelated future-calendar request may remain failed without invalidating
    # the price/event/index scope proven by the later job and current saved facts.
    repository.save_state("requests", "future-calendar", {
        "state": "FAILED", "started_at": f"{DAY}T19:00:00+08:00",
        "identity": {"source": "baostock", "query": "calendar", "params": {}}})
    result = check(tmp_path, monkeypatch, SavedFacts())
    assert result["latest_collection_job"]["job_id"] == "latest-success"
    assert result["collection_state"] == "COMPLETE"
    assert [job["state"] for job in result["jobs"]] == ["PARTIAL", "COMPLETE"]
    assert result["request_states"] == {"FAILED": 1}
    assert len(result["pending_requests"]) == 1


def test_success_label_without_per_security_refresh_proof_is_not_sufficient(tmp_path, monkeypatch):
    add_job(MarketRepository(tmp_path), "inconsistent-success", "20", refresh=False)
    result = check(tmp_path, monkeypatch, SavedFacts())
    assert result["fact_coverage_state"] == "COMPLETE"
    assert result["latest_refresh_complete"] is False
    assert result["collection_state"] == "PARTIAL"


@pytest.mark.parametrize("resolved", [False, True])
def test_later_subset_failure_cannot_hide_behind_whole_pool_success(tmp_path, monkeypatch, resolved):
    repository = MarketRepository(tmp_path)
    add_job(repository, "whole-pool", "18")
    whole = repository.states("jobs")["whole-pool"]
    whole["requested_codes"].append("SZ000001")
    whole["securities"].append({"code": "SZ000001", "event_refresh_complete": True,
                                "event_coverage": "READY"})
    repository.save_state("jobs", "whole-pool", whole)
    add_job(repository, "subset-failed", "19", state="PARTIAL", refresh=False)
    if resolved:
        add_job(repository, "subset-recovered", "20")
    facts = SavedFacts()
    facts.pool = lambda *a, **k: DataResult("READY", {"selected": [
        {"code": CODE}, {"code": "SZ000001"}]})
    result = check(tmp_path, monkeypatch, facts)
    assert result["latest_collection_job"]["job_id"] == "whole-pool"
    assert result["collection_state"] == ("COMPLETE" if resolved else "PARTIAL")
    assert bool(result["later_scope_incomplete"]) is not resolved
