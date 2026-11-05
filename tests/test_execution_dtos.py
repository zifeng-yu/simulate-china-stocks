"""LX02 wire shapes distinguish proposals, frozen evidence and pending money."""

from dataclasses import MISSING, asdict, fields
from datetime import date, datetime
from decimal import Decimal
from typing import get_type_hints

import pytest
from test_ledger_trading import book, call, plan_payload

from china_stock_sim import domain
from china_stock_sim.execution.base import ExecutionModel
from china_stock_sim.strategies.base import PlanningContext, Strategy, StrategyDecision

assert book  # Public ledger fixture, isolated under tmp_path.


def result_values(state="WAITING_DATA"):
    money = None if state == "WAITING_DATA" else 0
    return {
        "result_id": "result-1", "order_id": "order-1", "plan_id": "plan-1",
        "account_id": "LLM", "code": "SH600000", "side": "BUY",
        "target_session": "2026-10-30", "state": state,
        "execution_model": "NEXT_SESSION_DAILY_OPEN_V1", "fee_policy_hash": "fee-proof",
        "rule_policy_hash": "rule-proof", "ruleset_hash": "ruleset-proof",
        "computed_at": "2026-10-30T19:00:00+08:00",
        "knowledge_cutoff": "2026-10-30T18:30:00+08:00",
        "account_view_hash": "view-proof", "account_view_evidence": {}, "account_revision": 2,
        "reason_code": "MISSING_BAR" if state == "WAITING_DATA" else "SUSPENDED",
        "reason_details": {}, "reason_evidence": {}, "execution_context": None,
        "market_snapshot_ids": (), "data_evidence": (), "quantity": money, "price": None,
        "amount_fen": money, "commission_fen": money, "stamp_fen": money,
        "transfer_fen": money, "fees_fen": money, "net_cash_fen": money,
    }


def test_execution_contract_has_no_optional_identity_or_money_defaults():
    for field in fields(domain.ExecutionResult):
        if field.name == "reason":
            continue
        assert field.default is MISSING and field.default_factory is MISSING
    for key in ("order_id", "account_view_hash", "account_view_evidence", "reason_evidence",
                "quantity", "net_cash_fen"):
        payload = result_values()
        del payload[key]
        with pytest.raises(TypeError):
            domain.ExecutionResult(**payload)


@pytest.mark.parametrize("field", [
    "result_id", "order_id", "plan_id", "account_id", "code", "target_session",
    "execution_model", "fee_policy_hash", "rule_policy_hash", "ruleset_hash",
    "computed_at", "knowledge_cutoff", "account_view_hash", "account_view_evidence", "account_revision",
    "reason_code", "reason_details", "reason_evidence",
])
def test_required_result_identity_cannot_be_none(field):
    with pytest.raises(ValueError):
        domain.ExecutionResult(**(result_values() | {field: None}))


def test_pending_and_terminal_zeroes_are_distinct_serialized_contracts():
    waiting = asdict(domain.ExecutionResult(**result_values()))
    unfilled = asdict(domain.ExecutionResult(**result_values("NOT_FILLED")))
    for key in ("quantity", "amount_fen", "commission_fen", "stamp_fen", "transfer_fen",
                "fees_fen", "net_cash_fen"):
        assert waiting[key] is None
        assert type(unfilled[key]) is int and unfilled[key] == 0
    assert waiting["price"] is unfilled["price"] is None
    with pytest.raises(ValueError):
        domain.ExecutionResult(**(result_values() | {"fees_fen": 0}))
    with pytest.raises(ValueError):
        domain.ExecutionResult(**(result_values("NOT_FILLED") | {"quantity": None}))
    with pytest.raises(ValueError):
        domain.ExecutionResult(**(result_values("NOT_FILLED") | {"fees_fen": False}))


@pytest.mark.parametrize("price", [10.0, True, "NaN", "Infinity", "bad", "-1"])
def test_result_wire_rejects_non_decimal_or_non_finite_price(price):
    payload = result_values("SIMULATED_FILL") | {"quantity": 100, "price": price}
    with pytest.raises(ValueError):
        domain.ExecutionResult(**payload)


def test_fill_keeps_decimal_price_integer_fen_and_signed_cash():
    payload = result_values("SIMULATED_FILL") | {
        "quantity": 100, "price": Decimal("10.01"), "amount_fen": 100100,
        "commission_fen": 500, "stamp_fen": 0, "transfer_fen": 1,
        "fees_fen": 501, "net_cash_fen": -100601, "reason_code": "OPEN_MATCH",
    }
    result = domain.ExecutionResult(**payload)
    assert result.price == Decimal("10.01") and result.net_cash_fen == -100601
    with pytest.raises(ValueError):
        domain.ExecutionResult(**(payload | {"computed_at": datetime(2026, 10, 30, 19)}))


def test_proposal_holds_sizing_intent_without_claiming_a_frozen_plan():
    intents = (
        domain.ExecutionIntent("SH600000", "BUY", "budget", 0, "10", budget_fen=200000),
        domain.ExecutionIntent("SZ000001", "SELL", "reduce", 1, "9", sell_fraction="0.5"),
        domain.ExecutionIntent("SH600001", "HOLD", "unchanged", 2, None),
    )
    proposal = domain.StrategyProposal(
        proposal_id="proposal-1", account_id="LLM", strategy_id="LLM-Daily",
        strategy_version="fixture", target_session=date(2026, 10, 30),
        market_as_of="2026-10-29", generated_at="2026-10-29T20:00:00+08:00",
        input_cutoff="2026-10-29T19:00:00+08:00", account_snapshot_id="account-snapshot",
        account_revision=1, ordered_intents=intents,
    )
    encoded = asdict(proposal)
    assert encoded["ordered_intents"][0]["budget_fen"] == 200000
    assert encoded["ordered_intents"][1]["sell_fraction"] == "0.5"
    assert "frozen_at" not in encoded and "plan_hash" not in encoded
    assert not hasattr(domain, "TradingPlan") and not hasattr(domain, "TradeIntent")


def test_frozen_order_unspecified_reservation_differs_from_declared_zero():
    unspecified = domain.FrozenOrder("order-1", "SH600000", "BUY", 100, Decimal("10"), "buy")
    explicit = domain.FrozenOrder("order-1", "SH600000", "BUY", 100, Decimal("10"), "buy",
                                  reserved_cash_fen=0, reserved_shares=0)
    assert unspecified.reserved_cash_fen is unspecified.reserved_shares is None
    assert explicit.reserved_cash_fen == explicit.reserved_shares == 0


def test_public_protocols_accept_frozen_inputs_and_emit_strategy_decisions():
    match = get_type_hints(ExecutionModel.match)
    assert match["plan"] is domain.FrozenPlan
    assert match["account"] is domain.ExecutionAccountView
    assert match["market"] is domain.ExecutionMarketBundle
    assert get_type_hints(Strategy.plan)["return"] is StrategyDecision
    assert get_type_hints(Strategy.plan)["context"] is PlanningContext


def test_frozen_output_types_match_real_public_ledger_dictionaries(book):
    payload = plan_payload(book, orders=[])
    result = call(book, "freeze_plan", payload)
    assert result["state"] == "APPLIED", result
    plan = book.get_plan("LLM", payload["plan_id"])
    view = book.execution_view("LLM", payload["plan_id"])
    assert domain.FrozenPlan.__required_keys__ <= plan.keys()
    assert domain.ExecutionAccountView.__required_keys__ <= view.keys()
    assert view["frozen_plan"] == plan
