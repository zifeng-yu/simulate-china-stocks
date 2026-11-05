"""Ledger action facts are independently usable, immutable and read-only."""

import socket

import pytest

from china_stock_sim.market.event_operations import EVENT_OPERATIONS, operation_readiness
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService

CODE = "SH600000"
DAY = "2026-07-16"
FETCHED = "2026-07-16T18:10:00+08:00"


def source(rows):
    return {"provider": "fixture", "adapter_version": "test", "fetched_at": FETCHED,
            "fields": sorted(set().union(*(row.keys() for row in rows))), "rows": rows}


def event(**changes):
    row = {"code": "sh.600000", "dividPlanAnnounceDate": "2026-03-31",
           "dividPlanDate": "2026-07-10", "dividRegistDate": "2026-07-15",
           "dividOperateDate": DAY, "dividPayDate": "2026-07-17",
           "dividStockMarketDate": "2026-07-20", "dividCashPsBeforeTax": "0.4",
           "dividStocksPs": "0.1", "dividReserveToStockPs": "0.2",
           "dividCashStock": "10送1转2派4元(含税)"}
    value = normalize_baostock_events([row], CODE, FETCHED)[0]
    value.update(changes)
    return value


@pytest.fixture
def repository(tmp_path):
    return MarketRepository(tmp_path / "facts_only")


def seed(repository, value, *, coverage=True, coverage_end=DAY):
    ids = repository.put("event", [value], source([value]))
    if coverage:
        rows = [{"code": CODE, "date": coverage_end, "purpose": "events", "start": "2026-01-01",
                 "end": coverage_end, "complete": True}]
        ids += repository.put("coverage", rows, {
            **source(rows), "fetched_at": max(FETCHED, coverage_end + "T18:10:00+08:00")})
    return ids


@pytest.mark.parametrize(("operation", "date"), [
    ("eligibility", "2026-07-15"), ("cash_accrual", DAY), ("share_accrual", DAY),
    ("cash_payment", "2026-07-17"), ("share_delivery", "2026-07-20"),
])
def test_each_action_returns_its_date_without_claiming_due(repository, operation, date):
    value = event()
    seed(repository, value)
    service = MarketDataService(repository.root)
    result = service.readiness(CODE, DAY, "event_settlement", operation=operation,
                               knowledge_cutoff=FETCHED)
    assert result.ready
    row = result.data[0]
    assert row["operation"] == operation
    assert row["operation_date"] == date
    assert row["operation_applicable"] is True
    assert row["settlement_readiness"]["operation_date"] == date
    # Future payment/listing is READY facts, never a hidden instruction to credit today.
    assert "is_due" not in row


def test_legacy_whole_settlement_still_waits_but_cash_actions_work(repository):
    value = event(share_listing_date=None)
    seed(repository, value)
    service = MarketDataService(repository.root)
    legacy = service.event_settlement(CODE, DAY, knowledge_cutoff=FETCHED)
    explicit = service.event_settlement(CODE, DAY, operation="settlement",
                                         knowledge_cutoff=FETCHED)
    assert legacy.as_dict() == explicit.as_dict()
    assert legacy.state == "WAITING_DATA"
    assert "UNKNOWN_SHARE_LISTING_DATE" in {i["kind"] for i in legacy.issues}
    for operation in ("eligibility", "cash_accrual", "cash_payment", "share_accrual"):
        assert service.event_settlement(CODE, DAY, operation=operation).ready
    assert not service.event_settlement(CODE, DAY, operation="share_delivery").ready


def test_unknown_payment_does_not_block_share_actions(repository):
    seed(repository, event(payment_date=None))
    service = MarketDataService(repository.root)
    assert service.event_settlement(CODE, DAY, operation="share_delivery").ready
    assert service.event_settlement(CODE, DAY, operation="cash_accrual").ready
    assert not service.event_settlement(CODE, DAY, operation="cash_payment").ready


def test_registration_only_needs_registration_not_ex_amount_or_payment(repository):
    value = event(ex_date=None, payment_date=None, share_listing_date=None,
                  cash_per_share=None, bonus_per_share=None, capital_per_share=None)
    seed(repository, value)
    service = MarketDataService(repository.root)
    result = service.event_settlement(CODE, DAY, operation="eligibility")
    assert result.ready and result.data[0]["operation_date"] == "2026-07-15"
    for operation in ("cash_accrual", "share_accrual", "cash_payment", "share_delivery"):
        assert not service.event_settlement(CODE, DAY, operation=operation).ready


@pytest.mark.parametrize("operation", ["cash_accrual", "cash_payment"])
def test_known_zero_cash_is_not_a_payment_and_unknown_is_not_zero(repository, operation):
    value = event(cash_per_share="0", payment_date=None)
    seed(repository, value)
    service = MarketDataService(repository.root)
    result = service.event_settlement(CODE, DAY, operation=operation)
    assert result.ready and result.data[0]["operation_applicable"] is False
    assert result.data[0]["operation_date"] is None
    unknown = operation_readiness(event(cash_per_share=None), FETCHED, operation=operation)
    assert unknown["state"] == "WAITING_DATA" and unknown["applicable"] is None
    assert "UNKNOWN_CASH_PER_SHARE" in unknown["issues"]


@pytest.mark.parametrize("operation", ["share_accrual", "share_delivery"])
def test_known_zero_shares_does_not_require_listing_date(repository, operation):
    seed(repository, event(bonus_per_share="0", capital_per_share="0", share_listing_date=None))
    result = MarketDataService(repository.root).event_settlement(CODE, DAY, operation=operation)
    assert result.ready and result.data[0]["operation_applicable"] is False
    unknown = operation_readiness(event(bonus_per_share="0", capital_per_share=None),
                                  FETCHED, operation=operation)
    assert unknown["state"] == "WAITING_DATA" and unknown["applicable"] is None


@pytest.mark.parametrize("operation", EVENT_OPERATIONS[1:])
@pytest.mark.parametrize("changes,issue", [
    ({"status": "PROPOSED"}, "EVENT_NOT_IMPLEMENTED"),
    ({"status": "CANCELLED"}, "EVENT_NOT_IMPLEMENTED"),
    ({"kind": "COMPLEX"}, "COMPLEX_ACTION"),
    ({"available_at": "2026-07-16T19:00:00+08:00"}, "EVENT_NOT_YET_AVAILABLE"),
    ({"conflicts": ["CONFLICT_CASH_PER_SHARE"]}, "CONFLICT_CASH_PER_SHARE"),
])
def test_common_safety_rules_apply_to_every_action(operation, changes, issue):
    result = operation_readiness(event(**changes), FETCHED, operation=operation)
    assert result["state"] == "WAITING_DATA" and issue in result["issues"]


def test_registration_unknown_does_not_falsely_become_eligible(repository):
    seed(repository, event(record_date=None))
    result = MarketDataService(repository.root).event_settlement(CODE, DAY, operation="eligibility")
    assert result.state == "WAITING_DATA"
    assert "UNKNOWN_RECORD_DATE" in {issue["kind"] for issue in result.issues}


def test_daily_selection_retains_scope_and_explicit_old_event_can_be_queried(repository):
    value = event()
    seed(repository, value, coverage_end="2026-07-21")
    service = MarketDataService(repository.root)
    daily = service.event_settlement(CODE, "2026-07-21", operation="cash_payment")
    assert daily.ready and daily.data == []
    old = service.event_settlement(CODE, "2026-07-21", event_ids=[value["event_id"]],
                                    operation="cash_payment")
    assert old.ready and old.data[0]["operation_date"] == "2026-07-17"
    missing = service.event_settlement(CODE, DAY, event_ids=["missing"], operation="eligibility")
    assert missing.state == "WAITING_DATA"
    assert missing.issues == ({"kind": "EVENT_NOT_FOUND", "event_id": "missing"},)


def test_cutoff_does_not_load_later_event_facts(repository):
    value = event()
    seed(repository, value)
    result = MarketDataService(repository.root).event_settlement(
        CODE, DAY, event_ids=[value["event_id"]], operation="cash_payment",
        knowledge_cutoff="2026-07-16T18:00:00+08:00")
    assert not result.ready and result.data == []


def test_daily_coverage_corruption_is_explicit_and_ids_do_not_need_unrelated_coverage(repository):
    value = event()
    ids = seed(repository, value)
    coverage_ref = repository.by_ids(ids[1:])[0]["raw_ref"]
    (repository.root / coverage_ref).write_text("corrupt", encoding="utf-8")
    service = MarketDataService(repository.root)
    assert service.event_settlement(CODE, DAY, operation="cash_payment").state == "CORRUPT"
    # The explicitly identified implemented event remains independently provable.
    assert service.event_settlement(CODE, DAY, operation="cash_payment",
                                    event_ids=[value["event_id"]]).ready


@pytest.mark.parametrize("operation", EVENT_OPERATIONS[1:])
def test_related_economic_conflict_keeps_every_operation_blocked(repository, operation):
    seed(repository, event(conflicts=["CONFLICT_CASH_PER_SHARE"]))
    result = MarketDataService(repository.root).event_settlement(CODE, DAY, operation=operation)
    assert result.state == "CONFLICT"
    assert "CONFLICT_CASH_PER_SHARE" in {i["kind"] for i in result.issues}


@pytest.mark.parametrize("field,operation", [
    ("record_date", "eligibility"), ("ex_date", "cash_accrual"),
    ("ex_date", "share_accrual"), ("payment_date", "cash_payment"),
    ("share_listing_date", "share_delivery"),
])
def test_required_dates_cannot_be_invalid(field, operation):
    result = operation_readiness(event(**{field: "2026-02-30"}), FETCHED,
                                  operation=operation)
    assert result["state"] == "WAITING_DATA"
    assert f"INVALID_{field.upper()}" in result["issues"]


def test_no_distribution_is_explicitly_inapplicable_in_eligibility():
    result = operation_readiness(event(kind="NO_DISTRIBUTION", record_date=None,
                                      cash_per_share="0", bonus_per_share="0",
                                      capital_per_share="0"), FETCHED, operation="eligibility")
    assert result["state"] == "READY" and result["applicable"] is False
    assert result["operation_date"] is None


def test_freeze_action_and_explicit_ids_replay_same_snapshot_and_integrity(repository):
    value = event(share_listing_date=None)
    ids = seed(repository, value)
    service = MarketDataService(repository.root)
    snapshot = service.freeze([CODE], DAY, knowledge_cutoff=FETCHED,
                              purposes=("events", "event_settlement"), operation="cash_payment",
                              event_ids=[value["event_id"]])
    body = snapshot["body"]
    assert body["content"]["event_query"] == {
        "operation": "cash_payment", "event_ids": [value["event_id"]]}
    assert body["content"]["results"][CODE]["event_settlement"]["state"] == "READY"
    assert set(ids) <= set(body["observation_ids"])
    assert service.read_snapshot(snapshot["snapshot_id"]) == snapshot
    raw = repository.by_ids(ids[:1])[0]["raw_ref"]
    (repository.root / raw).write_text("corrupt", encoding="utf-8")
    assert service.event_settlement(CODE, DAY, operation="cash_payment").state == "CORRUPT"
    with pytest.raises(CorruptData):
        service.read_snapshot(snapshot["snapshot_id"])


@pytest.mark.parametrize("operation", ["cash_payment", "settlement"])
def test_freeze_rejects_ambiguous_multi_security_id_filter(repository, operation):
    service = MarketDataService(repository.root)
    with pytest.raises(ValueError, match="一个证券"):
        service.freeze([CODE, "SZ000001"], DAY, purposes=("event_settlement",),
                       event_ids=["event-id"], operation=operation)
    assert not repository.root.exists()


def test_action_queries_neither_create_files_nor_use_network(repository, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("read-only action queries attempted networking")

    monkeypatch.setattr(socket, "create_connection", unexpected)
    service = MarketDataService(repository.root)
    for operation in EVENT_OPERATIONS:
        assert not service.event_settlement(CODE, DAY, operation=operation).ready
    assert not repository.root.exists()
    seed(repository, event())
    before = {p.relative_to(repository.root): p.read_bytes()
              for p in repository.root.rglob("*") if p.is_file()}
    for operation in EVENT_OPERATIONS:
        assert service.event_settlement(CODE, DAY, operation=operation).ready
    assert before == {p.relative_to(repository.root): p.read_bytes()
                      for p in repository.root.rglob("*") if p.is_file()}


def test_invalid_action_or_scope_is_not_silently_ignored(repository):
    service = MarketDataService(repository.root)
    with pytest.raises(ValueError, match="未知权益动作"):
        service.event_settlement(CODE, DAY, operation="account_posting")
    with pytest.raises(ValueError, match="仅适用"):
        service.readiness(CODE, DAY, "valuation", operation="cash_payment")
    with pytest.raises(ValueError, match="仅适用"):
        service.freeze([CODE], DAY, operation="cash_payment")
    with pytest.raises(ValueError, match="不能是单个字符串"):
        service.event_settlement(CODE, DAY, event_ids="event-id")
    with pytest.raises(ValueError, match="非空字符串"):
        service.event_settlement(CODE, DAY, event_ids=[None])


@pytest.mark.parametrize("value", ["NaN", "-0.1", "bad", True])
def test_invalid_amount_cannot_be_treated_as_zero_or_ready(value):
    result = operation_readiness(event(cash_per_share=value), FETCHED, operation="cash_payment")
    assert result["state"] == "WAITING_DATA"
    assert result["applicable"] is None
    assert "INVALID_CASH_PER_SHARE" in result["issues"]


def test_partial_results_keep_ready_event_without_suppressing_other_missing_facts(repository):
    first = event()
    second = event(record_date="2026-06-15", ex_date="2026-06-16", payment_date=None,
                   proposal_date="2026-02-01", event_id="other-source-event")
    seed(repository, first)
    seed(repository, second)
    result = MarketDataService(repository.root).event_settlement(
        CODE, DAY, operation="cash_payment", event_ids=[first["event_id"], second["event_id"]])
    assert result.state == "PARTIAL" and len(result.data) == 2
    assert sum(e["settlement_readiness"]["state"] == "READY" for e in result.data) == 1
