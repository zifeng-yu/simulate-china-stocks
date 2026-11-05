from datetime import date, timedelta
from decimal import Decimal

import pytest

from china_stock_sim.market.quality import (
    DataQualityError,
    compare_overlapping_bars,
    normalize_bars,
    validate_window,
)
from china_stock_sim.market.universe import normalize_securities, select_pool


@pytest.fixture
def sessions():
    # Fixture sessions represent a supplied exchange calendar, not calendar inference.
    first = date(2026, 1, 5)
    return [(first + timedelta(days=offset)).isoformat() for offset in range(121)]


def bar(code="sh.600000", session="2026-10-26", **changes):
    return {
        "date": session,
        "code": code,
        "open": "10.10",
        "high": "11.00",
        "low": "9.90",
        "close": "10.50",
        "preclose": "10.00",
        "volume": "100000",
        "amount": "1020000.123456",
        "tradestatus": "1",
        "isST": "0",
        "adjustflag": "3",
        **changes,
    }


def security(code="sh.600000", name="浦发银行", **changes):
    return {
        "code": code,
        "code_name": name,
        "ipoDate": "2000-01-01",
        "outDate": "",
        "type": "1",
        "status": "1",
        **changes,
    }


def history(code, sessions, amount="100000"):
    volume = str(int(Decimal(amount) / 10))
    return [bar(code, session, amount=amount, volume=volume) for session in sessions[-20:]]


def test_decimal_prices_quantities_flags_and_raw_values_are_preserved():
    row = normalize_bars([bar()], "SH600000", ["2026-10-26"])[0]
    assert row["code"] == "SH600000"
    assert row["open"] == "10.10"
    assert row["amount"] == "1020000.123456"
    assert row["volume"] == 100000
    assert row["tradestatus"] == 1
    assert row["isST"] == 0
    assert row["adjustflag"] == 3
    assert row["volume_raw_bao"] == "100000"
    assert row["amount_raw_bao"] == "1020000.123456"
    assert row["halt_normalized"] is False


def test_only_proved_halt_can_normalize_blank_quantities():
    source = bar(
        open="10",
        high="10",
        low="10",
        close="10",
        preclose="10",
        tradestatus="0",
        volume="",
        amount=" ",
    )
    row = normalize_bars([source])[0]
    assert row["volume"] == 0
    assert row["amount"] == "0"
    assert row["volume_raw_bao"] == ""
    assert row["amount_raw_bao"] == " "
    assert row["halt_normalized"] is True
    assert normalize_bars([row])[0] == row
    unchanged = normalize_bars([{**source, "volume": "0", "amount": "0"}])[0]
    assert unchanged["halt_normalized"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"open": ""},
        {"volume": ""},
        {"amount": ""},
        {"volume": "NaN"},
        {"amount": "Infinity"},
        {"close": "-1"},
        {"amount": "-1"},
        {"low": "10.6"},
        {"high": "10.4"},
        {"preclose": "0"},
        {"volume": "12.3"},
        {"isST": ""},
        {"isST": "2"},
        {"tradestatus": "unknown"},
        {"adjustflag": "1"},
        {"adjustflag": "2"},
        {"date": "20261026"},
        {"date": "2026-02-30"},
        {"code": "600000"},
        {"code": "sh.600.000"},
    ],
)
def test_invalid_source_values_are_rejected(changes):
    with pytest.raises(DataQualityError):
        normalize_bars([bar(**changes)])


@pytest.mark.parametrize("field", ["volume", "amount"])
@pytest.mark.parametrize("bad", ["NaN", "Inf", "nonsense", "-1"])
def test_halted_invalid_quantities_are_never_treated_as_blank(field, bad):
    source = bar(
        open="10",
        high="10",
        low="10",
        close="10",
        preclose="10",
        tradestatus="0",
        volume="",
        amount="",
    )
    source[field] = bad
    with pytest.raises(DataQualityError):
        normalize_bars([source])


def test_halt_requires_flat_prices_and_zero_quantities():
    with pytest.raises(DataQualityError):
        normalize_bars([bar(tradestatus="0", volume="", amount="")])
    with pytest.raises(DataQualityError):
        normalize_bars(
            [bar(open="10", high="10", low="10", close="10", tradestatus="0", amount="0")]
        )


def test_required_adjustflag_and_request_identity_are_checked():
    source = bar()
    del source["adjustflag"]
    with pytest.raises(DataQualityError, match="adjustflag"):
        normalize_bars([source])
    with pytest.raises(DataQualityError, match="证券代码不符"):
        normalize_bars([bar()], expected_code="SH600001")
    with pytest.raises(DataQualityError, match="交易日越界"):
        normalize_bars([bar()], allowed_dates=["2026-10-27"])
    with pytest.raises(DataQualityError, match="重复证券交易日"):
        normalize_bars([bar(), bar("SH600000")])


def test_window_does_not_claim_success_when_one_day_is_missing(sessions):
    rows = [bar(session=session) for session in sessions]
    assert validate_window(rows, "SH600000", sessions) == {
        "complete": True,
        "missing_dates": [],
        "expected_rows": 121,
        "actual_rows": 121,
    }
    del rows[60]
    assert validate_window(rows, "SH600000", sessions) == {
        "complete": False,
        "missing_dates": [sessions[60]],
        "expected_rows": 121,
        "actual_rows": 120,
    }


def test_window_rejects_duplicate_or_reversed_calendar(sessions):
    with pytest.raises(DataQualityError, match="交易日窗口"):
        validate_window([], "SH600000", sessions[::-1])
    with pytest.raises(DataQualityError, match="交易日窗口"):
        validate_window([], "SH600000", sessions + [sessions[-1]])


@pytest.mark.parametrize(
    "code",
    [
        "sh.600000",
        "sh.601000",
        "sh.603000",
        "sh.605000",
        "sz.000001",
        "sz.001001",
        "sz.002001",
        "sz.003001",
    ],
)
def test_main_board_requires_source_stock_type_and_explicit_code_scheme(code):
    row = normalize_securities([security(code)], "2026-10-26")[0]
    assert row["eligible_metadata"] is True
    assert "非交易所官方" in row["board_basis"]
    assert row["corporate_actions_status"] == "pending_verification"
    not_stock = normalize_securities([security(code, type="2")], "2026-10-26")[0]
    assert not_stock["eligible_metadata"] is False
    assert not_stock["security_type"] is None


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"code": "sh.688001"}, "代码范围"),
        ({"code": "sz.300001"}, "代码范围"),
        ({"code": "bj.920001"}, "代码范围"),
        ({"code": "sh.510300", "type": "5"}, "type=1"),
        ({"type": ""}, "type=1"),
        ({"status": "unknown"}, "状态未知"),
        ({"status": "0"}, "非上市状态"),
        ({"ipoDate": ""}, "上市日期未知"),
        ({"ipoDate": "2030-01-01"}, "尚未上市"),
        ({"outDate": "2025-01-01"}, "已退市"),
        ({"outDate": "invalid"}, "退市日期无效"),
        ({"code_name": "*ST某股"}, "风险警示"),
        ({"code_name": "某股退"}, "退市标记"),
    ],
)
def test_unknown_or_ineligible_metadata_keeps_an_exclusion_reason(changes, reason):
    row = normalize_securities([security(**changes)], "2026-10-26")[0]
    assert row["eligible_metadata"] is False
    assert reason in row["exclusion_reason"]


def test_source_metadata_missing_fields_and_canonical_duplicates():
    source = security()
    del source["outDate"]
    row = normalize_securities([source], "2026-10-26")[0]
    assert row["eligible_metadata"] is False
    assert "outDate" in row["exclusion_reason"]
    with pytest.raises(DataQualityError, match="代码重复"):
        normalize_securities([security(), security("SH600000")], "2026-10-26")


def test_pool_ranks_turnover_only_and_breaks_ties_by_code(sessions):
    rows = [security("sh.600002"), security("sh.600000"), security("sh.600001")]
    metadata = normalize_securities(rows, sessions[-1])
    bars = history("sh.600002", sessions, "500") + history("sh.600000", sessions, "100")
    bars += history("sh.600001", sessions, "500")
    pool = select_pool(metadata, bars, sessions, max_size=2)
    assert [row["code"] for row in pool["selected"]] == ["SH600001", "SH600002"]
    assert pool["selected"][0]["mean_amount20"] == "500"
    assert pool["eligible_count"] == 3
    assert pool["selected_count"] == 2
    assert pool["requested_size"] == 2
    assert pool["ready"] is True
    assert pool["corporate_actions_status"] == "pending_verification"
    assert pool["excluded"][0]["code"] == "SH600000"
    assert "排名" in pool["excluded"][0]["reason"]


def test_pool_rejects_young_missing_st_halted_zero_and_bad_metadata(sessions):
    rows = [security(f"sh.60000{number}") for number in range(7)]
    rows[1]["ipoDate"] = sessions[1]
    rows[6]["status"] = "unknown"
    metadata = normalize_securities(rows, sessions[-1])
    bars = []
    for number in range(7):
        stock_bars = history(f"sh.60000{number}", sessions)
        if number == 2:
            del stock_bars[10]
        elif number == 3:
            stock_bars[-1]["isST"] = "1"
        elif number == 4:
            stock_bars[-1].update(
                open="10", high="10", low="10", close="10", tradestatus="0", volume="", amount=""
            )
        elif number == 5:
            for row in stock_bars:
                row["amount"] = "0"
                row["volume"] = "0"
        bars.extend(stock_bars)
    pool = select_pool(metadata, bars, sessions)
    assert [row["code"] for row in pool["selected"]] == ["SH600000"]
    assert pool["selected_count"] == 1
    assert pool["requested_size"] == 100
    rejected = {row["code"]: row for row in pool["excluded"]}
    assert "121" in rejected["SH600001"]["reason"]
    assert rejected["SH600002"]["missing_dates"] == [sessions[-20:][10]]
    assert "ST" in rejected["SH600003"]["reason"]
    assert "停牌" in rejected["SH600004"]["reason"]
    assert "成交额" in rejected["SH600005"]["reason"]
    assert "状态未知" in rejected["SH600006"]["reason"]


def test_empty_pool_is_not_ready_and_short_window_is_rejected(sessions):
    metadata = normalize_securities([security()], sessions[-1])
    result = select_pool(metadata, [], sessions)
    assert result["selected"] == []
    assert result["ready"] is False
    assert len(result["excluded"][0]["missing_dates"]) == 20
    with pytest.raises(DataQualityError, match="121"):
        select_pool(metadata, [], sessions[-20:])


def test_metadata_snapshot_must_match_preparation_cutoff(sessions):
    metadata = normalize_securities([security()], sessions[-2])
    result = select_pool(metadata, history("sh.600000", sessions), sessions)
    assert result["ready"] is False
    assert "快照日期" in result["excluded"][0]["reason"]


@pytest.mark.parametrize("volume,amount", [("0", "100000"), ("100000", "0")])
def test_volume_and_amount_zero_states_must_agree(volume, amount):
    with pytest.raises(DataQualityError, match="零值状态不一致"):
        normalize_bars([bar(volume=volume, amount=amount)])


@pytest.mark.parametrize("amount", ["988.9", "1101.1"])
def test_vwap_cannot_exceed_high_low_plus_one_cent(amount):
    with pytest.raises(DataQualityError, match="成交均价"):
        normalize_bars([bar(volume="100", amount=amount)])


@pytest.mark.parametrize("amount", ["989", "1101"])
def test_vwap_accepts_exact_one_cent_rounding_tolerance(amount):
    assert normalize_bars([bar(volume="100", amount=amount)])[0]["amount"] == amount


def test_overlap_comparison_uses_decimal_equality_and_reports_both_values():
    first = normalize_bars([bar()])
    second = normalize_bars([bar(open="10.100")])
    assert compare_overlapping_bars(first, second) == []
    second[0]["close"] = "10.60"
    conflicts = compare_overlapping_bars(first, second)
    assert conflicts == [
        {
            "code": "SH600000",
            "date": "2026-10-26",
            "fields": {"close": {"batch": "10.50", "history": "10.60"}},
        }
    ]
