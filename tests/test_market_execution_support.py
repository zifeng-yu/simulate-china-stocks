"""DX01-DX03: executable price facts, precise boundaries and complete evidence."""

from decimal import Decimal

import pytest

from china_stock_sim.market.base import MarketDataReader
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.signals import execution_facts

CODE = "SH600000"
DAY = "2026-07-20"
PREVIOUS = "2026-07-17"
FETCHED = DAY + "T18:10:00+08:00"
RULE = {"rule_id": "isolated-mainboard", "effective_from": "2026-07-06",
        "price_tick": "0.01", "limit_rate": "0.10"}
SECURITY = {"code": CODE, "date": DAY, "listed": "1999-11-10", "board": "沪市主板",
            "source_status": 1, "eligibility_known": True}


def bar(day=DAY, price="10", reference="10", **changes):
    return {"code": CODE, "date": day, "open": price, "high": price,
            "low": price, "close": price, "preclose": reference, "volume": 100,
            "amount": str(Decimal(price) * 100), "tradestatus": 1, "isST": 0,
            "adjustflag": 3, **changes}


def distribution(cash="0.005", **changes):
    row = {"code": "sh.600000", "dividPlanAnnounceDate": "2026-07-01",
           "dividPlanDate": "2026-07-10", "dividRegistDate": PREVIOUS,
           "dividOperateDate": DAY, "dividPayDate": DAY, "dividStockMarketDate": "",
           "dividCashPsBeforeTax": cash, "dividStocksPs": "0",
           "dividReserveToStockPs": "0", "dividCashStock": ""}
    return {**normalize_baostock_events([row], CODE, FETCHED)[0], **changes}


def facts(prices=None, *, security=None, events=(), rule=None, cutoff=FETCHED):
    return execution_facts(prices or bar(), security or SECURITY, list(events), cutoff,
                           events_complete=True, rule_context=RULE if rule is None else rule)


def put(repository, kind, rows, fetched=FETCHED):
    return repository.put(kind, rows, {"provider": "isolated", "adapter_version": "test",
                                      "fetched_at": fetched, "rows": rows})


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))
    repository = MarketRepository(tmp_path / "execution-facts-only")
    identities = {}
    for day, trading in [(PREVIOUS, 1), ("2026-07-18", 0), ("2026-07-19", 0), (DAY, 1)]:
        identities[day] = put(repository, "calendar", [{"date": day, "is_trading_day": trading}])
    put(repository, "bar", [bar()])
    put(repository, "security", [SECURITY])
    put(repository, "coverage", [{"code": CODE, "date": DAY, "purpose": "events",
                                 "start": PREVIOUS, "end": DAY, "complete": True,
                                 "scope": "published_cash_bonus_capital_distributions",
                                 "complex_action_absence_proved": False}])
    return repository, MarketDataService(repository.root), identities


@pytest.mark.parametrize("location", ["context", "security", "both"])
def test_special_status_unknown_cannot_use_a_normal_rate(location):
    rule, security = dict(RULE), dict(SECURITY)
    if location in {"context", "both"}:
        rule["special_status_unknown"] = True
    if location in {"security", "both"}:
        security["special_status_unknown"] = True
    result = facts(rule=rule, security=security)
    assert result["state"] == "WAITING_DATA"
    assert "SPECIAL_STATUS_UNKNOWN" in result["issues"]
    assert result["limit_rate"] is result["limit_up"] is result["limit_down"] is None


def test_context_cannot_override_an_unknown_security_status():
    result = facts(rule={**RULE, "special_status_unknown": False},
                   security={**SECURITY, "special_status_unknown": True})
    assert "SPECIAL_STATUS_UNKNOWN" in result["issues"]


@pytest.mark.parametrize("reference,upper,lower", [
    ("0.04", "0.05", "0.03"), ("0.01", "0.02", "0.01"),
    ("10", "11.00", "9.00"),
])
def test_confirmed_tick_reference_has_minimum_movement_and_positive_floor(reference, upper, lower):
    result = facts(bar(price=reference, reference=reference))
    assert result["state"] == "READY", result
    assert (result["limit_up"], result["limit_down"]) == (upper, lower)
    assert result["reference_price_source"] == result["reference_price_selected"] == reference
    assert result["reference_price_exact"] is None  # No ex-date formula was consumed.
    assert result["limit_rate"] == "0.10"


def test_formula_source_and_selected_reference_are_not_overwritten_by_tick_rounding():
    result = facts(bar(price="9.99", reference="9.99", previous_close="10"),
                   events=[distribution("0.006")])
    assert result["state"] == "READY", result
    assert result["reference_price_exact"] == "9.994"
    assert result["reference_price_source"] == "9.99"
    assert result["reference_price_selected"] == result["reference_price"] == "9.994"
    assert result["limit_up"] == "10.99" and result["limit_down"] == "8.99"


def test_precision_tolerance_does_not_resolve_different_limit_boundaries():
    result = facts(bar(reference="10.00", previous_close="10"), events=[distribution()])
    assert result["state"] == "WAITING_DATA"
    assert result["reference_price_exact"] == "9.995"
    assert result["reference_price_source"] == "10.00"
    assert result["reference_price"] is result["reference_price_selected"] is None
    assert result["limit_up"] is result["limit_down"] is None
    assert "PRICE_REFERENCE_PRECISION_PENDING" in result["issues"]
    assert "EX_REFERENCE_SOURCE_CONFLICT" not in result["issues"]


def test_matching_high_precision_reference_rounds_only_final_limits():
    result = facts(bar(reference="9.995", previous_close="10"), events=[distribution()])
    assert result["state"] == "READY", result
    assert result["reference_price"] == result["reference_price_source"] == "9.995"
    assert result["limit_up"] == "10.99"  # Pre-rounding R to 10 would incorrectly give 11.
    assert result["limit_down"] == "9.00"


def test_non_tick_low_price_does_not_invent_rounding_order():
    result = facts(bar(price="0.04", reference="0.044"))
    assert result["state"] == "WAITING_DATA"
    assert result["reference_price_source"] == "0.044"
    assert result["reference_price_selected"] is None
    assert result["limit_up"] is result["limit_down"] is None
    assert "PRICE_REFERENCE_PRECISION_PENDING" in result["issues"]


def test_source_disagreement_and_invalid_price_never_become_ready():
    conflict = facts(bar(reference="9.98", previous_close="10"), events=[distribution()])
    assert conflict["state"] == "WAITING_DATA"
    assert "EX_REFERENCE_SOURCE_CONFLICT" in conflict["issues"]
    invalid = facts(bar(reference="NaN"))
    assert invalid["state"] == "WAITING_DATA"
    assert invalid["reference_price_selected"] is None
    outside = facts(bar(price="11.01"))
    assert outside["state"] == "WAITING_DATA"
    assert "PRICE_OUTSIDE_EFFECTIVE_LIMITS" in outside["issues"]


def test_no_price_limit_is_known_not_a_zero_limit():
    result = facts(rule={**RULE, "no_price_limit": True})
    assert result["state"] == "READY" and result["no_price_limit"] is True
    assert result["limit_rate"] is result["limit_up"] is result["limit_down"] is None


@pytest.mark.parametrize("cause", ["previous_price", "closed_day"])
def test_execution_propagates_corrupt_previous_evidence(cache, cause):
    repository, service, identities = cache
    previous_ids = put(repository, "bar", [bar(PREVIOUS)])
    bad_ids = previous_ids if cause == "previous_price" else identities["2026-07-19"]
    path = repository.root / repository.by_ids(bad_ids)[0]["raw_ref"]
    path.write_text("corrupt isolated original", encoding="utf-8")
    assert service.prices(CODE, DAY, DAY, knowledge_cutoff=FETCHED).ready
    result = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert result.state == "CORRUPT"
    assert result.data is None and result.issues


def test_execution_propagates_previous_price_conflict(cache):
    repository, service, _ = cache
    put(repository, "bar", [bar(PREVIOUS, conflicts=[{"kind": "SOURCE_DISAGREEMENT"}])])
    result = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert result.state == "CONFLICT" and result.data is None
    assert result.issues[0]["kind"] == "DATA_CONFLICT"


def test_execution_preserves_calendar_conflict_contract(cache, monkeypatch):
    _, service, identities = cache
    issue = {"kind": "CALENDAR_SOURCE_CONFLICT", "date": PREVIOUS}
    monkeypatch.setattr(service, "calendar_operation", lambda *a, **k:
                        DataResult("CONFLICT", None, (issue,), tuple(identities[PREVIOUS])))
    result = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert result.state == "CONFLICT" and result.issues == (issue,)
    assert set(identities[PREVIOUS]) <= set(result.observation_ids)


def test_missing_previous_close_has_explicit_source_only_basis(cache):
    _, service, _ = cache
    result = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert result.ready
    assert result.data["reference_basis"] == "source_unadjusted_preclose_only"
    assert result.data["reference_price_exact"] is None


def test_missing_previous_close_still_blocks_ex_date(cache):
    repository, service, _ = cache
    put(repository, "event", [distribution()])
    result = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert result.state == "WAITING_DATA"
    assert "PREVIOUS_CLOSE_REQUIRED_FOR_EX_DATE" in result.issues
    assert result.data["limit_up"] is None


def test_execution_snapshot_binds_weekend_and_precise_query_result(cache):
    repository, service, identities = cache
    put(repository, "bar", [bar(PREVIOUS)])
    current = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert current.ready
    for day_ids in identities.values():
        assert set(day_ids) <= set(current.observation_ids)
    snapshot = service.freeze([CODE], DAY, knowledge_cutoff=FETCHED,
                              purposes=("execution",), rule_context=RULE)
    frozen = service.read_snapshot(snapshot["snapshot_id"])
    assert frozen["body"]["content"]["results"][CODE]["execution"] == current.as_dict()
    manifest = service.evidence_manifest([snapshot["snapshot_id"]])
    assert manifest
    path = repository.root / repository.by_ids(identities["2026-07-18"])[0]["raw_ref"]
    path.unlink()
    with pytest.raises(CorruptData):
        service.read_snapshot(snapshot["snapshot_id"])


def test_precise_reference_fields_round_trip_public_snapshot(cache):
    repository, service, _ = cache
    put(repository, "bar", [bar(PREVIOUS)])
    put(repository, "bar", [bar(reference="9.995")])
    put(repository, "event", [distribution()])
    result = service.readiness(CODE, DAY, "execution", knowledge_cutoff=FETCHED, rule_context=RULE)
    assert result.ready and result.data["limit_up"] == "10.99"
    snapshot = service.freeze([CODE], DAY, knowledge_cutoff=FETCHED,
                              purposes=("execution",), rule_context=RULE)
    assert snapshot["body"]["content"]["results"][CODE]["execution"] == result.as_dict()


@pytest.mark.parametrize("ex_date", [DAY, None])
def test_target_or_undated_complex_action_still_blocks_prices(ex_date):
    event = distribution(kind="COMPLEX", ex_date=ex_date)
    result = facts(events=[event])
    assert result["state"] == "WAITING_DATA"
    assert "KNOWN_COMPLEX_ACTION" in result["issues"]
    assert result["limit_up"] is result["limit_down"] is None
    assert result["known_complex_event_ids"] == [event["event_id"]]


def test_future_complex_fact_is_visible_without_invalidating_target_prices():
    event = distribution(kind="COMPLEX", ex_date="2026-07-24")
    result = facts(events=[event])
    assert result["state"] == "READY", result
    assert result["known_complex_event_ids"] == [event["event_id"]]
    assert result["event_ids"] == [] and result["limit_up"] == "11.00"
    assert "side" not in result and "buy_eligible" not in result


def test_late_future_announcement_cannot_rewrite_frozen_plan_knowledge(cache):
    repository, service, _ = cache
    old = service.freeze([CODE], DAY, knowledge_cutoff=FETCHED, purposes=("events",))
    late = "2026-07-22T18:10:00+08:00"
    event = distribution(kind="COMPLEX", ex_date="2026-07-27", fetched_at=late,
                         available_at=late, published_date="2026-07-22")
    put(repository, "event", [event], fetched=late)
    assert service.events(CODE, knowledge_cutoff=FETCHED).data == []
    assert service.read_snapshot(old["snapshot_id"]) == old
    late_events = service.events(CODE, knowledge_cutoff=late)
    assert late_events.ready and late_events.data[0]["kind"] == "COMPLEX"
    late_prices = service.readiness(CODE, DAY, "execution", knowledge_cutoff=late, rule_context=RULE)
    assert late_prices.ready  # It is the plan consumer that uses frozen Kp for buy exclusion.
    assert late_prices.data["known_complex_event_ids"]


def test_public_reader_declares_existing_pool_query(cache):
    repository, service, _ = cache
    assert "pool" in MarketDataReader.__dict__
    put(repository, "pool", [{"date": PREVIOUS, "effective_from": DAY,
                              "selected": [{"code": CODE}]}])
    assert service.pool(PREVIOUS, knowledge_cutoff=FETCHED).state == "WAITING_DATA"
    assert service.pool(DAY, knowledge_cutoff=FETCHED).data["selected"] == [{"code": CODE}]
