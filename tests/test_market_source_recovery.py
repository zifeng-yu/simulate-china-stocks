"""A failed source cannot consume the entire batch; interruption remains resumable."""

from datetime import timedelta

import pytest
from test_market_recovery import root_with_config
from test_market_update import Clock, FakeSource, collect, request

from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.update import MarketUpdater


class ScriptedSource(FakeSource):
    def __init__(self, clock, calls, outcomes):
        super().__init__("baostock", clock, calls)
        self.outcomes = list(outcomes)

    def fetch(self, query, params):
        if query == "history" and self.outcomes:
            failure = self.outcomes.pop(0)
            if failure is not None:
                self.calls.append((self.provider_id, query, dict(params)))
                raise failure
        return super().fetch(query, params)


def network_error():
    return ProviderError("source login unavailable", retryable=True)


def setup_sources(tmp_path, outcomes, backup_type=FakeSource):
    root, clock, calls = root_with_config(tmp_path), Clock(), []
    providers = {"baostock": ScriptedSource(clock, calls, outcomes),
                 "tencent": backup_type("tencent", clock, calls)}
    return root, clock, calls, providers


def stock_codes(count):
    return tuple(f"SH{600000 + n:06d}" for n in range(count))


def history_requests(root):
    return [r for r in MarketRepository(root).states("requests").values()
            if r["identity"]["query"] == "history"]


def test_source_outage_pauses_other_primary_requests_but_fallback_continues(tmp_path):
    root, clock, calls, providers = setup_sources(tmp_path, [network_error() for _ in range(4)])
    result = collect(root, providers, clock, request(candidates=stock_codes(4)))
    assert result["state"] == "COMPLETE"  # Complete prices, not complete trading facts.
    assert sum(query == "history" for _, query, _ in calls) == 3
    assert sum(query == "prices" for _, query, _ in calls) == 4
    paused = [r for r in history_requests(root) if r["state"] == "PAUSED"]
    assert len(paused) == 1
    assert paused[0]["pause_reason"] == "SOURCE_COOLDOWN"
    assert paused[0]["attempts"] == 0
    assert paused[0]["next_retry_at"] == (clock() + timedelta(seconds=60)).isoformat()
    assert result["source_cooldowns"]["baostock"]["consecutive_network_failures"] == 3
    assert any(i["kind"] == "SOURCE_COOLDOWN" for i in result["issues"])
    assert not MarketDataService(root).execution_facts(stock_codes(4)[-1], "2026-10-27").ready


def test_expired_source_cooldown_probes_and_success_resets_failure_streak(tmp_path):
    class SlowBackup(FakeSource):
        def fetch(self, query, params):
            response = super().fetch(query, params)
            self.clock.advance(20)
            return response

    root, clock, calls, providers = setup_sources(
        tmp_path, [network_error() for _ in range(3)], SlowBackup)
    result = collect(root, providers, clock, request(candidates=stock_codes(6)))
    assert result["state"] == "COMPLETE"
    assert sum(query == "history" for _, query, _ in calls) == 4
    assert sum(query == "prices" for _, query, _ in calls) == 5
    assert sum(r["state"] == "PAUSED" for r in history_requests(root)) == 2
    assert result["source_cooldowns"] == {}
    last = [r for r in history_requests(root) if r["identity"]["params"]["code"] == stock_codes(6)[-1]][0]
    assert last["state"] == "OK"


def test_failed_probe_reopens_source_cooldown_without_waiting_for_three_more_failures(tmp_path):
    class SlowBackup(FakeSource):
        def fetch(self, query, params):
            response = super().fetch(query, params)
            self.clock.advance(20)
            return response

    root, clock, calls, providers = setup_sources(
        tmp_path, [network_error() for _ in range(4)], SlowBackup)
    result = collect(root, providers, clock, request(candidates=stock_codes(7)))
    assert result["state"] == "COMPLETE"
    assert sum(query == "history" for _, query, _ in calls) == 4
    assert sum(query == "prices" for _, query, _ in calls) == 7
    assert result["source_cooldowns"]["baostock"]["consecutive_network_failures"] == 4
    assert sum(r["state"] == "PAUSED" for r in history_requests(root)) == 3


@pytest.mark.parametrize("category", ["UNPUBLISHED", "FORMAT"])
def test_non_network_response_breaks_network_failure_streak(tmp_path, category):
    outcomes = [network_error(), network_error(), ProviderError("no accepted data", category=category),
                network_error(), network_error()]
    root, clock, calls, providers = setup_sources(tmp_path, outcomes)
    result = collect(root, providers, clock, request(candidates=stock_codes(5)))
    assert result["state"] == "COMPLETE"
    assert sum(query == "history" for _, query, _ in calls) == 5
    assert result["source_cooldowns"] == {}
    assert not any(r["state"] == "PAUSED" for r in history_requests(root))


def test_success_breaks_network_failure_streak(tmp_path):
    root, clock, calls, providers = setup_sources(
        tmp_path, [network_error(), network_error(), None, network_error(), network_error()])
    result = collect(root, providers, clock, request(candidates=stock_codes(5)))
    assert result["state"] == "COMPLETE"
    assert sum(query == "history" for _, query, _ in calls) == 5
    assert result["source_cooldowns"] == {}


def test_source_cooldown_does_not_shorten_server_retry_after_even_on_refresh(tmp_path):
    errors = [network_error() for _ in range(3)]
    for error in errors:
        error.retry_after_seconds = 120
    root, clock, calls, providers = setup_sources(tmp_path, errors)
    wanted = request(candidates=stock_codes(4))
    collect(root, providers, clock, wanted)
    clock.advance(60)
    result = collect(root, providers, clock, wanted, refresh=True)
    assert sum(query == "history" for _, query, _ in calls) == 4  # Only fourth can retry.
    assert sum(i["kind"] == "COOLDOWN" and i.get("origin") == "server"
               for i in result["issues"]) == 3
    assert sum(r["state"] == "FAILED" for r in history_requests(root)) == 3


def test_keyboard_interrupt_persists_current_request_and_job_then_same_command_resumes(tmp_path):
    root, clock, calls, providers = setup_sources(tmp_path, [KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        collect(root, providers, clock)
    repository = MarketRepository(root)
    failed = history_requests(root)[0]
    assert failed["state"] == "PAUSED"
    assert failed["pause_reason"] == "INTERRUPTED"
    assert failed["attempts"] == 1
    jobs = list(repository.states("jobs").values())
    assert len(jobs) == 1
    assert jobs[0]["state"] == "PAUSED"
    assert jobs[0]["pause_reason"] == "INTERRUPTED"
    assert not any(r["state"] == "RUNNING" for r in repository.states("requests").values())
    assert collect(root, providers, clock)["state"] == "COMPLETE"
    assert history_requests(root)[0]["state"] == "OK"
    assert history_requests(root)[0]["attempts"] == 2
    assert sum(query == "history" for _, query, _ in calls) == 2
    assert sum(query == "calendar" for _, query, _ in calls) == 1


def test_interrupt_between_requests_persists_job_and_releases_writer_lock(tmp_path, monkeypatch):
    root, clock, _, providers = setup_sources(tmp_path, [])

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(MarketUpdater, "_prices", interrupt)
        with pytest.raises(KeyboardInterrupt):
            collect(root, providers, clock)
    repository = MarketRepository(root)
    assert next(iter(repository.states("jobs").values()))["pause_reason"] == "INTERRUPTED"
    assert not any(r["state"] == "RUNNING" for r in repository.states("requests").values())
    assert collect(root, providers, clock)["state"] == "COMPLETE"


def test_next_writer_marks_stale_running_jobs_and_requests_paused_without_losing_retry(tmp_path):
    root, clock, _, providers = setup_sources(tmp_path, [])
    repository = MarketRepository(root)
    earlier = (clock() - timedelta(minutes=10)).isoformat()
    retry_at = (clock() + timedelta(minutes=5)).isoformat()
    stale = {"state": "RUNNING", "started_at": earlier, "attempts": 2,
             "server_retry_at": retry_at, "next_retry_at": retry_at,
             "identity": {"source": "baostock", "query": "history",
                          "params": {"code": "SH600099"}}}
    repository.save_state("jobs", "previous-job", {"state": "RUNNING", "started_at": earlier})
    repository.save_state("requests", "previous-request", stale)
    untouched = {**stale, "state": "FAILED"}
    repository.save_state("requests", "previous-failed-request", untouched)
    result = collect(root, providers, clock)
    assert result["state"] == "COMPLETE"
    old_job = repository.states("jobs")["previous-job"]
    old_request = repository.states("requests")["previous-request"]
    for old in (old_job, old_request):
        assert old["state"] == "PAUSED"
        assert old["pause_reason"] == "INTERRUPTED_PREVIOUS_RUN"
        assert old["started_at"] == earlier
        assert old["recovered_at"] == clock().isoformat()
    assert old_request["server_retry_at"] == old_request["next_retry_at"] == retry_at
    assert old_request["identity"] == stale["identity"]
    assert old_request["attempts"] == 2
    assert repository.states("requests")["previous-failed-request"] == untouched
    assert repository.states("jobs")[result["job_id"]]["state"] == "COMPLETE"
