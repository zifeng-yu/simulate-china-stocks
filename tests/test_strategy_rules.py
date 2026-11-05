"""Offline acceptance of rule arithmetic, frozen targets and resource constraints."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from china_stock_sim.common.fees import ExecutionSupportError, fee_breakdown
from china_stock_sim.common.sizing import bounded_sell_quantity, intent_quantity
from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
from china_stock_sim.strategies.base import PlanningContext
from china_stock_sim.strategies.features import rank_features, rule_features, sample_volatility
from china_stock_sim.strategies.rules import plan
from china_stock_sim.strategies.sizing import buy_size, limit_price, sell_size

FEES = {"version": "test", "commission_rate": "0.0003", "commission_minimum_fen": 500,
        "commission_includes_exchange_fees": True, "sell_stamp_rate": "0.0005",
        "sell_stamp_effective_from": "2023-08-28", "transfer_rate_both_sides": "0.00001",
        "transfer_effective_from": "2022-04-29", "rounding": "ROUND_HALF_UP"}
SESSIONS = ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")
CODES = [f"SH{600000 + i:06d}" for i in range(25)]


def security(momentum=0, close="10"):
    prices = ["10"] * 121
    prices[100] = str(Decimal(10) + Decimal(momentum))
    return {"in_pool": True, "eligibility": "READY", "issues": [],
            "adjusted_close": prices, "raw_bars": [], "close": close}


def holding(code, shares, sellable=None):
    return {"code": code, "total_shares": shares,
            "sellable_shares": shares if sellable is None else sellable,
            "cost_fen": shares * 1000}


def context(*, strategy="S1", count=12, positions=(), cash=5_000_000, nav=5_000_000,
            parameters=None, **kwargs):
    values = dict(STRATEGY_DEFINITIONS[strategy]["defaults"])
    values.update(momentum_weight="1", low_volatility_weight="0")
    values.update(parameters or {})
    return PlanningContext(account_id="account", strategy_id=strategy,
        target_session=SESSIONS[0], market_as_of="2026-10-30",
        input_cutoff="2026-11-01T20:00:00+08:00", generated_at="2026-11-01T20:00:00+08:00",
        account={"positions": list(positions), "available_cash_fen": cash},
        policy={"parameters": values}, net_assets_fen=nav,
        securities={code: security(25 - index) for index, code in enumerate(CODES[:count])},
        fee_policy=FEES, task_sessions=SESSIONS, **kwargs)


def task(decision):
    return {**deepcopy(decision.calculation["new_task"]), "sessions": list(SESSIONS),
            "completed_quantities": {}, "status": "ACTIVE"}


def test_features_exact_indices_sample_denominator_and_no_annualization():
    prices = [str(Decimal(10) + Decimal(i) / 100) for i in range(121)]
    result = rule_features(prices)
    with localcontext() as ctx:
        ctx.prec = 34
        returns = [Decimal(prices[i]) / Decimal(prices[i - 1]) - 1 for i in range(61, 121)]
        mean = sum(returns) / 60
        assert result["momentum"] == Decimal(prices[100]) / Decimal(prices[0]) - 1
        assert result["volatility"] == (sum((r - mean) ** 2 for r in returns) / 59).sqrt()
    changed = prices.copy()
    changed[120] = "30"
    assert rule_features(changed)["momentum"] == result["momentum"]


def test_custom_window_and_llm_sample_volatility():
    prices = [str(Decimal(10) + i) for i in range(21)]
    assert rule_features(prices, 21, 5, 20)["momentum"] == Decimal("1.5")
    with localcontext() as ctx:
        ctx.prec = 34
        returns = [Decimal(b) / Decimal(a) - 1 for a, b in zip(prices, prices[1:])]
        mean = sum(returns) / 20
        assert sample_volatility(prices) == (sum((r - mean) ** 2 for r in returns) / 19).sqrt()


@pytest.mark.parametrize("prices,args", [(["1"] * 120, (121, 20, 60)),
    (["1"] * 21, (21, 20, 20)), (["1"] * 21, (21, 5, 21)),
    (["0"] * 121, (121, 20, 60)), ([1.1] * 121, (121, 20, 60))])
def test_invalid_features_never_fill_zero(prices, args):
    with pytest.raises(ValueError):
        rule_features(prices, *args)


def test_percentiles_ties_all_equal_empty_single_and_code_tiebreak():
    assert rank_features({}) == []
    one = {"x": {"momentum": Decimal("-0.2"), "volatility": Decimal(0)}}
    assert rank_features(one)[0]["score"] == Decimal("0.5")
    tied = rank_features({code: one["x"] for code in ["z", "a", "b"]})
    assert [row["code"] for row in tied] == ["a", "b", "z"]
    assert all(row["score"] == Decimal("0.5") for row in tied)
    values = {"a": {"momentum": Decimal(0), "volatility": Decimal(3)},
              "b": {"momentum": Decimal(2), "volatility": Decimal(1)},
              "c": {"momentum": Decimal(2), "volatility": Decimal(1)}}
    rows = rank_features(values)
    assert [(r["code"], r["rank"], r["score"]) for r in rows] == [
        ("b", 1, Decimal("0.75")), ("c", 2, Decimal("0.75")), ("a", 3, Decimal(0))]
    assert sample_volatility(["10"] * 61) == 0


def test_first_build_top_ten_without_daily_cap_and_fixed_target_weights():
    data = context()
    before = deepcopy(data)
    result = plan(data)
    assert data == before and result == plan(data)
    assert len(result.targets) == 10 and len(result.ordered_intents) == 10
    assert [t["code"] for t in result.targets] == CODES[:10]
    assert all(t["target_value_fen"] == 475_000 for t in result.targets)
    assert all(i["limit_price"] == "10.10" for i in result.ordered_intents)
    assert result.calculation["initial_evaluated"] is True
    assert result.calculation["new_task"]["end_session"] == SESSIONS[-1]


def test_fewer_candidates_not_reweighted_and_low_capital_no_parent():
    result = plan(context(count=2))
    assert [t["target_value_fen"] for t in result.targets] == [475_000, 475_000]
    poor = plan(context(count=2, nav=1_000, cash=1_000))
    assert poor.state == "READY" and not poor.ordered_intents and not poor.targets
    assert poor.task_id is None and poor.calculation["initial_evaluated"] is True
    assert "new_task" not in poor.calculation


def test_normal_day_empty_does_not_demand_unneeded_signals():
    data = context(positions=[holding(CODES[0], 105)], initial_evaluated=True)
    data.securities.clear()
    assert plan(data).no_order_reason == "NON_REBALANCE_DAY"


def test_missing_candidate_excluded_but_missing_held_rank_blocks():
    data = context(count=2)
    data.securities[CODES[0]]["adjusted_close"] = []
    result = plan(data)
    assert result.state == "READY" and result.targets[0]["code"] == CODES[1]
    held = replace(data, account={"positions": [holding(CODES[0], 100)],
                                  "available_cash_fen": 1_000_000})
    result = plan(held)
    assert result.state == "WAITING_INPUT" and not result.ordered_intents
    assert result.calculation["initial_evaluated"] is False


def test_all_unavailable_waits_but_known_ineligible_is_valid_empty():
    data = context(count=1)
    data.securities[CODES[0]]["eligibility"] = "UNAVAILABLE"
    assert plan(data).state == "WAITING_INPUT"
    data.securities[CODES[0]]["eligibility"] = "EXCLUDED"
    result = plan(data)
    assert result.state == "READY" and not result.targets
    assert result.calculation["initial_evaluated"] is True


@pytest.mark.parametrize("field,value", [("in_pool", False), ("eligibility", "EXCLUDED")])
def test_known_out_pool_or_excluded_holding_exit_needs_no_rank(field, value):
    data = context(count=1, positions=[holding(CODES[0], 105)])
    data.securities[CODES[0]].update({field: value, "adjusted_close": []})
    result = plan(data)
    assert result.ordered_intents[0]["quantity"] == 105
    assert result.ordered_intents[0]["reason"] == "EXIT"


def test_exit_requires_real_close_and_waiting_never_becomes_initial_complete():
    data = context(count=1, positions=[holding(CODES[0], 105)])
    data.securities[CODES[0]].update(in_pool=False, close=None)
    assert plan(data).state == "WAITING_INPUT"
    assert plan(data).calculation["initial_evaluated"] is False


def test_lt_retained_rank_20_occupies_slot_before_rank_1_new():
    data = context(strategy="S1-LT", count=21, positions=[holding(CODES[19], 105)],
                   parameters={"target_count": 1})
    result = plan(data)
    assert not result.targets and not result.ordered_intents
    retained = result.calculation["portfolio_targets"][0]
    assert retained["code"] == CODES[19] and retained["target_shares"] == 105


def test_lt_rank_21_exit_and_new_buy_waits_for_real_slot():
    data = context(strategy="S1-LT", count=21, positions=[holding(CODES[20], 105)],
                   parameters={"target_count": 1})
    result = plan(data)
    assert {t["kind"] for t in result.targets} == {"EXIT", "BUY"}
    assert [i["side"] for i in result.ordered_intents] == ["SELL"]
    assert any(s["reason"] == "HOLDING_SLOTS_FULL" for s in result.calculation["skipped"])


@pytest.mark.parametrize("shares,expected", [(105, None), (600, None), (601, "TRIM")])
def test_lt_no_topup_or_trim_at_twelve_percent_but_trim_above(shares, expected):
    result = plan(context(strategy="S1-LT", count=1, positions=[holding(CODES[0], shares)]))
    assert (result.targets[0]["kind"] if result.targets else None) == expected
    if expected:
        assert result.targets[0]["target_value_fen"] == 475_000
        assert result.targets[0]["target_shares"] == 400


def test_lt_keeps_more_than_target_count_without_arbitrary_selling():
    data = context(strategy="S1-LT", count=4,
                   positions=[holding(code, 100) for code in CODES[:3]],
                   parameters={"target_count": 2})
    result = plan(data)
    assert not result.targets
    assert len(result.calculation["portfolio_targets"]) == 3


def test_lt_custom_entrant_weight_uses_trim_target():
    data = context(strategy="S1-LT", count=1,
                   parameters={"target_weight": "0.095", "trim_target": "0.08"})
    result = plan(data)
    assert result.targets[0]["target_value_fen"] == 400_000
    assert result.targets[0]["target_shares"] == 400


@pytest.mark.parametrize("close,buy,sell", [("10.123", "10.22", "10.03"),
                                          ("0.001", "0.01", "0.01")])
def test_tick_direction_and_minimum(close, buy, sell):
    parameters = context().policy["parameters"]
    assert str(limit_price(close, "BUY", parameters)) == buy
    assert str(limit_price(close, "SELL", parameters)) == sell


def test_inclusive_fees_exact_budget_and_parent_cap():
    cost = -fee_breakdown(Decimal("10"), 100, "BUY", FEES, SESSIONS[0])["net_cash_fen"]
    assert buy_size(CODES[0], cost - 1, Decimal(10), FEES, SESSIONS[0])[0] == 0
    quantity, proof = buy_size(CODES[0], cost, Decimal(10), FEES, SESSIONS[0])
    assert quantity == 100 and proof["committed_cash_fen"] == cost
    assert buy_size(CODES[0], 100_000_000, Decimal(10), FEES, SESSIONS[0], cap=250)[0] == 200


@pytest.mark.parametrize("available,cap,expected", [(105, 105, 105), (105, 104, 100),
    (105, 5, 5), (105, 4, 0), (205, 201, 200), (205, 105, 105), (100, 99, 0),
    (105, 1000, 105), (1_000_105, 1_000_000, 1_000_000)])
def test_shared_bounded_sell_preserves_odd_lot_and_integer_cap(available, cap, expected):
    assert bounded_sell_quantity(available, cap) == expected
    result, proof = sell_size(CODES[0], holding(CODES[0], available), cap, Decimal(10),
                              FEES, SESSIONS[0])
    assert result == expected and proof["quantity"] == expected


def test_explicit_sell_quantity_is_not_silently_repaired():
    with pytest.raises(ExecutionSupportError):
        intent_quantity({"code": CODES[0], "side": "SELL", "price": Decimal(10), "quantity": 104},
                        [holding(CODES[0], 105)], FEES, SESSIONS[0])


def test_continuation_uses_actual_fills_not_new_ranks_or_increased_cash():
    data = context(count=2)
    first = plan(data)
    parent = task(first)
    parent["completed_quantities"][f"{CODES[0]}:BUY"] = 200
    next_data = replace(data, target_session=SESSIONS[1], parent_task=parent, month_end=True,
        account={"positions": [holding(CODES[0], 200)], "available_cash_fen": 50_000_000})
    next_data = deepcopy(next_data)
    next_data.securities[CODES[0]].update(close="5", adjusted_close=[])
    next_data.securities[CODES[1]].update(adjusted_close=[])
    next_data.securities[CODES[2]] = security(10000)
    result = plan(next_data)
    assert result.calculation["trigger"] == "MONTH_END_COALESCED"
    assert result.task_id == first.task_id and "new_task" not in result.calculation
    assert result.ordered_intents[0]["quantity"] == 200
    assert {i["code"] for i in result.ordered_intents} == set(CODES[:2])
    assert result.ordered_intents[0]["limit_price"] == "5.05"


def test_no_expected_sale_proceeds_or_expected_slot_release():
    data = context(count=2, cash=0, positions=[holding(CODES[2], 100)])
    data.securities[CODES[2]] = {**security(), "in_pool": False}
    result = plan(data)
    assert [i["side"] for i in result.ordered_intents] == ["SELL"]
    parent = task(result)
    parent["completed_quantities"] = {f"{CODES[2]}:SELL": 100}
    next_day = replace(data, target_session=SESSIONS[1], parent_task=parent,
                       account={"positions": [], "available_cash_fen": 200_000})
    buys = plan(next_day).ordered_intents
    assert buys and all(i["side"] == "BUY" for i in buys)
    assert sum(-fee_breakdown(Decimal(i["limit_price"]), i["quantity"], "BUY", FEES,
                             SESSIONS[1])["net_cash_fen"] for i in buys) <= 200_000


def test_remaining_sell_cannot_reverse_or_overshoot_target_after_position_change():
    data = context(count=1, positions=[holding(CODES[0], 1000)])
    parent = task(plan(data))
    result = plan(replace(data, target_session=SESSIONS[1], parent_task=parent,
                         account={"positions": [holding(CODES[0], 300)],
                                  "available_cash_fen": 5_000_000}))
    assert not result.ordered_intents


@pytest.mark.parametrize("state,target", [("EXPIRED", SESSIONS[1]),
    ("ACTIVE", "2026-11-09"), ("COMPLETE", SESSIONS[1])])
def test_expired_or_complete_parent_never_reopens_window(state, target):
    data = context(count=1)
    parent = task(plan(data))
    parent["status"] = state
    result = plan(replace(data, target_session=target, parent_task=parent, month_end=True))
    assert result.no_order_reason == "REBALANCE_WINDOW_CLOSED"
    assert not result.targets and "new_task" not in result.calculation


def test_unknown_vs_confirmed_changed_buy_eligibility_on_continuation():
    data = context(count=1)
    parent = task(plan(data))
    child = replace(deepcopy(data), target_session=SESSIONS[1], parent_task=parent)
    child.securities[CODES[0]]["eligibility"] = "UNAVAILABLE"
    assert plan(child).state == "WAITING_INPUT"
    child.securities[CODES[0]]["eligibility"] = "EXCLUDED"
    result = plan(child)
    assert result.state == "READY" and not result.ordered_intents
    assert result.calculation["skipped"][0]["reason"] == "BUY_ELIGIBILITY_CHANGED"


def test_full_actual_calendar_window_required_and_nonpositive_nav_no_buy():
    assert plan(replace(context(), task_sessions=SESSIONS[:2])).state == "WAITING_INPUT"
    assert not plan(context(nav=0)).ordered_intents
    assert not plan(context(nav=-100)).ordered_intents


def test_parent_parameter_change_and_overfilled_parent_are_rejected():
    data = context(count=1)
    parent = task(plan(data))
    parent["parameters"]["buy_limit_offset"] = "0.02"
    with pytest.raises(ValueError, match="参数"):
        plan(replace(data, parent_task=parent))
    parent = task(plan(data))
    parent["completed_quantities"] = {f"{CODES[0]}:BUY": 10_000}
    with pytest.raises(ValueError, match="成交量"):
        plan(replace(data, parent_task=parent))
