"""Daily consumers need actual index comparisons and purpose-specific event facts."""

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from china_stock_sim.market.cli import execute, register_commands
from china_stock_sim.market.contracts import DataRequest
from china_stock_sim.market.events import normalize_baostock_events, normalize_eastmoney_events
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.update import MarketUpdater

DAY = "2026-07-16"
FETCHED = DAY + "T18:10:00+08:00"
INDEX = "SH000001"
CODE = "SH600000"


def source(rows, *, fetched=FETCHED, provider="fixture"):
    return {"provider": provider, "adapter_version": "test", "fetched_at": fetched,
            "fields": sorted(set().union(*(r.keys() for r in rows))) if rows else [],
            "rows": rows}


@pytest.fixture
def repository(tmp_path):
    return MarketRepository(tmp_path / "facts_only")


def seed_index(repository, *, previous=True, previous_fetched=FETCHED):
    dates = [{"date": day, "is_trading_day": int(day in {"2026-07-10", "2026-07-13"})}
             for day in ("2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13")]
    repository.put("calendar", dates, source(dates, fetched="2026-07-01T18:00:00+08:00"))
    current = [{"code": INDEX, "date": "2026-07-13", "close": "3012.00"}]
    ids = repository.put("index", current, source(current))
    if previous:
        prior = [{"code": INDEX, "date": "2026-07-10", "close": "3000.00"}]
        ids += repository.put("index", prior, source(prior, fetched=previous_fetched))
    return ids


def cash_event(*, complete=False, fetched=FETCHED, **changes):
    if complete:
        row = {"code": "sh.600000", "dividPlanAnnounceDate": "2026-03-31",
               "dividPlanDate": "2026-07-10", "dividRegistDate": "2026-07-15",
               "dividOperateDate": DAY, "dividPayDate": DAY, "dividStockMarketDate": "",
               "dividCashPsBeforeTax": "0.42", "dividStocksPs": "0",
               "dividReserveToStockPs": "0", "dividCashStock": "10派4.2元(含税)"}
        row.update(changes)
        return normalize_baostock_events([row], CODE, fetched)[0]
    row = {"SECURITY_CODE": "600000", "SECUCODE": "600000.SH", "REPORT_DATE": "2025-12-31",
           "PLAN_NOTICE_DATE": "2026-03-31", "NOTICE_DATE": "2026-07-10",
           "EQUITY_RECORD_DATE": "2026-07-15", "EX_DIVIDEND_DATE": DAY,
           "PRETAX_BONUS_RMB": "4.2", "BONUS_RATIO": "0", "IT_RATIO": "0",
           "IMPL_PLAN_PROFILE": "10派4.2元(含税)", "ASSIGN_PROGRESS": "实施分配"}
    row.update(changes)
    return normalize_eastmoney_events([row], CODE, fetched)[0]


def seed_event(repository, event):
    return repository.put("event", [event], source([event], fetched=event["fetched_at"],
                                                  provider=event["provider"]))


def seed_coverage(repository, *, start=DAY, end=DAY):
    rows = [{"code": CODE, "date": end, "purpose": "events", "start": start, "end": end,
             "complete": True, "scope": "reported_cash_bonus_capital_distributions"}]
    return repository.put("coverage", rows, source(rows, fetched=max(FETCHED, end + "T18:10:00+08:00")))


def test_index_quote_uses_previous_exchange_session_and_binds_both_prices(repository):
    ids = seed_index(repository)
    result = MarketDataService(repository.root).readiness(INDEX, "2026-07-13", "index")
    assert result.ready
    assert result.data["previous_session"] == "2026-07-10"
    assert result.data["previous_close"] == "3000.00"
    assert result.data["change"] == "12.00"
    assert result.data["change_percent"] == "0.4000"
    assert set(ids) <= set(result.observation_ids)


@pytest.mark.parametrize("previous", [False, True])
def test_index_missing_or_late_previous_close_keeps_quote_but_never_invents_zero(repository, previous):
    seed_index(repository, previous=previous, previous_fetched="2026-07-16T19:00:00+08:00")
    result = MarketDataService(repository.root).index_quote(INDEX, "2026-07-13",
                                                           knowledge_cutoff=FETCHED)
    assert result.state == "PARTIAL"
    assert result.data["close"] == "3012.00"
    assert result.data["previous_close"] is result.data["change"] is result.data["change_percent"] is None
    assert "MISSING_PREVIOUS_INDEX_CLOSE" in {i["kind"] for i in result.issues}


def test_index_holiday_is_not_a_ready_empty_quote(repository):
    seed_index(repository)
    result = MarketDataService(repository.root).index_quote(INDEX, "2026-07-12")
    assert not result.ready and result.issues[0]["kind"] == "NON_TRADING_SESSION"


def test_valuation_holiday_is_waiting_while_price_range_may_be_empty(repository):
    seed_index(repository)
    service = MarketDataService(repository.root)
    prices = service.prices(CODE, "2026-07-12", "2026-07-12")
    assert prices.ready and prices.data == []
    valuation = service.readiness(CODE, "2026-07-12", "valuation")
    assert valuation.state == "WAITING_DATA" and valuation.data == []
    assert valuation.issues == ({"kind": "NON_TRADING_SESSION", "date": "2026-07-12"},)


def test_raw_event_can_be_visible_while_payment_is_not_ready_for_settlement(repository):
    seed_event(repository, cash_event())
    seed_coverage(repository)
    service = MarketDataService(repository.root)
    assert service.events(CODE).ready
    result = service.readiness(CODE, DAY, "event_settlement")
    assert result.state == "WAITING_DATA"
    assert result.issues[0]["kind"] == "UNKNOWN_PAYMENT_DATE"
    assert result.data[0]["settlement_readiness"]["state"] == "WAITING_DATA"


def test_cross_source_dates_complete_one_event_without_double_cash(repository):
    seed_event(repository, cash_event())
    seed_event(repository, cash_event(complete=True))
    coverage_ids = seed_coverage(repository)
    result = MarketDataService(repository.root).event_settlement(CODE, DAY)
    assert result.ready and len(result.data) == 1
    assert result.data[0]["payment_date"] == DAY
    assert result.data[0]["cash_per_share"] == "0.42"
    assert set(coverage_ids) <= set(result.observation_ids)


def test_historical_unknown_payment_is_explicit_outstanding_item_not_everyday_block(repository):
    event = cash_event()
    seed_event(repository, event)
    seed_coverage(repository, end="2026-07-17")
    service = MarketDataService(repository.root)
    daily = service.event_settlement(CODE, "2026-07-17")
    assert daily.ready and daily.data == []
    outstanding = service.event_settlement(CODE, "2026-07-17", event_ids=[event["event_id"]])
    assert outstanding.state == "WAITING_DATA"
    assert any(i["kind"] == "UNKNOWN_PAYMENT_DATE" for i in outstanding.issues)


def test_explicit_outstanding_event_is_readable_by_merged_id_and_source_id(repository):
    event = cash_event(complete=True)
    seed_event(repository, event)
    service = MarketDataService(repository.root)
    canonical = service.events(CODE).data[0]["event_id"]
    for identity in (event["event_id"], canonical):
        result = service.event_settlement(CODE, "2026-07-20", event_ids=[identity])
        assert result.ready and len(result.data) == 1
    missing = service.event_settlement(CODE, DAY, event_ids=["missing-event"])
    assert missing.state == "WAITING_DATA"
    assert missing.issues == ({"kind": "EVENT_NOT_FOUND", "event_id": "missing-event"},)


def test_daily_no_events_needs_coverage_proof(repository):
    service = MarketDataService(repository.root)
    assert not service.event_settlement(CODE, DAY).ready
    seed_coverage(repository)
    result = service.event_settlement(CODE, DAY)
    assert result.ready and result.data == []


def test_undated_proposal_is_visible_and_waits_in_daily_settlement(repository):
    seed_event(repository, cash_event(EQUITY_RECORD_DATE=None, EX_DIVIDEND_DATE=None,
                                      ASSIGN_PROGRESS="董事会预案"))
    seed_coverage(repository)
    result = MarketDataService(repository.root).event_settlement(CODE, DAY)
    assert result.state == "WAITING_DATA" and len(result.data) == 1
    assert "EVENT_NOT_IMPLEMENTED" in {i["kind"] for i in result.issues}


def test_event_payment_revision_after_cutoff_cannot_make_old_settlement_ready(repository):
    seed_event(repository, cash_event())
    seed_coverage(repository)
    seed_event(repository, cash_event(complete=True, fetched="2026-07-16T19:00:00+08:00"))
    service = MarketDataService(repository.root)
    assert not service.event_settlement(CODE, DAY, knowledge_cutoff=FETCHED).ready
    assert service.event_settlement(CODE, DAY, knowledge_cutoff="2026-07-16T19:00:00+08:00").ready


def test_event_conflict_and_damaged_evidence_cannot_be_used_for_settlement(repository):
    ids = seed_event(repository, cash_event(complete=True))
    seed_event(repository, cash_event(PRETAX_BONUS_RMB="4.3", IMPL_PLAN_PROFILE="10派4.3元(含税)"))
    service = MarketDataService(repository.root)
    assert service.event_settlement(CODE, DAY).state == "CONFLICT"
    raw = repository.by_ids(ids)[0]["raw_ref"]
    (repository.root / raw).write_text("damaged", encoding="utf-8")
    assert service.event_settlement(CODE, DAY).state == "CORRUPT"


def test_cli_exposes_outstanding_event_readiness_without_network_or_models(repository):
    event = cash_event(complete=True)
    seed_event(repository, event)
    parser = argparse.ArgumentParser()
    register_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["data-query", "--code", CODE, "--as-of", DAY,
                              "--purpose", "event_settlement", "--event-ids", event["event_id"]])
    result, code = execute(args, repository.root)
    assert code == 0 and result["state"] == "READY"


class IndexFixtureSource:
    """Only fixture calendar data defines sessions; no network/weekday fallback in production."""

    adapter_version = "test"

    def __init__(self, provider_id, calls, omitted_previous=False):
        self.provider_id, self.calls, self.omitted_previous = provider_id, calls, omitted_previous

    def fetch(self, query, params):
        self.calls.append((self.provider_id, query, params))
        rows = []
        day, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        while day <= end:
            if query == "calendar":
                rows.append({"calendar_date": day.isoformat(), "is_trading_day": int(day.weekday() < 5)})
            elif query == "prices" and day.weekday() < 5:
                if not (self.omitted_previous and day.isoformat() == "2026-07-10"):
                    price = "3000" if day.isoformat() == "2026-07-10" else "3012"
                    rows.append({"code": params["code"], "date": day.isoformat(),
                                 **dict.fromkeys(("open", "high", "low", "close"), price)})
            elif query != "prices":
                raise AssertionError(f"unexpected fixture query {query}")
            day += timedelta(days=1)
        return source(rows, provider=self.provider_id, fetched="2026-07-13T20:00:00+08:00")


@pytest.mark.parametrize("omit_previous", [False, True])
def test_daily_index_sync_requests_two_exchange_sessions_even_with_one_day_window(tmp_path, omit_previous):
    root = tmp_path / "standalone_data"
    (root / "configs").mkdir(parents=True)
    original = Path(__file__).resolve().parents[1] / "configs/preparation.toml"
    (root / "configs/preparation.toml").write_bytes(original.read_bytes())
    calls = []
    providers = {name: IndexFixtureSource(name, calls, omit_previous) for name in ("baostock", "tencent")}
    updater = MarketUpdater(root, providers=providers,
                            clock=lambda: datetime.fromisoformat("2026-07-13T20:00:00+08:00"))
    result = updater.run(DataRequest("2026-07-13", history_sessions=1, include_events=False))
    assert result["state"] == ("PARTIAL" if omit_previous else "COMPLETE")
    assert result["indices_complete"] is (not omit_previous)
    requests = [params for _, query, params in calls if query == "prices"]
    assert len(requests) == 3
    assert all(p["start_date"] == "2026-07-10" and p["end_date"] == "2026-07-13" for p in requests)
    quote = MarketDataService(root).index_quote(INDEX, "2026-07-13")
    if omit_previous:
        assert quote.state == "PARTIAL" and quote.data["change_percent"] is None
    else:
        assert quote.ready and quote.data["change_percent"] == "0.4000"


def seed_execution_days(repository):
    """Complete synthetic facts let tests distinguish scope from other data gaps."""
    days = [{"date": day, "is_trading_day": "1"}
            for day in ("2026-07-15", DAY, "2026-07-17")]
    repository.put("calendar", days, source(days))
    bars = [{"code": CODE, "date": day["date"], "open": "10", "high": "10",
             "low": "10", "close": "10", "preclose": "10", "volume": "10000",
             "amount": "100000", "tradestatus": "1", "isST": "0", "adjustflag": "3"}
            for day in days]
    repository.put("bar", bars, source(bars))
    securities = [{"code": CODE, "date": DAY, "listed": "1999-11-10", "board": "沪市主板",
                   "source_status": 1, "eligibility_known": True}]
    repository.put("security", securities, source(securities))
    seed_coverage(repository, start="2026-07-15", end="2026-07-17")
    return {"rule_id": "fixture-mainboard", "effective_from": "2026-01-01",
            "limit_rate": "0.10", "price_tick": "0.01"}


def test_historical_conflict_remains_visible_without_blocking_unrelated_daily_consumers(repository):
    event = cash_event(complete=True, dividCashPsBeforeTax="0.43")
    seed_event(repository, event)
    rule = seed_execution_days(repository)
    service = MarketDataService(repository.root)
    assert service.events(CODE).state == "CONFLICT"
    daily = service.event_settlement(CODE, "2026-07-17")
    assert daily.ready and daily.data == []
    executed = service.execution_facts(CODE, "2026-07-17", rule_context=rule)
    assert executed.ready and executed.data["limit_up"] == "11.00"
    # Neither source nor merged outstanding identifiers may escape the conflict.
    canonical = service.events(CODE).data[0]["event_id"]
    for identity in (event["event_id"], canonical):
        outstanding = service.event_settlement(CODE, "2026-07-17", event_ids=[identity])
        assert outstanding.state == "CONFLICT"
        assert "CONFLICT_CASH_PER_SHARE" in {issue["kind"] for issue in outstanding.issues}


def test_target_day_conflict_still_blocks_settlement_and_execution(repository):
    seed_event(repository, cash_event(complete=True, dividCashPsBeforeTax="0.43"))
    rule = seed_execution_days(repository)
    service = MarketDataService(repository.root)
    settlement = service.event_settlement(CODE, DAY)
    assert settlement.state == "CONFLICT" and not settlement.ready
    execution = service.execution_facts(CODE, DAY, rule_context=rule)
    assert execution.state == "WAITING_DATA"
    assert "CONFLICT_CASH_PER_SHARE" in execution.issues
    assert execution.data["limit_up"] is execution.data["limit_down"] is None


@pytest.mark.parametrize("progress", ["实施分配", "董事会预案", "未知进度"])
def test_undated_conflict_cannot_be_assumed_outside_execution_or_settlement_scope(repository, progress):
    event = cash_event(EQUITY_RECORD_DATE=None, EX_DIVIDEND_DATE=None,
                      PRETAX_BONUS_RMB="4.3", ASSIGN_PROGRESS=progress)
    seed_event(repository, event)
    rule = seed_execution_days(repository)
    service = MarketDataService(repository.root)
    assert service.event_settlement(CODE, DAY).state == "CONFLICT"
    execution = service.execution_facts(CODE, DAY, rule_context=rule)
    assert execution.state == "WAITING_DATA"
    assert {"UNDATED_ACTION", "CONFLICT_CASH_PER_SHARE"} <= set(execution.issues)


def test_undated_complex_event_remains_blocked_after_scope_filtering(repository):
    event = cash_event(EQUITY_RECORD_DATE=None, EX_DIVIDEND_DATE=None,
                      IMPL_PLAN_PROFILE="差异化分红", ASSIGN_PROGRESS="董事会预案")
    seed_event(repository, event)
    rule = seed_execution_days(repository)
    service = MarketDataService(repository.root)
    assert not service.event_settlement(CODE, DAY).ready
    execution = service.execution_facts(CODE, DAY, rule_context=rule)
    assert execution.state == "WAITING_DATA" and "KNOWN_COMPLEX_ACTION" in execution.issues


def test_corrupt_event_evidence_still_blocks_unrelated_execution(repository):
    ids = seed_event(repository, cash_event(complete=True, dividCashPsBeforeTax="0.43"))
    rule = seed_execution_days(repository)
    reference = repository.by_ids(ids)[0]["raw_ref"]
    (repository.root / reference).write_text("damaged", encoding="utf-8")
    service = MarketDataService(repository.root)
    assert service.event_settlement(CODE, "2026-07-17").state == "CORRUPT"
    assert service.execution_facts(CODE, "2026-07-17", rule_context=rule).state == "CORRUPT"
