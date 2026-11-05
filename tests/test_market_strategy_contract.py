"""Strategy consumers use the public reader for causal, configurable price windows."""

import argparse
import socket
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from inspect import Parameter, signature
from types import SimpleNamespace

import pytest

from china_stock_sim.market.base import MarketDataReader
from china_stock_sim.market.cli import execute, register_commands
from china_stock_sim.market.contracts import DataRequest, DataResult
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

CODE = "SH600000"


def put(repository, kind, rows, fetched, *, provider="fixture"):
    return repository.put(kind, rows, {
        "provider": provider, "adapter_version": "test", "fetched_at": fetched,
        "fields": sorted(set().union(*(row.keys() for row in rows))), "rows": rows,
    }, key_fields=["code", "purpose", "start", "end"] if kind == "coverage" else None)


def seed_history(tmp_path, *, count=150, omit_bar=None, dividend=False, coverage_size=None):
    repository = MarketRepository(tmp_path / "standalone_market")
    sessions, calendar = [], []
    day = date(2024, 1, 1)
    # This is an explicit fixture calendar, including an artificial weekday closure.
    while len(sessions) < count:
        trading = day.weekday() < 5 and day.isoformat() != "2024-01-10"
        calendar.append({"date": day.isoformat(), "is_trading_day": int(trading)})
        if trading:
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    end = sessions[-1]
    cutoff = end + "T18:12:00+08:00"
    calendar_ids = put(repository, "calendar", calendar, "2023-12-31T18:00:00+08:00")
    ex_index = max(0, count - 10)
    bars = []
    for index, session in enumerate(sessions):
        if index == omit_bar:
            continue
        close = "9.50" if dividend and index >= ex_index else "10.00"
        bars.append({"code": CODE, "date": session, "open": close, "high": close,
                     "low": close, "close": close, "preclose": close, "volume": 10000,
                     "amount": str(Decimal(close) * 10000), "tradestatus": 1,
                     "isST": 0, "adjustflag": 3})
    bar_ids = put(repository, "bar", bars, cutoff)
    result = SimpleNamespace(repository=repository, reader=MarketDataService(repository.root),
                             sessions=sessions, end=end, cutoff=cutoff,
                             calendar_ids=calendar_ids, bar_ids=bar_ids,
                             ex_date=sessions[ex_index])
    add_coverage(result, size=coverage_size or count)
    return result


def add_coverage(facts, *, size, fetched=None):
    fetched = fetched or facts.cutoff
    rows = [{"code": CODE, "date": facts.end, "purpose": "events",
             "start": facts.sessions[-size], "end": facts.end, "complete": True,
             "scope": "reported_cash_bonus_capital_distributions"}]
    facts.coverage_ids = put(facts.repository, "coverage", rows, fetched)
    return facts.coverage_ids


def add_dividend(facts, *, fetched):
    ex_index = facts.sessions.index(facts.ex_date)
    raw = {"code": "sh.600000", "dividPlanAnnounceDate": facts.sessions[ex_index - 5],
           "dividPlanDate": facts.sessions[ex_index - 2],
           "dividRegistDate": facts.sessions[ex_index - 1],
           "dividOperateDate": facts.ex_date, "dividPayDate": facts.ex_date,
           "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.50",
           "dividStocksPs": "0", "dividReserveToStockPs": "0",
           "dividCashStock": "10派5元(含税)"}
    events = normalize_baostock_events([raw], CODE, fetched)
    return put(facts.repository, "event", events, fetched, provider="baostock")


def consume_prices(reader: MarketDataReader, facts, size: int) -> DataResult:
    """A consumer knows only the public Protocol, never repository layout or a provider."""
    return reader.signal_prices(CODE, facts.end, size=size, knowledge_cutoff=facts.cutoff)


def bytes_at(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def block_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A read-only strategy input query attempted network/subprocess/collection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(MarketDataService, "update", forbidden)


def test_public_reader_custom_window_preserves_default_purposes_and_is_read_only(tmp_path, monkeypatch):
    facts = seed_history(tmp_path)
    method = MarketDataReader.__dict__["signal_prices"]
    params = signature(method).parameters
    assert params["size"].default == 121
    assert params["size"].kind == Parameter.KEYWORD_ONLY
    assert params["knowledge_cutoff"].kind == Parameter.KEYWORD_ONLY
    before = bytes_at(facts.repository.root)
    block_external_work(monkeypatch)

    custom = consume_prices(facts.reader, facts, 42)
    default = facts.reader.signal_prices(CODE, facts.end, knowledge_cutoff=facts.cutoff)
    llm = facts.reader.readiness(CODE, facts.end, "llm", knowledge_cutoff=facts.cutoff)
    rules = facts.reader.readiness(CODE, facts.end, "rules", knowledge_cutoff=facts.cutoff)
    for result, size in [(custom, 42), (default, 121), (llm, 21), (rules, 121)]:
        assert isinstance(result, DataResult) and result.ready
        assert [row["date"] for row in result.data] == facts.sessions[-size:]
        assert all(Decimal(row["raw_close"]) == Decimal(row["adjusted_close"]) == 10
                   for row in result.data)
        assert set(facts.coverage_ids) <= set(result.observation_ids)
    assert default == rules
    assert bytes_at(facts.repository.root) == before
    assert not (facts.repository.root / "configs").exists()
    assert not list(facts.repository.root.rglob("ledger.sqlite3"))


@pytest.mark.parametrize("size", [1, 500])
def test_public_custom_window_reads_requested_sessions_at_collection_boundaries(tmp_path, size):
    facts = seed_history(tmp_path, count=size)
    result = consume_prices(facts.reader, facts, size)
    assert result.ready and len(result.data) == size
    assert [row["date"] for row in result.data] == facts.sessions
    assert "2024-01-10" not in {row["date"] for row in result.data}


@pytest.mark.parametrize("size", [0, -1, True, 2.0, "21"])
def test_public_custom_window_rejects_nonpositive_or_noninteger_sizes_without_writes(tmp_path, size):
    root = tmp_path / "missing_market"
    reader: MarketDataReader = MarketDataService(root)
    with pytest.raises(ValueError, match="正整数"):
        reader.signal_prices(CODE, "2024-03-04", size=size,
                             knowledge_cutoff="2024-03-04T18:12:00+08:00")
    assert not root.exists()


def test_public_custom_window_on_missing_cache_does_not_create_directories(tmp_path, monkeypatch):
    root = tmp_path / "missing_market"
    block_external_work(monkeypatch)
    reader: MarketDataReader = MarketDataService(root)
    result = reader.signal_prices(CODE, "2024-03-04", size=42,
                                  knowledge_cutoff="2024-03-04T18:12:00+08:00")
    assert result.state == "MISSING" and result.data == []
    assert not root.exists()


def test_custom_window_never_bridges_missing_calendar_or_bar(tmp_path):
    facts = seed_history(tmp_path, omit_bar=149 - 30)
    missing_bar = consume_prices(facts.reader, facts, 42)
    assert missing_bar.state == "PARTIAL"
    assert any(issue["kind"] == "MISSING_BAR" for issue in missing_bar.issues)
    assert all("adjusted_close" not in row for row in missing_bar.data)
    missing_calendar = consume_prices(facts.reader, facts, 151)
    assert missing_calendar.state == "WAITING_DATA"
    assert any(issue["kind"] == "MISSING_CALENDAR" for issue in missing_calendar.issues)
    # Shorter purpose only needs its own complete final 21 sessions.
    assert facts.reader.readiness(CODE, facts.end, "llm", knowledge_cutoff=facts.cutoff).ready


def test_extended_window_requires_its_own_fresh_coverage_not_only_short_window(tmp_path):
    facts = seed_history(tmp_path, coverage_size=21)
    assert facts.reader.readiness(CODE, facts.end, "llm", knowledge_cutoff=facts.cutoff).ready
    short_proof = consume_prices(facts.reader, facts, 150)
    assert short_proof.state == "WAITING_DATA" and short_proof.data == []
    assert {"kind": "EVENT_COVERAGE_UNKNOWN"} in short_proof.issues

    stale_time = facts.end + "T10:00:00+08:00"
    add_coverage(facts, size=150, fetched=stale_time)
    assert not consume_prices(facts.reader, facts, 150).ready
    details = facts.reader.event_coverage(CODE, facts.sessions[0], facts.end,
                                         knowledge_cutoff=facts.cutoff)
    assert details.state == "WAITING_DATA"
    assert any(issue["kind"] == "EVENT_COVERAGE_STALE" for issue in details.issues)

    fresh_time = facts.end + "T19:00:00+08:00"
    fresh_ids = add_coverage(facts, size=150, fetched=fresh_time)
    assert not consume_prices(facts.reader, facts, 150).ready
    refreshed = facts.reader.signal_prices(CODE, facts.end, size=150, knowledge_cutoff=fresh_time)
    assert refreshed.ready and len(refreshed.data) == 150
    assert set(fresh_ids) <= set(refreshed.observation_ids)


def test_late_dividend_cannot_repair_old_strategy_input_and_raw_prices_stay_separate(tmp_path):
    facts = seed_history(tmp_path, dividend=True)
    late = facts.end + "T19:00:00+08:00"
    event_ids = add_dividend(facts, fetched=late)
    frozen_knowledge = consume_prices(facts.reader, facts, 42)
    assert frozen_knowledge.state == "WAITING_DATA" and frozen_knowledge.data == []
    assert {"kind": "UNEXPLAINED_REFERENCE_CHANGE"} in frozen_knowledge.issues
    later = facts.reader.signal_prices(CODE, facts.end, size=42, knowledge_cutoff=late)
    assert later.ready and len(later.data) == 42
    assert Decimal(later.data[-1]["raw_close"]) == Decimal("9.50")
    assert Decimal(later.data[-1]["adjusted_close"]) == 10
    assert set(event_ids) <= set(later.observation_ids)
    ex_row = next(row for row in later.data if row["date"] == facts.ex_date)
    assert ex_row["event_ids"]
    assert consume_prices(facts.reader, facts, 42) == frozen_knowledge


def test_corrupt_coverage_is_never_ready_and_public_detail_preserves_cause(tmp_path):
    facts = seed_history(tmp_path)
    raw = facts.repository.by_ids(facts.coverage_ids)[0]["raw_ref"]
    (facts.repository.root / raw).write_text("damaged coverage bytes")
    result = consume_prices(facts.reader, facts, 42)
    assert result.state == "WAITING_DATA" and result.data == []
    details = facts.reader.event_coverage(CODE, facts.sessions[-42], facts.end,
                                         knowledge_cutoff=facts.cutoff)
    assert details.state == "CORRUPT"
    assert details.issues[0]["kind"] == "CORRUPT"


def test_explicit_source_price_conflict_blocks_custom_strategy_input(tmp_path):
    facts = seed_history(tmp_path)
    selected = facts.repository.by_ids(facts.bar_ids[-1:])[0]["payload"]
    conflicting = {**selected, "conflicts": ["KNOWN_SOURCE_PRICE_CONFLICT"]}
    put(facts.repository, "bar", [conflicting], facts.end + "T19:00:00+08:00")
    result = facts.reader.signal_prices(CODE, facts.end, size=42,
                                        knowledge_cutoff=facts.end + "T19:00:00+08:00")
    assert result.state == "CONFLICT" and not result.ready
    assert any(issue["kind"] == "DATA_CONFLICT" for issue in result.issues)


def test_sync_cli_keeps_custom_history_in_every_request_without_loading_accounts(tmp_path, monkeypatch):
    observed = []

    def update(self, request, **kwargs):
        observed.append((request, kwargs))
        return {"state": "COMPLETE"}

    monkeypatch.setattr(MarketDataService, "update", update)
    parser = argparse.ArgumentParser()
    register_commands(parser.add_subparsers(dest="command", required=True))
    root = tmp_path / "standalone_market"
    for as_of in ["2024-07-29", "2024-07-30"]:
        args = parser.parse_args(["data-sync", "--as-of", as_of, "--codes", CODE,
                                  "--history-sessions", "150", "--no-indices"])
        result, exit_code = execute(args, root)
        assert exit_code == 0 and result["state"] == "COMPLETE"
    assert [request.as_of for request, _ in observed] == ["2024-07-29", "2024-07-30"]
    assert all(isinstance(request, DataRequest) and request.history_sessions == 150
               and request.candidates == (CODE,) and request.include_events
               and kwargs["download"] is False for request, kwargs in observed)
    assert not root.exists()
