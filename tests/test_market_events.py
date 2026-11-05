"""Event units, knowledge cutoffs, cross-source identity and causal price inputs."""

import io
import json
from decimal import Decimal
from email.message import Message
from urllib.error import HTTPError

import pytest

from china_stock_sim.market.events import (
    event_readiness,
    merge_events,
    normalize_baostock_events,
    normalize_eastmoney_events,
)
from china_stock_sim.market.providers.baostock import ProviderError
from china_stock_sim.market.providers.eastmoney import EastmoneyEventProvider
from china_stock_sim.market.quality import DataQualityError
from china_stock_sim.market.signals import adjusted_series, execution_facts

FETCHED = "2026-07-16T18:10:00+08:00"


def bao(**changes):
    row = {
        "code": "sh.600000", "dividPlanAnnounceDate": "2026-03-31",
        "dividPlanDate": "2026-07-10", "dividRegistDate": "2026-07-15",
        "dividOperateDate": "2026-07-16", "dividPayDate": "2026-07-16",
        "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.42",
        "dividStocksPs": "0.000000", "dividReserveToStockPs": "",
        "dividCashStock": "10派4.2元（含税，扣税后3.78或4.2元）",
    }
    row.update(changes)
    return row


def em(**changes):
    row = {
        "SECURITY_CODE": "600000", "SECUCODE": "600000.SH", "REPORT_DATE": "2025-12-31",
        "PLAN_NOTICE_DATE": "2026-03-31 00:00:00", "NOTICE_DATE": "2026-07-10 00:00:00",
        "EQUITY_RECORD_DATE": "2026-07-15", "EX_DIVIDEND_DATE": "2026-07-16",
        "PRETAX_BONUS_RMB": 4.2, "BONUS_RATIO": None, "IT_RATIO": None,
        "IMPL_PLAN_PROFILE": "10派4.20元(含税,扣税后3.78元)", "ASSIGN_PROGRESS": "实施分配",
    }
    row.update(changes)
    return row


def event(**changes):
    return normalize_baostock_events([bao(**changes)], "SH600000", FETCHED)[0]


def bar(session, close="10", preclose="10", **changes):
    row = {"code": "SH600000", "date": session, "open": close, "high": close,
           "low": close, "close": close, "preclose": preclose, "volume": "100",
           "amount": str(Decimal(close) * 100), "tradestatus": "1", "isST": "0", "adjustflag": "3",
           "fetched_at": FETCHED}
    row.update(changes)
    return row


def test_baostock_realistic_cash_blanks_are_derived_only_from_complete_plan():
    result = event()
    assert result["cash_per_share"] == "0.42"
    assert result["capital_per_share"] == "0"
    assert result["inferred_fields"] == ["capital_per_share"]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "READY"


def test_stock_distribution_keeps_distinct_ex_payment_and_listing_dates():
    result = event(dividCashStock="10转1派5.15元", dividCashPsBeforeTax="0.515",
                   dividReserveToStockPs="0.1", dividStockMarketDate="2026-07-17")
    assert result["capital_per_share"] == "0.1"
    assert result["share_listing_date"] == "2026-07-17"
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "READY"


def test_eastmoney_ratios_are_per_ten_and_payment_date_remains_unknown():
    result = normalize_eastmoney_events([em()], "SH600000", FETCHED)[0]
    assert result["cash_per_share"] == "0.42"
    assert result["bonus_per_share"] == "0"
    assert event_readiness(result, FETCHED)["state"] == "READY"
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"
    assert result["payment_date"] is None


def test_eastmoney_stock_ratio_and_complete_text_confirm_unit():
    result = normalize_eastmoney_events([em(IT_RATIO=1, PRETAX_BONUS_RMB=5.15,
                                            IMPL_PLAN_PROFILE="10转1.00派5.15元(含税)")],
                                        "SH600000", FETCHED)[0]
    assert result["capital_per_share"] == "0.1"
    assert result["cash_per_share"] == "0.515"


@pytest.mark.parametrize("source", ["baostock", "eastmoney"])
@pytest.mark.parametrize("plan, kind", [
    ("现金分红占合并报表净利润的比例不高于三分之一", "DISTRIBUTION"),
    ("归属于合并报表口径母公司普通股股东净利润", "DISTRIBUTION"),
    ("公司合并方案待定", "COMPLEX"),
    ("吸收合并方案待定", "COMPLEX"),
    ("换股合并方案待定", "COMPLEX"),
    ("本次吸收合并报表已披露", "COMPLEX"),
    ("本次换股合并报表已披露", "COMPLEX"),
    ("按合并报表口径披露；另有吸收合并事项", "COMPLEX"),
    ("按合并报表口径披露；另有配股事项", "COMPLEX"),
    ("按合并报表口径披露；另有缩股事项", "COMPLEX"),
    ("按合并报表口径披露；另有重组事项", "COMPLEX"),
    ("按合并报表口径披露；本次差异化分红", "COMPLEX"),
])
def test_accounting_phrase_exemption_preserves_actual_complex_actions(source, plan, kind):
    reference = "fixture/unchanged-source.json"
    if source == "baostock":
        result = normalize_baostock_events(
            [bao(dividCashStock=plan)], "SH600000", FETCHED, raw_reference=reference)[0]
    else:
        result = normalize_eastmoney_events(
            [em(IMPL_PLAN_PROFILE=plan)], "SH600000", FETCHED, raw_reference=reference)[0]
    assert result["kind"] == kind
    assert result["source_plan"] == plan and result["raw_reference"] == reference
    if kind == "COMPLEX":
        signal = adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [result],
                                 FETCHED, events_complete=True)
        assert signal["state"] == "WAITING_DATA" and "COMPLEX_ACTION" in signal["issues"]


@pytest.mark.parametrize("progress, status", [
    ("股东大会决议通过", "PROPOSED"),
    ("预披露", "PROPOSED"),
    ("股东大会否决", "CANCELLED"),
    ("取消实施分配", "CANCELLED"),
    ("实施方案终止", "CANCELLED"),
    ("股东大会决议通过后终止", "CANCELLED"),
    ("实施分配", "IMPLEMENTED"),
    ("实施方案", "IMPLEMENTED"),
    ("董事会决议通过", "PROPOSED"),
    ("未经股东大会决议通过", "UNKNOWN"),
    ("预披露待确认", "UNKNOWN"),
    ("股东大会否决待确认", "UNKNOWN"),
    ("其他未知进度", "UNKNOWN"),
])
def test_eastmoney_progress_mapping_is_exact_and_changes_no_other_facts(progress, status):
    reference = "fixture/original-progress.json"
    result = normalize_eastmoney_events([em(ASSIGN_PROGRESS=progress)], "SH600000", FETCHED,
                                       raw_reference=reference)[0]
    baseline = normalize_eastmoney_events([em(ASSIGN_PROGRESS="未知")], "SH600000", FETCHED,
                                         raw_reference=reference)[0]
    assert result["status"] == status
    # Existing dates, amounts, tax facts and evidence remain identical. In
    # particular, populated ex-dates cannot promote an approval to implementation.
    assert {key: value for key, value in result.items() if key != "status"} == {
        key: value for key, value in baseline.items() if key != "status"}
    if status != "IMPLEMENTED":
        assert "EVENT_NOT_IMPLEMENTED" in event_readiness(
            result, FETCHED, purpose="settlement")["issues"]


def test_accounting_predisclosure_allows_signals_without_inventing_settlement_facts():
    plan = ("2026年度中期现金分红金额占半年度归属于合并报表口径母公司普通股股东"
            "净利润的比例不高于三分之一")
    result = normalize_eastmoney_events([em(
        ASSIGN_PROGRESS="预披露", IMPL_PLAN_PROFILE=plan, PRETAX_BONUS_RMB=None,
        BONUS_RATIO=None, IT_RATIO=None, EQUITY_RECORD_DATE=None, EX_DIVIDEND_DATE=None,
    )], "SH600000", FETCHED, raw_reference="fixture/predisclosure.json")[0]
    assert result["kind"] == "DISTRIBUTION" and result["status"] == "PROPOSED"
    assert result["source_plan"] == plan and result["inferred_fields"] == []
    assert all(result[field] is None for field in (
        "cash_per_share", "bonus_per_share", "capital_per_share", "record_date",
        "ex_date", "payment_date", "share_listing_date"))
    assert all(fact["state"] == "UNKNOWN" for fact in result["tax_facts"].values())
    signal = adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [result], FETCHED,
                             events_complete=True)
    assert signal["state"] == "READY" and all(not row["event_ids"] for row in signal["rows"])
    settlement = event_readiness(result, FETCHED, purpose="settlement")
    assert settlement["state"] == "WAITING_DATA"
    assert {"EVENT_NOT_IMPLEMENTED", "UNKNOWN_CASH_PER_SHARE", "UNKNOWN_EX_DATE",
            "UNKNOWN_PAYMENT_DATE", "UNKNOWN_RECORD_DATE"} <= set(settlement["issues"])


def test_shareholder_approval_without_ex_date_does_not_become_a_settleable_distribution():
    result = normalize_eastmoney_events([em(
        ASSIGN_PROGRESS="股东大会决议通过", EQUITY_RECORD_DATE=None, EX_DIVIDEND_DATE=None,
    )], "SH600000", FETCHED)[0]
    signal = adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [result], FETCHED,
                             events_complete=True)
    assert signal["state"] == "READY"
    assert result["status"] == "PROPOSED" and result["cash_per_share"] == "0.42"
    settlement = event_readiness(result, FETCHED, purpose="settlement")
    assert settlement["state"] == "WAITING_DATA"
    assert {"EVENT_NOT_IMPLEMENTED", "UNKNOWN_EX_DATE"} <= set(settlement["issues"])


def test_shareholder_rejection_retains_complex_event_but_excludes_it_from_signals():
    plan = "吸收合并方案"
    result = normalize_eastmoney_events([em(
        ASSIGN_PROGRESS="股东大会否决", IMPL_PLAN_PROFILE=plan,
        EQUITY_RECORD_DATE=None, EX_DIVIDEND_DATE=None,
    )], "SH600000", FETCHED, raw_reference="fixture/rejected-plan.json")[0]
    assert result["status"] == "CANCELLED" and result["kind"] == "COMPLEX"
    assert result["source_plan"] == plan and result["raw_reference"] == "fixture/rejected-plan.json"
    signal = adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [result], FETCHED,
                             events_complete=True)
    assert signal["state"] == "READY" and all(not row["event_ids"] for row in signal["rows"])
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_rejected_and_implemented_observations_remain_an_explicit_status_conflict():
    result = normalize_eastmoney_events(
        [em(ASSIGN_PROGRESS="股东大会否决"), em()], "SH600000", FETCHED)[0]
    assert result["status"] == "UNKNOWN" and "CONFLICT_EVENT_STATUS" in result["conflicts"]
    assert {item["status"] for item in result["source_evidence"]} == {"CANCELLED", "IMPLEMENTED"}
    for combined in (result, merge_events([result])[0]):
        signal = adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [combined], FETCHED,
                                 events_complete=True)
        assert signal["state"] == "WAITING_DATA" and "CONFLICT_EVENT_STATUS" in signal["issues"]
        assert event_readiness(combined, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_accounting_phrase_exemption_does_not_hide_a_source_plan_amount_conflict():
    plan = "10派4.20元(含税，按合并报表口径披露)"
    result = normalize_eastmoney_events([em(
        IMPL_PLAN_PROFILE=plan, PRETAX_BONUS_RMB="8.4",
    )], "SH600000", FETCHED)[0]
    assert result["kind"] == "DISTRIBUTION" and result["cash_per_share"] is None
    assert result["conflicts"] == ["CONFLICT_CASH_PER_SHARE"]
    assert result["conflict_evidence"] == [{
        "field": "cash_per_share", "kind": "SOURCE_PLAN_MISMATCH",
        "structured_value": "0.84", "plan_value": "0.42", "source_plan": plan,
    }]
    signal = adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [result], FETCHED,
                             events_complete=True)
    assert signal["state"] == "WAITING_DATA" and "CONFLICT_CASH_PER_SHARE" in signal["issues"]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_two_distributions_for_same_reporting_period_are_distinct():
    second = em(PLAN_NOTICE_DATE="2026-06-01", NOTICE_DATE="2026-06-10",
                EQUITY_RECORD_DATE="2026-06-15", EX_DIVIDEND_DATE="2026-06-16")
    records = normalize_eastmoney_events([em(), second], "SH600000", FETCHED)
    assert len(records) == 2 and len({row["event_id"] for row in records}) == 2


def test_unknown_amount_is_not_fabricated_zero():
    result = event(dividCashStock="尚待公布", dividReserveToStockPs="")
    assert result["capital_per_share"] is None
    assert not result["terms_ready"]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "garbled"])
def test_invalid_event_amount_rejected(value):
    with pytest.raises(DataQualityError):
        event(dividCashPsBeforeTax=value)


def test_source_plan_conflict_retained_without_choosing_either_amount():
    result = event(dividCashPsBeforeTax="4.2")
    assert result["cash_per_share"] is None and not result["terms_ready"]
    assert result["conflicts"] == ["CONFLICT_CASH_PER_SHARE"]
    assert result["conflict_evidence"][0]["structured_value"] == "4.2"
    assert result["conflict_evidence"][0]["plan_value"] == "0.42"
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_identical_proposal_and_implementation_merge_with_original_stage_evidence():
    proposed = bao(dividPlanDate="", dividCashStock="10派4.2元")
    implemented = bao()
    rows = normalize_baostock_events([proposed, implemented], "SH600000", FETCHED,
                                    raw_reference="fixture/source.json")
    assert rows == normalize_baostock_events([implemented, proposed], "SH600000", FETCHED,
                                           raw_reference="fixture/source.json")
    assert len(rows) == 1
    result = rows[0]
    assert result["status"] == "IMPLEMENTED" and not result["conflicts"]
    assert result["published_date"] == "2026-07-10"
    assert result["cash_per_share"] == "0.42"
    assert {row["status"] for row in result["source_evidence"]} == {"PROPOSED", "IMPLEMENTED"}
    assert [row["source_row"] for row in result["source_evidence"]] == [proposed, implemented]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "READY"
    assert merge_events(rows)[0]["source_evidence"] == result["source_evidence"]


@pytest.mark.parametrize("changes, field", [
    ({"dividCashPsBeforeTax": "0.5", "dividCashStock": "10派5元"}, "cash_per_share"),
    ({"dividOperateDate": "2026-07-17", "dividPayDate": "2026-07-17"}, "ex_date"),
    ({"dividPayDate": "2026-07-17"}, "payment_date"),
])
def test_proposal_implementation_conflicts_remain_unknown(changes, field):
    proposed = bao(dividPlanDate="", dividCashStock="10派4.2元")
    result = normalize_baostock_events([proposed, bao(**changes)], "SH600000", FETCHED)[0]
    assert result[field] is None
    assert f"CONFLICT_{field.upper()}" in result["conflicts"]
    assert len(result["source_evidence"]) == 2
    assert result["conflict_evidence"]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_source_plan_conflict_does_not_block_other_events_or_disappear_on_merge():
    bad = bao(dividPlanAnnounceDate="2026-03-10", dividCashPsBeforeTax="0.3",
              dividCashStock="10派2元（含税，扣税后1.8或2元）")
    good = bao(dividPlanAnnounceDate="2026-04-01", dividOperateDate="2026-07-17",
               dividPayDate="2026-07-17")
    events = normalize_baostock_events([bad, good], "SH600000", FETCHED)
    assert len(events) == 2
    assert events[0]["cash_per_share"] is None
    assert events[0]["source_evidence"][0]["source_row"] == bad
    assert event_readiness(events[1], FETCHED, purpose="settlement")["state"] == "READY"
    other = normalize_eastmoney_events([em(PRETAX_BONUS_RMB=2,
                                         IMPL_PLAN_PROFILE="10派2元")], "SH600000", FETCHED)[0]
    combined = merge_events([events[0], other])[0]
    assert combined["cash_per_share"] is None
    assert combined["conflict_evidence"] == events[0]["conflict_evidence"]
    assert event_readiness(combined, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_incomplete_proposal_terms_do_not_claim_confirmed_implementation():
    proposed = bao(dividPlanDate="", dividCashPsBeforeTax="", dividCashStock="方案待定")
    result = normalize_baostock_events([proposed, bao()], "SH600000", FETCHED)[0]
    assert result["cash_per_share"] is None
    assert "STAGE_TERMS_INCOMPLETE" in result["conflicts"]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_conflicting_nonzero_amount_cannot_be_classified_as_no_distribution():
    result = event(dividCashStock="不分配不转增", dividCashPsBeforeTax="0.3")
    assert result["kind"] == "DISTRIBUTION"
    assert "CONFLICT_CASH_PER_SHARE" in result["conflicts"]
    assert adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [result], FETCHED,
                           events_complete=True)["state"] == "WAITING_DATA"


@pytest.mark.parametrize("changes", [{"dividPayDate": "2026-07-01"},
                                     {"dividRegistDate": "2026-07-15corrupt"}])
def test_malformed_or_impossible_event_dates_rejected(changes):
    with pytest.raises(DataQualityError):
        event(**changes)


def test_future_and_undated_announcements_are_retained():
    future = event(dividOperateDate="2026-07-20", dividPayDate="2026-07-20")
    proposal = event(dividPlanDate="", dividRegistDate="", dividOperateDate="", dividPayDate="")
    assert future["ex_date"] == "2026-07-20"
    assert proposal["status"] == "PROPOSED" and proposal["ex_date"] is None


def test_actual_fetch_proves_same_day_availability_without_invented_timestamp():
    result = event(dividPlanDate="2026-07-16")
    assert result["announced_at"] is None
    assert result["publication_precision"] == "date"
    assert event_readiness(result, FETCHED)["state"] == "READY"
    assert event_readiness(result, "2026-07-16T18:09:59+08:00")["state"] == "WAITING_DATA"


def test_publication_after_fetch_is_a_conflict():
    result = event(dividPlanDate="2026-07-17")
    assert "PUBLICATION_DATE_AFTER_FETCH" in event_readiness(result, FETCHED)["issues"]


def test_merge_deduplicates_and_fills_dates_with_all_evidence():
    first = event()
    second = normalize_eastmoney_events([em()], "SH600000", FETCHED)[0]
    merged = merge_events([second, first])
    assert len(merged) == 1 and len(merged[0]["evidence"]) == 2
    assert merged[0]["payment_date"] == "2026-07-16"
    assert event_readiness(merged[0], FETCHED, purpose="settlement")["state"] == "READY"


def test_merge_conflicting_amounts_does_not_silently_choose_a_usable_value():
    other = event(dividCashPsBeforeTax="0.5", dividCashStock="10派5元")
    merged = merge_events([event(), other])[0]
    assert merged["cash_per_share"] is None
    assert event_readiness(merged, FETCHED)["state"] == "WAITING_DATA"


@pytest.mark.parametrize("coarse, precise, per_ten", [
    ("0.159915", "0.1599147", "1.599147"),
    ("0.139081", "0.1390814", "1.390814"),
])
def test_matching_complete_plans_prove_cross_source_decimal_rounding(coarse, precise, per_ten):
    plan = f"10派{per_ten}元"
    primary = event(dividCashPsBeforeTax=coarse, dividCashStock=plan)
    secondary = normalize_eastmoney_events(
        [em(PRETAX_BONUS_RMB=per_ten, IMPL_PLAN_PROFILE=plan)], "SH600000", FETCHED)[0]
    for observations in ([primary, secondary], [secondary, primary]):
        result = merge_events(observations)[0]
        assert result["cash_per_share"] == precise and not result["conflicts"]
        assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "READY"
        assert {item["terms"]["cash_per_share"] for item in result["evidence"]} == {coarse, precise}
        note = result["precision_notes"][0]
        assert note["resolved_value"] == precise
        assert note["basis"] == "matching_complete_plans_and_decimal_rounding"
        assert note["absolute_tolerance"] == "0.000001"
        assert {item["value"] for item in note["observations"]} == {coarse, precise}


def test_proposal_implementation_rounding_uses_same_proof_and_keeps_original_rows():
    proposed = bao(dividPlanDate="", dividCashPsBeforeTax="0.159915",
                   dividCashStock="10派1.599147元")
    implemented = bao(dividCashPsBeforeTax="0.1599147", dividCashStock="10派1.599147元")
    result = normalize_baostock_events([proposed, implemented], "SH600000", FETCHED)[0]
    assert result["status"] == "IMPLEMENTED" and not result["conflicts"]
    assert result["cash_per_share"] == "0.1599147"
    assert len(result["precision_notes"]) == 1
    assert [item["source_row"] for item in result["source_evidence"]] == [proposed, implemented]
    assert merge_events([result])[0]["precision_notes"] == result["precision_notes"]


@pytest.mark.parametrize("coarse, coarse_plan, precise_plan", [
    ("0.159915", "方案待定", "10派1.599147元"),  # No complete source plan.
    ("0.159915", "10派1.59915元", "10派1.599147元"),  # Plans differ.
    ("0.159914", "10派1.599147元", "10派1.599147元"),  # Not nearest rounding.
    ("0.159916", "10派1.599147元", "10派1.599147元"),  # Outside tolerance.
    ("0.159915", "10派1.599148元", "10派1.599148元"),  # Plan value not quoted.
])
def test_nearby_amounts_without_rounding_proof_remain_conflicts(
        coarse, coarse_plan, precise_plan):
    first = event(dividCashPsBeforeTax=coarse, dividCashStock=coarse_plan)
    second = normalize_eastmoney_events(
        [em(PRETAX_BONUS_RMB="1.599147", IMPL_PLAN_PROFILE=precise_plan)],
        "SH600000", FETCHED)[0]
    result = merge_events([first, second])[0]
    assert result["cash_per_share"] is None
    assert "CONFLICT_CASH_PER_SHARE" in result["conflicts"]
    assert not result["precision_notes"]


def test_precision_resolution_does_not_erase_unknowns_or_existing_conflicts():
    coarse = event(dividCashPsBeforeTax="0.159915", dividCashStock="10派1.599147元")
    precise = event(dividCashPsBeforeTax="0.1599147", dividCashStock="10派1.599147元")
    unknown = event(dividCashPsBeforeTax="", dividCashStock="方案待定")
    for third in (unknown, {**unknown, "conflicts": ["CONFLICT_CASH_PER_SHARE"]}):
        result = merge_events([coarse, precise, third])[0]
        assert result["cash_per_share"] is None
        assert "CONFLICT_CASH_PER_SHARE" in result["conflicts"]
        assert not result["precision_notes"]


def test_amount_precision_does_not_relax_conflicting_dates():
    first = event(dividCashPsBeforeTax="0.159915", dividCashStock="10派1.599147元")
    second = event(dividCashPsBeforeTax="0.1599147", dividCashStock="10派1.599147元",
                   dividPayDate="2026-07-17")
    result = merge_events([first, second])[0]
    assert result["cash_per_share"] == "0.1599147"
    assert result["payment_date"] is None
    assert result["conflicts"] == ["CONFLICT_PAYMENT_DATE"]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "WAITING_DATA"


def test_cross_source_conflict_cannot_be_ignored_as_no_distribution():
    no_distribution = event(dividCashStock="不分配不转增", dividCashPsBeforeTax="0")
    merged = merge_events([no_distribution, event()])[0]
    assert merged["kind"] == "DISTRIBUTION" and merged["cash_per_share"] is None
    assert adjusted_series([bar("2026-07-15"), bar("2026-07-16")], [merged], FETCHED,
                           events_complete=True)["state"] == "WAITING_DATA"


def test_cash_ex_date_does_not_create_a_false_loss():
    bars = [bar("2026-07-15"), bar("2026-07-16", "9.58", "9.58")]
    result = adjusted_series(bars, [event()], FETCHED, events_complete=True)
    assert result["state"] == "READY"
    assert float(result["rows"][-1]["adjusted_close"]) == 10


def test_duplicate_sources_do_not_double_adjust():
    bars = [bar("2026-07-15"), bar("2026-07-16", "9.58", "9.58")]
    other = normalize_eastmoney_events([em()], "SH600000", FETCHED)[0]
    result = adjusted_series(bars, merge_events([event(), other]), FETCHED, events_complete=True)
    assert result["state"] == "READY" and float(result["rows"][-1]["adjusted_close"]) == 10


def test_future_fetched_event_cannot_repair_old_cutoff():
    bars = [bar("2026-07-15"), bar("2026-07-16", "9.58", "9.58")]
    result = adjusted_series(bars, [event()], "2026-07-16T18:09:00+08:00", events_complete=True)
    assert result["state"] == "WAITING_DATA" and not result["rows"]


def test_no_events_can_be_ready_only_with_coverage():
    bars = [bar("2026-07-15"), bar("2026-07-16")]
    assert adjusted_series(bars, [], FETCHED)["state"] == "WAITING_DATA"
    assert adjusted_series(bars, [], FETCHED, events_complete=True)["state"] == "READY"


def test_unexplained_reference_change_is_waiting_not_fabricated_adjustment():
    bars = [bar("2026-07-15"), bar("2026-07-16", "9.58", "9.58")]
    result = adjusted_series(bars, [], FETCHED, events_complete=True)
    assert "UNEXPLAINED_REFERENCE_CHANGE" in result["issues"]


RULE = {"rule_id": "fixture-mainboard", "effective_from": "2026-01-01",
        "limit_rate": "0.10", "price_tick": "0.01"}
SECURITY = {"code": "SH600000", "listed": "1999-11-10", "board": "沪市主板",
            "source_status": 1, "eligibility_known": True}


def test_normal_execution_facts_can_be_ready_without_order_decision():
    result = execution_facts(bar("2026-07-16"), SECURITY, [], FETCHED,
                             events_complete=True, rule_context=RULE)
    assert result["state"] == "READY"
    assert result["limit_up"] == "11.00" and result["limit_down"] == "9.00"
    assert "side" not in result


@pytest.mark.parametrize("changes, issue", [
    ({"source_status": None}, "SECURITY_STATUS_UNKNOWN"),
    ({"source_status": "unknown"}, "SECURITY_STATUS_UNKNOWN"),
    ({"eligibility_known": False}, "SECURITY_ELIGIBILITY_UNKNOWN"),
    ({"eligibility_known": None}, "SECURITY_ELIGIBILITY_UNKNOWN"),
])
def test_unknown_security_identity_does_not_use_normal_price_limits(changes, issue):
    result = execution_facts(bar("2026-07-16"), {**SECURITY, **changes}, [], FETCHED,
                             events_complete=True, rule_context=RULE)
    assert result["state"] == "WAITING_DATA"
    assert issue in result["issues"]
    assert result["limit_up"] is result["limit_down"] is None


def test_execution_reference_jump_without_known_event_cannot_be_ready():
    result = execution_facts(bar("2026-07-16", "5", "5", previous_close="10"),
                             SECURITY, [], FETCHED, events_complete=True, rule_context=RULE)
    assert result["state"] == "WAITING_DATA"
    assert "UNEXPLAINED_REFERENCE_CHANGE" in result["issues"]
    assert result["limit_up"] is result["limit_down"] is None


@pytest.mark.parametrize("previous_close", ["NaN", "Infinity", "-1", "bad"])
def test_invalid_previous_close_is_waiting_instead_of_ignored(previous_close):
    result = execution_facts(bar("2026-07-16", previous_close=previous_close),
                             SECURITY, [], FETCHED, events_complete=True, rule_context=RULE)
    assert result["state"] == "WAITING_DATA"
    assert any(issue.startswith("INVALID_PREVIOUS_CLOSE") for issue in result["issues"])


def test_normal_reference_with_known_previous_close_is_ready():
    result = execution_facts(bar("2026-07-16", previous_close="10"),
                             SECURITY, [], FETCHED, events_complete=True, rule_context=RULE)
    assert result["state"] == "READY"


def test_ex_date_uses_confirmed_terms_and_previous_close():
    result = execution_facts(bar("2026-07-16", "9.58", "9.58", previous_close="10"),
                             SECURITY, [event()], FETCHED, events_complete=True, rule_context=RULE)
    assert result["state"] == "READY" and result["reference_price"] == "9.58"
    assert result["limit_up"] == "10.54"


def test_unknown_rule_is_not_assumed_ten_percent():
    result = execution_facts(bar("2026-07-16"), SECURITY, [], FETCHED, events_complete=True)
    assert result["state"] == "WAITING_DATA" and result["limit_up"] is None


def test_price_only_fallback_is_waiting_without_raising():
    prices = bar("2026-07-16")
    del prices["isST"]
    del prices["preclose"]
    for result in [adjusted_series([prices], [], FETCHED, events_complete=True),
                   execution_facts(prices, SECURITY, [], FETCHED,
                                   events_complete=True, rule_context=RULE)]:
        assert result["state"] == "WAITING_DATA"
        assert "isST" in result["issues"][0] and "preclose" in result["issues"][0]


def test_source_ex_reference_disagreement_is_waiting():
    prices = bar("2026-07-16", "9.58", "9.60", previous_close="10")
    result = execution_facts(prices, SECURITY, [event()], FETCHED,
                             events_complete=True, rule_context=RULE)
    assert result["state"] == "WAITING_DATA"
    assert "EX_REFERENCE_SOURCE_CONFLICT" in result["issues"]
    signal = adjusted_series([bar("2026-07-15"), prices], [event()], FETCHED, events_complete=True)
    assert "EX_REFERENCE_SOURCE_CONFLICT" in signal["issues"]


def test_prices_outside_effective_limits_wait_for_facts_review():
    result = execution_facts(bar("2026-07-16", "12", "10"), SECURITY, [], FETCHED,
                             events_complete=True, rule_context=RULE)
    assert result["state"] == "WAITING_DATA"
    assert "PRICE_OUTSIDE_EFFECTIVE_LIMITS" in result["issues"]


def test_impossible_adjustment_returns_waiting_not_an_exception():
    excessive = event(dividCashPsBeforeTax="20", dividCashStock="10派200元")
    prices = bar("2026-07-16", previous_close="10")
    execution = execution_facts(prices, SECURITY, [excessive], FETCHED,
                                events_complete=True, rule_context=RULE)
    signal = adjusted_series([bar("2026-07-15"), prices], [excessive], FETCHED, events_complete=True)
    assert execution["state"] == signal["state"] == "WAITING_DATA"
    assert any("INVALID_ACTION_ADJUSTMENT" in issue for issue in signal["issues"])


def test_nested_provenance_is_checked_against_cutoff():
    prices = bar("2026-07-16")
    del prices["fetched_at"]
    prices["_provenance"] = {"available_at": "2026-07-16T18:11:00+08:00"}
    result = adjusted_series([prices], [], FETCHED, events_complete=True)
    assert "BAR_NOT_YET_AVAILABLE" in result["issues"]


def test_provider_explicit_empty_and_complete_pagination():
    payload = {"success": True, "result": {"count": 0, "pages": 0, "data": []}}
    provider = EastmoneyEventProvider(opener=lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    assert provider.fetch("events", {"code": "SH600000"})["coverage"]["complete"]


def test_provider_null_result_is_not_no_event_proof():
    payload = {"success": True, "result": None}
    provider = EastmoneyEventProvider(opener=lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    with pytest.raises(ProviderError) as caught:
        provider.fetch_events("SH600000")
    assert caught.value.category == "UNPUBLISHED"


def test_event_provider_respects_total_pagination_deadline(monkeypatch):
    import china_stock_sim.market.providers.eastmoney as module

    tick = [100.0]
    timeouts = []
    monkeypatch.setattr(module.time, "monotonic", lambda: tick[0])

    def opener(request, timeout):
        timeouts.append(timeout)
        tick[0] += 6
        payload = {"success": True, "result": {"count": 3, "pages": 3, "data": [em()]}}
        return io.BytesIO(json.dumps(payload).encode())

    with pytest.raises(ProviderError, match="总超时"):
        EastmoneyEventProvider(timeout=10, opener=opener).fetch_events("SH600000")
    assert timeouts == [10, 4]


def test_event_provider_production_uses_bounded_transport(monkeypatch):
    import china_stock_sim.market.providers.eastmoney as module

    calls = []

    def fetch(url, *, timeout):
        calls.append((url, timeout))
        return {"payload": {"success": True, "result": {"count": 0, "pages": 0, "data": []}}}

    monkeypatch.setattr(module, "fetch_json", fetch)
    assert EastmoneyEventProvider(timeout=8).fetch_events("SH600000")["rows"] == []
    assert len(calls) == 1 and 0 < calls[0][1] <= 8


def test_event_provider_preserves_retry_after_and_http_status():
    headers = Message()
    headers["Retry-After"] = "120"

    def opener(*args, **kwargs):
        raise HTTPError("https://datacenter-web.eastmoney.com", 429, "rate limited", headers, None)

    with pytest.raises(ProviderError) as caught:
        EastmoneyEventProvider(opener=opener).fetch_events("SH600000")
    assert caught.value.retryable and caught.value.retry_after_seconds == 120
    assert caught.value.status_code == 429 and caught.value.category == "NETWORK"


def test_event_provider_schema_error_is_not_a_network_failure():
    def opener(*args, **kwargs):
        return io.BytesIO(b'{"success":true,"result":{"pages":"bad"}}')
    with pytest.raises(ProviderError) as caught:
        EastmoneyEventProvider(opener=opener).fetch_events("SH600000")
    assert caught.value.category == "FORMAT" and not caught.value.retryable
