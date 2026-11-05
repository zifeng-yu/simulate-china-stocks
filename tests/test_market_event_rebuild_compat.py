"""Ordinary cache consumption must preserve a classification repair's knowledge time."""

import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_market_integration import AS_OF as PREPARATION_DAY
from test_market_integration import CLOCK as PREPARATION_CLOCK
from test_market_integration import PreparationSource

from china_stock_sim.market import update
from china_stock_sim.market.contracts import DataRequest, PendingEventRange
from china_stock_sim.market.event_rebuild import rebuild_event_classification
from china_stock_sim.market.events import normalize_baostock_events, normalize_eastmoney_events
from china_stock_sim.market.preparation import Preparation
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.storage import digest, read_json

CODE, DAY = "SH600000", "2026-10-30"
FETCHED = "2026-11-01T15:00:00+08:00"
BEFORE = "2026-11-01T15:30:00+08:00"
REPAIRED = "2026-11-01T16:00:00+08:00"
CONSUMED = datetime.fromisoformat("2026-11-01T16:30:00+08:00")
PLAN = "现金分红占合并报表口径利润的30%"


def bao_row():
    return {"code": CODE, "dividPlanAnnounceDate": "2026-04-30", "dividPlanDate": "",
            "dividRegistDate": "", "dividOperateDate": "", "dividPayDate": "",
            "dividStockMarketDate": "", "dividCashPsBeforeTax": "", "dividStocksPs": "",
            "dividReserveToStockPs": "", "dividCashStock": PLAN}


def source_raw(source):
    if source == "baostock":
        row = bao_row()
    else:
        row = {"SECURITY_CODE": CODE[2:], "SECUCODE": "600000.SH", "REPORT_DATE": "2026-06-30",
               "PLAN_NOTICE_DATE": "2026-04-30", "NOTICE_DATE": "2026-04-30",
               "EQUITY_RECORD_DATE": None, "EX_DIVIDEND_DATE": None,
               "PRETAX_BONUS_RMB": None, "BONUS_RATIO": None, "IT_RATIO": None,
               "ASSIGN_PROGRESS": "预披露", "IMPL_PLAN_PROFILE": PLAN}
    raw = {"provider": "baostock" if source == "baostock" else "eastmoney_events",
           "adapter_version": "1", "fetched_at": FETCHED, "fields": list(row), "rows": [row]}
    if source == "eastmoney":
        raw["coverage"] = {"complete": True, "code": CODE,
                           "scope": "reported_cash_bonus_capital_distributions"}
    return raw


class OfflineSource:
    adapter_version = "1"

    def __init__(self, source, raw=None):
        self.provider_id = "baostock" if source == "baostock" else "eastmoney_events"
        self.raw, self.calls = raw, 0

    def fetch(self, query, params):
        assert self.raw is not None, "successful same-day source should have been reused"
        self.calls += 1
        return self.raw


@pytest.fixture
def root(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline compatibility regression attempted external access")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root = tmp_path / "market"
    (root / "configs").mkdir(parents=True)
    (root / "configs/preparation.toml").write_bytes(
        (Path(__file__).resolve().parents[1] / "configs/preparation.toml").read_bytes())
    return root


def seed(repository, source, *, legacy=True, changed_amount=False):
    raw = source_raw(source)
    reference = repository.save_raw(raw)
    normalize = normalize_baostock_events if source == "baostock" else normalize_eastmoney_events
    records = normalize(raw["rows"], CODE, FETCHED, raw_reference=reference)
    if legacy:
        records[0]["kind"] = "COMPLEX"
        if source == "eastmoney":
            records[0]["status"] = "UNKNOWN"
    if changed_amount:
        records[0]["cash_per_share"] = "0.3"
        records[0]["normalization"] = {"revision": "event_classification_20261101"}
    members = repository.put("event", records, raw)
    batch = {"code": CODE, "date": DAY, "members": members, "scope": "reported_distributions",
             "batch_key": "baostock:2026" if source == "baostock" else "eastmoney:all"}
    if source == "baostock":
        batch["year"] = 2026
    repository.put("event_batch", [batch], raw, key_fields=["code", "batch_key"])
    identity = {"source": source, "adapter": "1",
                "query": "dividends" if source == "baostock" else "events",
                "params": {"code": CODE, **({"year": 2026} if source == "baostock" else {})}}
    repository.save_state("requests", digest(identity), {"identity": identity, "state": "OK",
        "attempts": 1, "retry_failures": 0, "raw_reference": reference, "finished_at": FETCHED})
    return raw


def observations(repository):
    with repository.connection() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT * FROM observations WHERE kind IN ('event','event_batch') ORDER BY id")]


def run_cached(root, clock=CONSUMED, *, sources=None, end=DAY):
    sources = sources or {source: OfflineSource(source) for source in ("baostock", "eastmoney")}
    request = DataRequest(DAY, pending_event_ranges=(PendingEventRange(CODE, end, end),),
                          include_indices=False)
    return update.MarketUpdater(root, clock=lambda: clock, providers=sources).run(request)


def test_rebuild_then_both_cached_sources_preserves_old_cutoff_and_reuses_exact_records(root):
    repository = MarketRepository(root)
    for source in ("baostock", "eastmoney"):
        seed(repository, source)
    service = MarketDataService(root)
    old = service.events(CODE, knowledge_cutoff=BEFORE).as_dict()
    plan = rebuild_event_classification(root, codes=[CODE], as_of=DAY, clock=lambda: REPAIRED)
    rebuild_event_classification(root, codes=[CODE], as_of=DAY, apply=True,
                                 expected_plan=plan["plan_id"], clock=lambda: REPAIRED)
    repaired = service.events(CODE, knowledge_cutoff=REPAIRED).as_dict()
    rows = observations(repository)
    for clock in (CONSUMED, CONSUMED + timedelta(minutes=1)):
        result = run_cached(root, clock)
        assert result["state"] == "COMPLETE" and result["requests_this_run"] == 0
        assert service.events(CODE, knowledge_cutoff=BEFORE).as_dict() == old
        assert service.events(CODE, knowledge_cutoff=clock).as_dict() == repaired
        assert observations(repository) == rows


@pytest.mark.parametrize("source", ["baostock", "eastmoney"])
@pytest.mark.parametrize("changed_amount", [False, True])
def test_cached_new_interpretation_uses_now_and_compares_all_semantics(root, source, changed_amount):
    repository = MarketRepository(root)
    for name in ("baostock", "eastmoney"):
        seed(repository, name, legacy=name == source and not changed_amount,
             changed_amount=name == source and changed_amount)
    service = MarketDataService(root)
    old = service.events(CODE, knowledge_cutoff=BEFORE).as_dict()
    assert run_cached(root)["state"] == "COMPLETE"
    assert service.events(CODE, knowledge_cutoff=BEFORE).as_dict() == old
    revised = next(item for item in repository.select("event", code=CODE)
                   if item["provider"] == ("baostock" if source == "baostock" else "eastmoney_events"))
    assert revised["payload"]["kind"] == "DISTRIBUTION"
    assert revised["payload"]["status"] == "PROPOSED"
    assert revised["payload"]["cash_per_share"] is None
    assert revised["fetched_at"] == FETCHED
    assert datetime.fromisoformat(revised["available_at"]) == CONSUMED
    rows = observations(repository)
    assert run_cached(root, CONSUMED + timedelta(minutes=1))["state"] == "COMPLETE"
    assert observations(repository) == rows


def test_genuinely_new_sources_keep_their_acquisition_time(root):
    sources = {source: OfflineSource(source, source_raw(source))
               for source in ("baostock", "eastmoney")}
    assert run_cached(root, sources=sources)["state"] == "COMPLETE"
    repository = MarketRepository(root)
    assert all(source.calls == 1 for source in sources.values())
    assert all(datetime.fromisoformat(row["available_at"]) == datetime.fromisoformat(FETCHED)
               for kind in ("event", "event_batch") for row in repository.select(kind))


def test_cached_range_change_keeps_members_and_original_batch_scope(root):
    repository = MarketRepository(root)
    for source in ("baostock", "eastmoney"):
        seed(repository, source)
    assert run_cached(root)["state"] == "COMPLETE"
    original = repository.select("event_batch")
    members = repository.select("event")
    # Multiple explicit requests may share an injected clock. A date-only
    # batch change does not change an event interpretation at that instant.
    assert run_cached(root, end="2026-10-29")["state"] == "COMPLETE"
    assert repository.select("event") == members
    assert repository.by_ids([batch["id"] for batch in original]) == original
    assert {batch["payload"]["date"] for batch in repository.select("event_batch")} == {
        "2026-10-29"}
    assert all(batch["payload"]["date"] == DAY for batch in original)


@pytest.mark.parametrize("repair_first", [False, True])
def test_preparation_event_only_import_preserves_old_time_and_existing_repair(root, monkeypatch,
                                                                           repair_first):
    class Source(PreparationSource):
        def fetch(self, query, params):
            raw = super().fetch(query, params)
            if query == "dividends" and params == {"code": CODE, "year": 2024}:
                row = {**bao_row(), "dividPlanAnnounceDate": "2024-01-31"}
                raw.update(rows=[row], fields=list(row))
            return raw

    state = Preparation(root, PREPARATION_DAY, download=True, provider=Source(),
                        clock=lambda: PREPARATION_CLOCK).run()
    assert state["state"] == "PREPARATION_COMPLETE"
    entry = next(item for item in state["requests"].values() if item["query"] == "dividends"
                 and item["params"] == {"code": CODE, "year": 2024})
    raw = read_json(root / entry["raw_reference"])["response"]
    repository = MarketRepository(root)
    reference = repository.save_raw(raw)
    records = normalize_baostock_events(raw["rows"], CODE, raw["fetched_at"], raw_reference=reference)
    records[0]["kind"] = "COMPLEX"
    members = repository.put("event", records, raw)
    if repair_first:
        repository.put("event_batch", [{"code": CODE, "date": PREPARATION_DAY,
            "batch_key": "baostock:2024", "year": 2024, "members": members,
            "scope": "reported_distributions"}], raw, key_fields=["code", "batch_key"])
    service = MarketDataService(root)
    before = PREPARATION_CLOCK + timedelta(minutes=10)
    old = service.events(CODE, knowledge_cutoff=before).as_dict()
    if repair_first:
        def clock():
            return PREPARATION_CLOCK + timedelta(minutes=20)

        plan = rebuild_event_classification(root, codes=[CODE], as_of=PREPARATION_DAY, clock=clock)
        rebuild_event_classification(root, codes=[CODE], as_of=PREPARATION_DAY, apply=True,
                                     expected_plan=plan["plan_id"], clock=clock)
    prior_rows = observations(repository)
    now = PREPARATION_CLOCK + timedelta(minutes=30)
    monkeypatch.setattr(update, "timestamp", lambda: now.isoformat())
    update.import_preparation(root, as_of=PREPARATION_DAY)
    assert service.events(CODE, knowledge_cutoff=before).as_dict() == old
    assert service.events(CODE, knowledge_cutoff=now).data[0]["kind"] == "DISTRIBUTION"
    current = repository.select("event", code=CODE)[0]
    assert current["fetched_at"] == raw["fetched_at"] and current["raw_ref"] == reference
    if repair_first:
        assert observations(repository) == prior_rows
        assert "normalization" in current["payload"]
    else:
        assert datetime.fromisoformat(current["available_at"]) == now
    after = observations(repository)
    update.import_preparation(root, as_of=PREPARATION_DAY)
    assert observations(repository) == after
