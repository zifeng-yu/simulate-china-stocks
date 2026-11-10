"""Cached collection saves raw work while preserving facts and normalization."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_market_pending_event_ranges import CODE, EXITED, run, wanted
from test_market_pending_event_ranges import environment as environment

from china_stock_sim.market import events, raw_validation
from china_stock_sim.market import repository as repository_module
from china_stock_sim.market.contracts import PendingEventRange
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.update import MarketUpdater


def facts(repository):
    with repository.connection() as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM observations ORDER BY id")]


def projection(result):
    return {key: value for key, value in result.items() if key not in {"job_id", "pid"}}


def test_cached_update_reduces_raw_reads_with_identical_facts_and_unique_normalization(
        environment, monkeypatch):
    root, clock, calls, _ = environment
    request = replace(wanted(PendingEventRange(CODE, "2026-10-26", "2026-10-27"),
                             candidates=(CODE, EXITED)), history_sessions=121)
    assert run(environment, request)["state"] == "COMPLETE"
    repository = MarketRepository(root)
    initial = facts(repository)
    initial_raw = {path.name: path.read_bytes() for path in repository.objects.glob("*.json")}
    initial_calls = list(calls)
    counters = Counter()
    for module in (repository_module, raw_validation):
        original = module.read_json

        def read(path, _original=original):
            if path.is_relative_to(repository.objects):
                counters["raw_reads"] += 1
            return _original(path)

        monkeypatch.setattr(module, "read_json", read)
    for name in ("normalize_baostock_events", "normalize_eastmoney_events"):
        original = getattr(events, name)

        def normalize(*args, _original=original, _name=name, **kwargs):
            counters[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(events, name, normalize)
    optimized = run(environment, request)
    optimized_counts = counters.copy()
    assert optimized["requests_this_run"] == 0
    assert optimized_counts["normalize_baostock_events"] == 2
    assert optimized_counts["normalize_eastmoney_events"] == 2
    counters.clear()

    @contextmanager
    def unscoped(self, shared=None):
        yield None

    # The exact pre-scope path remains available when no update scope is active.
    monkeypatch.setattr(MarketRepository, "validation_scope", unscoped)
    ordinary = run(environment, request)
    assert projection(optimized) == projection(ordinary)
    assert optimized_counts["raw_reads"] < counters["raw_reads"] / 4
    assert counters["normalize_baostock_events"] == optimized_counts["normalize_baostock_events"]
    assert counters["normalize_eastmoney_events"] == optimized_counts["normalize_eastmoney_events"]
    assert calls == initial_calls
    assert facts(repository) == initial
    assert {path.name: path.read_bytes() for path in repository.objects.glob("*.json")} == initial_raw


def test_interrupted_update_clears_both_scopes_and_releases_writer(environment, monkeypatch):
    root, clock, calls, sources = environment
    updater = MarketUpdater(root, providers=sources, clock=clock)
    seen = []
    original = sources["baostock"].fetch

    def interrupt(query, params):
        seen.append(updater.repository._raw_validation)
        assert updater.service.repository._raw_validation is seen[-1]
        if query == "dividends":
            raise KeyboardInterrupt()
        return original(query, params)

    monkeypatch.setattr(sources["baostock"], "fetch", interrupt)
    with pytest.raises(KeyboardInterrupt):
        updater.run(wanted(PendingEventRange(CODE, "2026-10-26", "2026-10-27")))
    assert seen and all(scope.closed and not scope.entries for scope in seen)
    assert updater.repository._raw_validation is updater.service.repository._raw_validation is None
    with MarketRepository(root).writer():
        pass
    saved = list(updater.repository.states("requests").values())
    assert saved[0]["state"] == "PAUSED" and saved[0]["pause_reason"] == "INTERRUPTED"
    monkeypatch.setattr(sources["baostock"], "fetch", original)
    resumed = updater.run(wanted(PendingEventRange(CODE, "2026-10-26", "2026-10-27")))
    assert resumed["state"] == "COMPLETE"


def test_competing_writer_does_not_create_a_scope_or_request(environment):
    root, clock, calls, sources = environment
    updater = MarketUpdater(root, providers=sources, clock=clock)
    with MarketRepository(root).writer(), pytest.raises(RuntimeError, match="已有数据更新"):
        updater.run(wanted(candidates=(CODE,)))
    assert calls == []
    assert updater.repository._raw_validation is updater.service.repository._raw_validation is None
