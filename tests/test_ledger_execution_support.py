"""LX01/LX04: original resources and transaction/retry semantics, in isolation."""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from test_ledger_trading import CODE, DAY, book, revision

from china_stock_sim.ledger.execution_evidence import account_input_hash
from china_stock_sim.ledger.execution_support import (
    apply_execution_batch,
    assert_execution_guards,
    capture_original_resources,
)
from china_stock_sim.ledger.kernel import LedgerError, digest

assert book  # Shared isolated LedgerService fixture; network connections fail.
NEXT = "2026-11-02"


def mutate(book, change, *, day=DAY):
    rev = revision(book)
    result = book._mutate("LLM", f"support-fixture:{rev}", {"fixture": rev}, rev, day, change)
    assert result["state"] == "APPLIED", result
    return result


def freeze_fixture(book, sides=("BUY",), *, legacy=False, cash_per_buy=1_000_510):
    """Seed only immutable input facts, so tests do not depend on plan validation.

    The ledger kernel, public view and all guard/batch behavior are the real
    implementations. Full validate/freeze/result integration is tested elsewhere.
    """
    def freeze(tx):
        for index, side in enumerate(sides):
            if side != "SELL":
                continue
            lot = {"lot_id": f"original:{index}", "code": f"SH60000{index}",
                   "quantity": 1000, "cost_fen": 1_000_000,
                   "acquired_date": "2026-10-29", "sellable_date": DAY,
                   "tax_start_date": "2026-10-29", "tax_start_status": "KNOWN"}
            tx.append("position_lots", lot["lot_id"], lot)
            tx.append("lot_movements", lot["lot_id"], {
                "lot_id": lot["lot_id"], "code": lot["code"], "effective_date": "2026-10-29",
                "quantity_delta": 1000, "cost_delta_fen": 1_000_000, "kind": "FIXTURE"})
        snapshot = book._account_state_tx(tx, DAY)
        orders, reserves = [], []
        for index, side in enumerate(sides):
            code, oid = f"SH60000{index}", f"plan:{index}"
            reserve = {"order_id": oid, "code": code, "side": side, "status": "ACTIVE",
                       "cash_fen": cash_per_buy if side == "BUY" else 0,
                       "shares": 1000 if side == "SELL" else 0,
                       "lot_allocations": [{"lot_id": f"original:{index}", "quantity": 1000}]
                       if side == "SELL" else []}
            order = {"order_id": oid, "plan_id": "plan", "code": code, "side": side,
                     "quantity": 1000, "limit_price": "10.00", "target_session": DAY,
                     "order_index": index, "reserved_cash_fen": reserve["cash_fen"],
                     "reserved_shares": reserve["shares"], "status": "PENDING"}
            orders.append(order)
            reserves.append(reserve)
        plan = {"plan_id": "plan", "target_session": DAY, "orders": orders,
                "order_ids": [o["order_id"] for o in orders], "reservations": reserves,
                "buy_budget_fen": sum(r["cash_fen"] for r in reserves),
                "account_snapshot": snapshot, "account_snapshot_hash": account_input_hash(snapshot),
                "account_revision_at_freeze": tx.state["account_revision"], "frozen_at": tx.now}
        if not legacy:
            plan["original_resources"] = capture_original_resources(tx, snapshot, orders, reserves)
        plan["plan_hash"] = digest(plan)
        tx.append("plans", "plan", plan)
        for order, reserve in zip(orders, reserves, strict=True):
            tx.append("orders", order["order_id"], order)
            tx.append("reservations", order["order_id"], reserve)
        return {}
    mutate(book, freeze)
    return book.get_plan("LLM", "plan")


def guard(book, order_id, *, expense=1_000_510):
    def check(tx):
        plan, order = tx.get("plans", "plan"), tx.get("orders", order_id)
        return assert_execution_guards(book, tx, plan, order, expense_fen=expense)
    return book._read("LLM", check)


def issue_kinds(view):
    return {i["kind"] for i in view["issues"]}


def test_view_is_read_only_stable_and_restores_own_cash_and_fifo(book):
    freeze_fixture(book, ("SELL", "BUY"))
    before = book.settings.database.read_bytes()
    first = book.execution_view("LLM", "plan")
    book.test_now[0] += timedelta(seconds=10)
    second = book.execution_view("LLM", "plan")
    assert first["view_hash"] == second["view_hash"]
    assert first["snapshot_at"] != second["snapshot_at"]
    assert book.settings.database.read_bytes() == before
    sell, buy = first["orders"]
    assert sell["own_reserved_shares"] == 1000
    assert sell["frozen_lot_allocations"] == [{"lot_id": "original:0", "quantity": 1000}]
    assert buy["own_reserved_cash_fen"] == buy["max_payable_fen"] == 1_000_510
    assert guard(book, "plan:0")["account_revision"] == revision(book)
    with pytest.raises(LedgerError, match="原订单资源"):
        guard(book, "plan:1")


def test_all_reserved_cash_does_not_self_block(book):
    freeze_fixture(book, cash_per_buy=5_000_000)
    assert book.account_state("LLM", DAY)["available_cash_fen"] == 0
    view = guard(book, "plan:0", expense=5_000_000)
    assert view["orders"][0]["max_payable_fen"] == 5_000_000


def test_legacy_resources_wait_instead_of_fabricating_original_cash(book):
    freeze_fixture(book, legacy=True)
    assert "ORIGINAL_RESOURCES_UNPROVEN" in issue_kinds(book.execution_view("LLM", "plan"))
    with pytest.raises(LedgerError):
        guard(book, "plan:0")


def test_next_day_income_cannot_repair_original_day_resources(book):
    freeze_fixture(book)
    mutate(book, lambda tx: tx.cash("late-dividend", "DIVIDEND", 2_000_000, NEXT, "later-case"))
    view = book.execution_view("LLM", "plan")
    assert view["original_resources"]["cash_fen"] == 5_000_000
    assert view["commit_guards"]["cash_fen"] == 7_000_000
    assert "LATER_FINANCIAL_DEPENDENCY" in issue_kinds(view)
    with pytest.raises(LedgerError):
        guard(book, "plan:0")


def test_same_day_credit_does_not_cover_new_tax_against_original_resources(book):
    freeze_fixture(book, cash_per_buy=5_000_000)
    def new_liability(tx):
        tx.cash("new-dividend", "DIVIDEND", 2_000_000, DAY, "new-case")
        tx.append("tax_items", "tax", {"assessed_fen": 1_000_000, "paid_fen": 0,
                                       "status": "PAYABLE", "effective_date": DAY})
    mutate(book, new_liability)
    view = book.execution_view("LLM", "plan")
    assert view["commit_guards"]["cash_fen"] == 7_000_000
    assert view["orders"][0]["max_payable_fen"] == 4_000_000
    with pytest.raises(LedgerError, match="原资金"):
        guard(book, "plan:0", expense=4_500_000)


def test_paying_frozen_tax_does_not_count_it_twice(book):
    mutate(book, lambda tx: tx.append("tax_items", "old-tax", {
        "assessed_fen": 1000, "paid_fen": 0, "status": "PAYABLE", "effective_date": "2026-10-29"}))
    freeze_fixture(book, cash_per_buy=4_999_000)
    def pay(tx):
        tax = tx.get("tax_items", "old-tax")
        tax.update(paid_fen=1000, status="PAID")
        tx.save("tax_items", "old-tax", tax)
        tx.cash("tax-paid", "DIVIDEND_TAX", -1000, DAY, "old-tax")
    mutate(book, pay)
    view = guard(book, "plan:0", expense=4_999_000)
    assert view["commit_guards"]["additional_tax_fen"] == 0
    assert view["orders"][0]["max_payable_fen"] == 4_999_000


def test_sale_cost_above_proceeds_consumes_original_cash(book):
    freeze_fixture(book, ("SELL", "BUY"), cash_per_buy=5_000_000)
    def sale(tx):
        order = tx.get("orders", "plan:0")
        order["status"] = "FILLED"
        tx.save("orders", "plan:0", order)
        reservation = tx.get("reservations", "plan:0")
        reservation["status"] = "RELEASED"
        tx.save("reservations", "plan:0", reservation)
        tx.append("fills", "plan:0", {"order_id": "plan:0", "plan_id": "plan", "side": "SELL",
                                      "net_cash_fen": -400, "effective_date": DAY})
        tx.cash("sale-net", "SELL_FEE", -400, DAY, "plan:0")
        tx.cash("later-credit", "DIVIDEND", 1000, DAY, "case")
    mutate(book, sale)
    view = book.execution_view("LLM", "plan")
    assert view["commit_guards"]["sale_cash_debits_fen"] == 400
    assert view["orders"][1]["max_payable_fen"] == 4_999_600
    with pytest.raises(LedgerError):
        guard(book, "plan:1", expense=5_000_000)


def test_unknown_tax_does_not_block_legal_sale_but_blocks_buy(book):
    freeze_fixture(book, ("SELL", "BUY"))
    mutate(book, lambda tx: tx.append("tax_items", "unknown", {
        "assessed_fen": None, "paid_fen": 0, "status": "WAITING_DATA", "effective_date": DAY}))
    guard(book, "plan:0")
    with pytest.raises(LedgerError) as error:
        guard(book, "plan:1")
    assert "UNKNOWN_TAX_LIABILITY" in {i["kind"] for i in error.value.details["issues"]}


@pytest.mark.parametrize("change", ["t_plus_one", "reservation", "unknown_movement"])
def test_frozen_sell_allocation_cannot_be_silently_substituted(book, change):
    freeze_fixture(book, ("SELL",))
    def edit(tx):
        if change == "t_plus_one":
            lot = tx.get("position_lots", "original:0")
            lot["sellable_date"] = NEXT
            tx.save("position_lots", lot["lot_id"], lot)
        elif change == "reservation":
            reservation = tx.get("reservations", "plan:0")
            reservation["lot_allocations"] = [{"lot_id": "different", "quantity": 1000}]
            tx.save("reservations", "plan:0", reservation)
        else:
            tx.append("lot_movements", "unknown-adjustment", {"lot_id": "original:0", "code": CODE,
                "quantity_delta": 0, "cost_delta_fen": 0, "effective_date": DAY, "kind": "UNEXPLAINED"})
    mutate(book, edit)
    with pytest.raises(LedgerError):
        guard(book, "plan:0")


def test_preopen_share_action_required_but_record_date_registration_is_post_trade(book):
    freeze_fixture(book, ("SELL",))
    def pending(tx):
        tx.append("corporate_action_cases", "case", {"case_id": "case", "code": CODE,
            "record_date": "2026-10-29", "status": "REGISTERED", "event": {
                "ex_date": DAY, "cash_per_share": "0", "bonus_per_share": "0.1",
                "capital_per_share": "0", "share_listing_date": NEXT}, "actions": {}})
        tx.append("corporate_action_cases", "record-today", {"case_id": "record-today", "code": CODE,
            "record_date": DAY, "status": "WAITING_DATA", "event": {"ex_date": NEXT}, "actions": {}})
    mutate(book, pending)
    with pytest.raises(LedgerError):
        guard(book, "plan:0")
    def recognize(tx):
        case = tx.get("corporate_action_cases", "case")
        case["actions"]["share_accrual"] = {"state": "APPLIED", "operation_date": DAY}
        tx.save("corporate_action_cases", "case", case)
        lot = tx.get("position_lots", "original:0")
        lot["cost_fen"] -= 90_909
        tx.save("position_lots", lot["lot_id"], lot)
        tx.append("lot_movements", "cost-transfer", {"lot_id": lot["lot_id"], "code": CODE,
            "quantity_delta": 0, "cost_delta_fen": -90_909, "effective_date": DAY,
            "kind": "CORPORATE_COST_TRANSFER", "reference_id": "case"})
    mutate(book, recognize)
    assert guard(book, "plan:0")["orders"][0]["frozen_lot_allocations"][0]["quantity"] == 1000


def batch_adapter(book, monkeypatch):
    """Use the actual kernel/operations store, replacing only market validation."""
    seen = []
    def apply(account, payload, *, operation_id, expected_revision):
        seen.append(expected_revision)
        def write(tx):
            order = tx.get("orders", payload["order_id"])
            order["status"] = payload["state"]
            tx.save("orders", order["order_id"], order)
            return {"state": "WAITING_DATA" if payload["state"] == "WAITING_DATA" else "APPLIED"}
        return book._mutate(account, operation_id, payload, expected_revision, DAY, write)
    monkeypatch.setattr(book, "_apply_execution", apply)
    return seen


def batch(book, results, *, key="batch", expected=None):
    return apply_execution_batch(book, "LLM", {"results": results}, operation_id=key,
                                 expected_revision=revision(book) if expected is None else expected)


def test_waiting_replay_remains_partial_and_new_evidence_can_finish(book, monkeypatch):
    freeze_fixture(book)
    batch_adapter(book, monkeypatch)
    waiting = [{"order_id": "plan:0", "state": "WAITING_DATA", "evidence": "missing"}]
    first = batch(book, waiting)
    before = book.settings.database.read_bytes()
    second = batch(book, waiting, expected=0)
    assert first["state"] == second["state"] == "PARTIAL"
    assert second["results"][0]["state"] == "ALREADY_APPLIED"
    assert second["order_states"] == ["WAITING_DATA"]
    assert book.settings.database.read_bytes() == before
    complete = batch(book, [{"order_id": "plan:0", "state": "NOT_FILLED", "evidence": "suspended"}])
    assert complete["completion_status"] == "COMPLETE"


def test_batch_resume_never_restores_an_old_receipt_revision(book, monkeypatch):
    freeze_fixture(book, ("BUY", "BUY"))
    seen = batch_adapter(book, monkeypatch)
    first = {"order_id": "plan:0", "state": "NOT_FILLED"}
    assert batch(book, [first])["state"] == "APPLIED"
    mutate(book, lambda tx: tx.cash("interleaved", "DIVIDEND", 1000, DAY, "case"))
    current = revision(book)
    done = batch(book, [deepcopy(first), {"order_id": "plan:1", "state": "NOT_FILLED"}])
    assert done["completion_status"] == "COMPLETE"
    assert seen[-2:] == [current, current]
    assert done["account_revision"] == current + 1


def test_first_new_batch_write_honors_caller_revision(book, monkeypatch):
    freeze_fixture(book)
    batch_adapter(book, monkeypatch)
    before = book.settings.database.read_bytes()
    result = batch(book, [{"order_id": "plan:0", "state": "NOT_FILLED"}], expected=0)
    assert result["state"] == "PARTIAL"
    assert result["results"][0]["state"] == "STALE_ACCOUNT"
    assert book.settings.database.read_bytes() == before


def test_batch_uses_frozen_order_even_when_caller_reverses_results(book, monkeypatch):
    freeze_fixture(book, ("SELL", "BUY"))
    batch_adapter(book, monkeypatch)
    result = batch(book, [{"order_id": "plan:1", "state": "NOT_FILLED"},
                          {"order_id": "plan:0", "state": "NOT_FILLED"}])
    assert result["submission_order"] == ["plan:0", "plan:1"]
    assert result["completion_status"] == "COMPLETE"


def test_unrelated_pending_distribution_does_not_block_a_legal_sale(book):
    freeze_fixture(book, ("SELL",))
    mutate(book, lambda tx: tx.append("corporate_action_cases", "other-case", {
        "case_id": "other-case", "code": "SZ000001", "status": "IDENTITY_PENDING",
        "record_date": "2026-10-29", "actions": {}}))
    assert guard(book, "plan:0")["orders"][0]["can_submit"]


def test_guard_rechecks_state_after_previous_read(book):
    freeze_fixture(book)
    old = book.execution_view("LLM", "plan")
    mutate(book, lambda tx: tx.append("tax_items", "late", {
        "assessed_fen": None, "paid_fen": 0, "status": "WAITING_DATA", "effective_date": DAY}))
    assert old["orders"][0]["can_submit"]
    with pytest.raises(LedgerError):
        guard(book, "plan:0")
    assert old["view_hash"] != book.execution_view("LLM", "plan")["view_hash"]


def test_prior_accounting_watermark_is_required(book):
    freeze_fixture(book)
    def older_start(tx):
        tx.state["simulation_start_session"] = "2026-10-29"
    mutate(book, older_start)
    book.test_now[0] = datetime.fromisoformat("2026-10-30T20:00:00+08:00")
    assert "PRIOR_ACCOUNTING_INCOMPLETE" in issue_kinds(book.execution_view("LLM", "plan"))
    with pytest.raises(LedgerError):
        guard(book, "plan:0")
