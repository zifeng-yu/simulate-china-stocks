"""Public command contracts used by a future ledger, without a ledger or network."""

import argparse
import json
import socket

import pytest
from test_market_consumer_gaps import CODE, DAY, cash_event, seed_event

from china_stock_sim.market.cli import execute, pending_event_ranges, register_commands
from china_stock_sim.market.contracts import DataRequest, PendingEventRange
from china_stock_sim.market.event_operations import EVENT_OPERATIONS
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService


def arguments(*argv):
    parser = argparse.ArgumentParser()
    register_commands(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(argv)


@pytest.fixture
def facts(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("账本消费者数据接口不应触发网络")

    monkeypatch.setattr(socket, "create_connection", no_network)
    repository = MarketRepository(tmp_path / "facts_without_account")
    event = cash_event(complete=True)
    seed_event(repository, event)
    return repository, event


@pytest.mark.parametrize("operation", EVENT_OPERATIONS)
def test_cli_event_operations_and_freeze_roundtrip(facts, operation):
    repository, event = facts
    args = arguments("data-query", "--code", CODE, "--as-of", DAY,
                     "--purpose", "event_settlement", "--event-operation", operation,
                     "--event-ids", event["event_id"])
    result, code = execute(args, repository.root)
    assert code == 0 and result["state"] == "READY"
    frozen, code = execute(arguments(
        "data-freeze", "--codes", CODE, "--as-of", DAY,
        "--purposes", "events,event_settlement", "--event-operation", operation,
        "--event-ids", event["event_id"]), repository.root)
    assert code == 0
    saved = MarketDataService(repository.root).read_snapshot(frozen["snapshot_id"])
    assert saved == frozen
    assert saved["body"]["content"]["results"][CODE]["event_settlement"]["data"] == result["data"]
    assert not (repository.root / "data/state/ledger.sqlite3").exists()


def test_cli_future_payment_ready_does_not_mean_payment_due(facts):
    repository, event = facts
    result, code = execute(arguments(
        "data-query", "--code", CODE, "--as-of", "2026-07-15",
        "--purpose", "event_settlement", "--event-operation", "cash_payment",
        "--event-ids", event["event_id"]), repository.root)
    assert code == 0
    assert result["data"][0]["operation_date"] == DAY
    assert result["data"][0]["operation_date"] > "2026-07-15"


@pytest.mark.parametrize("argv", [
    ("data-query", "--code", CODE, "--as-of", DAY, "--event-operation", "cash_payment"),
    ("data-freeze", "--codes", CODE, "--as-of", DAY,
     "--event-operation", "eligibility"),
    ("data-freeze", "--codes", CODE, "--as-of", DAY, "--event-ids", "one"),
])
def test_cli_rejects_event_options_outside_event_purpose(tmp_path, argv):
    with pytest.raises(ValueError):
        execute(arguments(*argv), tmp_path / "no_writes")
    assert not (tmp_path / "no_writes").exists()


def test_cli_pending_ranges_are_explicit_and_do_not_auto_add_pool(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps([{"code": "sh.600000", "start": "2020-01-01",
                                "end": "2020-12-31", "event_ids": ["old-id"]}]))
    observed = []

    def update(self, request, **kwargs):
        observed.append(request)
        return {"state": "COMPLETE"}

    def no_implicit_pool(*args, **kwargs):
        raise AssertionError("定向历史权益补查不得隐式引入整个候选池")

    monkeypatch.setattr(MarketDataService, "update", update)
    monkeypatch.setattr(MarketDataService, "pool", no_implicit_pool)
    result, code = execute(arguments("data-sync", "--as-of", DAY,
                                    "--pending-event-ranges", str(path), "--no-indices"), tmp_path)
    assert code == 0 and result["state"] == "COMPLETE"
    assert observed[0].candidates == ()
    assert observed[0].pending_event_ranges == (
        PendingEventRange(CODE, "2020-01-01", "2020-12-31", ("old-id",)),)
    assert observed[0].history_sessions == 121
    assert not (tmp_path / "data/state/ledger.sqlite3").exists()


@pytest.mark.parametrize("value", [
    {}, ["not-object"], [{"code": CODE, "start": "2020-01-01"}],
    [{"code": CODE, "start": "2020-01-02", "end": "2020-01-01"}],
    [{"code": CODE, "start": "2020-01-01", "end": "2020-01-02", "typo": True}],
])
def test_cli_rejects_invalid_pending_range_file(tmp_path, value):
    path = tmp_path / "ranges.json"
    path.write_text(json.dumps(value))
    with pytest.raises((ValueError, TypeError)):
        pending_event_ranges(path)


def test_cli_rejects_oversized_pending_range_file(tmp_path):
    path = tmp_path / "large.json"
    path.write_text(" " * 1_000_001)
    with pytest.raises(ValueError, match="大小限制"):
        pending_event_ranges(path)


def test_old_positional_request_remains_compatible():
    request = DataRequest(DAY, (CODE,), (), (), (), 121, True, False)
    assert request.pending_event_ranges == ()
    assert request.ordered_codes == (CODE,)
