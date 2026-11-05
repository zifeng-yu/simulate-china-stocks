"""Pre-open validation: real ledger freeze plus rule and causal boundary regressions."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from test_ledger_trading import CODE, DAY, book, call

from china_stock_sim.common.policies import load_policy_bundle
from china_stock_sim.domain import ExecutionIntent, StrategyProposal
from china_stock_sim.execution.validation import DefaultPlanValidator
from china_stock_sim.market.contracts import DataResult

assert book


@pytest.fixture
def inputs(book):
    start = date(2026, 1, 1)
    book.market.calendar_rows = [
        {
            "date": (start + timedelta(days=i)).isoformat(),
            "is_trading_day": str(int((start + timedelta(days=i)).weekday() < 5)),
        }
        for i in range(365)
    ]
    codes = ["SH600000", "SH600001", "SH600002", "SH600003", "SH600004", "SH600005", "SZ000001"]
    book.market.pool = lambda as_of, **kwargs: DataResult(
        "READY",
        {"effective_from": "2026-10-29", "selected": [{"code": code} for code in codes]},
        (),
        ("pool-proof",),
    )
    account = book.account_state("LLM", DAY)
    cutoff = book._now().isoformat()
    p = {
        "proposal_id": "idea",
        "account_id": "LLM",
        "strategy_id": "LLM-Daily",
        "strategy_version": "fixed",
        "target_session": DAY,
        "market_as_of": "2026-10-29",
        "input_cutoff": cutoff,
        "generated_at": cutoff,
        "account_revision": account["account_revision"],
        "account_snapshot_id": account["state_id"],
        "ordered_intents": [
            {
                "code": CODE,
                "side": "BUY",
                "order_index": 0,
                "reason": "test",
                "limit_price": "10.00",
                "budget_fen": 1_000_510,
            }
        ],
    }
    evidence = []

    def add(method, **kwargs):
        kwargs["knowledge_cutoff"] = cutoff
        evidence.append(
            {
                "method": method,
                "kwargs": kwargs,
                "result": getattr(book.market, method)(**kwargs).as_dict(),
            }
        )

    add("calendar", start=DAY, end=DAY)
    add("calendar_operation", operation="previous_session", day=DAY)
    add("calendar_operation", operation="window", day=p["market_as_of"], size=121)
    add("pool", as_of=DAY)
    for code in codes:
        add("prices", code=code, start=p["market_as_of"], end=p["market_as_of"])
        add("security", code=code)
        add("events", code=code)
        add("event_coverage", code=code, start=p["market_as_of"], end=p["market_as_of"])
    bundle = load_policy_bundle(
        book.root / "configs/execution_rules.toml",
        target_session=DAY,
        knowledge_cutoff=cutoff,
        execution_policy=book.settings.raw["execution"],
        fee_policy=book.settings.raw["fees"],
    )
    return {
        "p": p,
        "account": account,
        "evidence": evidence,
        "bundle": bundle,
        "now": book._now(),
        "book": book,
    }


def run(data):
    return DefaultPlanValidator().validate_plan(
        data["p"], data["account"], data["evidence"], data["bundle"], now=data["now"]
    )


def query(data, method, code=None):
    return next(
        q
        for q in data["evidence"]
        if q["method"] == method and (code is None or q["kwargs"].get("code") == code)
    )


def sell(data, available=1150, quantity=None, fraction=None):
    data["account"]["positions"] = [
        {
            "code": CODE,
            "total_shares": available,
            "sellable_shares": available,
            "reserved_shares": 0,
            "cost_fen": 10000,
            "lots": [],
        }
    ]
    i = data["p"]["ordered_intents"][0]
    i.pop("budget_fen")
    i.update(side="SELL")
    if quantity is not None:
        i["quantity"] = quantity
    else:
        i["sell_fraction"] = fraction


def test_public_validation_to_real_freeze_is_nonempty_and_read_only(inputs):
    book = inputs["book"]
    before = book.settings.database.read_bytes()
    first = run(inputs)
    assert first["status"] == "VALID", first
    assert first["accepted_orders"][0]["quantity"] == 1000
    assert first["accepted_orders"][0]["reserved_cash_fen"] == 1_000_510
    assert book.settings.database.read_bytes() == before
    outcome = call(book, "freeze_plan", first["plan_envelope"])
    assert outcome["state"] == "APPLIED", outcome
    assert book.get_plan("LLM", outcome["plan_id"])["orders"][0]["quantity"] == 1000
    assert book.account_state("LLM", DAY)["reserved_cash_fen"] == 1_000_510


def test_dictionary_and_dataclass_inputs_are_identical(inputs):
    expected = run(inputs)
    fields = deepcopy(inputs["p"])
    fields["ordered_intents"] = tuple(ExecutionIntent(**i) for i in fields["ordered_intents"])
    inputs["p"] = StrategyProposal(**fields)
    # Optional dataclass defaults must be semantically equivalent to absent dict fields.
    other = run(inputs)
    assert other == expected


@pytest.mark.parametrize(
    "budget,quantity", [(100500, 0), (100501, 100), (1000509, 900), (1000510, 1000), (0, 0)]
)
def test_budget_includes_commission_and_fen_boundaries(inputs, budget, quantity):
    inputs["p"]["ordered_intents"][0]["budget_fen"] = budget
    r = run(inputs)
    assert r["status"] == "VALID", r
    assert (r["accepted_orders"][0]["quantity"] if r["accepted_orders"] else 0) == quantity
    if quantity == 0:
        assert r["skipped_intents"][0]["reason_code"] == "BELOW_ONE_LOT"
        assert r["plan_envelope"].no_order_reason


@pytest.mark.parametrize(
    "budget,quantity,amount,commission,transfer,reservation",
    [
        (1_600_515, 1500, 1_500_000, 500, 15, 1_500_515),
        (1_600_516, 1600, 1_600_000, 500, 16, 1_600_516),
        (1_700_526, 1600, 1_600_000, 500, 16, 1_600_516),
        (1_700_527, 1700, 1_700_000, 510, 17, 1_700_527),
        (1_800_557, 1700, 1_700_000, 510, 17, 1_700_527),
        (1_800_558, 1800, 1_800_000, 540, 18, 1_800_558),
    ],
)
def test_budget_crosses_minimum_commission_rate_switch(
    inputs, budget, quantity, amount, commission, transfer, reservation
):
    # At 10 yuan, 1600 shares still pay the 5-yuan minimum; 1700 pay 5.10.
    # Each pair straddles the next legal 100-share lot by exactly one fen.
    inputs["p"]["ordered_intents"][0]["budget_fen"] = budget
    result = run(inputs)
    assert result["status"] == "VALID", result
    order = result["accepted_orders"][0]
    assert order["quantity"] == quantity
    assert order["reserved_cash_fen"] == reservation == amount + commission + transfer
    assert order["reserved_cash_fen"] <= budget
    frozen = result["plan_envelope"].orders[0]
    assert frozen.quantity == quantity and frozen.reserved_cash_fen == reservation


@pytest.mark.parametrize(
    "price,amount,commission,reservation",
    [
        ("166.66", 1_666_600, 500, 1_667_117),
        ("166.67", 1_666_700, 500, 1_667_217),
        ("166.83", 1_668_300, 500, 1_668_817),
        ("166.84", 1_668_400, 501, 1_668_918),
    ],
)
def test_budget_commission_switch_rounds_each_fee_before_lot_sizing(
    inputs, price, amount, commission, reservation
):
    # A notional just above 16666.67 yuan still rounds commission to 500 fen.
    # The first tested whole-lot notional yielding 501 fen is 16684 yuan.
    intent = inputs["p"]["ordered_intents"][0]
    intent.update(limit_price=price, budget_fen=reservation - 1)
    insufficient = run(inputs)
    assert insufficient["status"] == "VALID", insufficient
    assert insufficient["accepted_orders"] == []
    assert insufficient["skipped_intents"][0]["reason_code"] == "BELOW_ONE_LOT"
    intent["budget_fen"] = reservation
    exact = run(inputs)
    assert exact["status"] == "VALID", exact
    order = exact["accepted_orders"][0]
    assert order["quantity"] == 100
    assert order["reserved_cash_fen"] == amount + commission + 17 == reservation


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_fen", True),
        ("budget_fen", 100.0),
        ("budget_fen", -1),
        ("limit_price", 10.0),
        ("limit_price", 10),
        ("limit_price", "NaN"),
        ("limit_price", "Infinity"),
        ("limit_price", "-1"),
        ("limit_price", "0"),
        ("limit_price", "10.001"),
        ("order_index", True),
        ("order_index", -1),
    ],
)
def test_invalid_numeric_inputs_never_produce_plan(inputs, field, value):
    inputs["p"]["ordered_intents"][0][field] = value
    r = run(inputs)
    assert r["status"] == "INVALID_INPUT", r
    assert r["plan_envelope"] is None


@pytest.mark.parametrize("quantity", [1, 99, 101, 1_000_100])
def test_explicit_illegal_buy_quantity_is_not_adjusted(inputs, quantity):
    i = inputs["p"]["ordered_intents"][0]
    i.pop("budget_fen")
    i["quantity"] = quantity
    assert run(inputs)["status"] == "INVALID_PLAN"


@pytest.mark.parametrize(
    "available,quantity,valid",
    [
        (1150, 100, True),
        (1150, 150, True),
        (1150, 1150, True),
        (1150, 125, False),
        (50, 50, True),
        (50, 25, False),
        (0, 100, False),
        (100, 200, False),
        (1_000_100, 1_000_100, False),
    ],
)
def test_explicit_sell_lots_and_t_plus_one_available_quantity(inputs, available, quantity, valid):
    sell(inputs, available, quantity=quantity)
    assert run(inputs)["status"] == ("VALID" if valid else "INVALID_PLAN")


@pytest.mark.parametrize(
    "available,fraction,quantity",
    [
        (1150, "1", 1150),
        (1150, "0.5", 550),
        (50, "0.5", 0),
        (50, "1", 50),
        (150, "0.5", 50),
        (100, "0.01", 0),
    ],
)
def test_sell_fraction_legal_rounding_and_trace(inputs, available, fraction, quantity):
    sell(inputs, available, fraction=fraction)
    r = run(inputs)
    assert r["status"] == "VALID", r
    assert (r["accepted_orders"][0]["quantity"] if r["accepted_orders"] else 0) == quantity
    sizing = (r["accepted_orders"] or r["skipped_intents"])[0]["sizing"]
    assert sizing["sellable_shares"] == available
    assert sizing["unrounded_quantity"] == str(Decimal(available) * Decimal(fraction))


@pytest.mark.parametrize("fraction", ["0", "-0.1", "1.1", "NaN", 0.5, True])
def test_invalid_sell_fraction(inputs, fraction):
    sell(inputs, fraction=fraction)
    assert run(inputs)["status"] == "INVALID_INPUT"


def test_missing_held_security_is_invalid_not_skipped(inputs):
    i = inputs["p"]["ordered_intents"][0]
    i.pop("budget_fen")
    i.update(side="SELL", sell_fraction="1")
    assert run(inputs)["status"] == "INVALID_PLAN"


def test_aggregate_budget_not_silently_scaled(inputs):
    base = inputs["p"]["ordered_intents"][0]
    base["budget_fen"] = 3_000_000
    inputs["p"]["ordered_intents"].append({**base, "code": "SH600001", "order_index": 1})
    r = run(inputs)
    assert r["status"] == "INVALID_PLAN" and r["plan_envelope"] is None


def test_sell_first_does_not_spend_same_open_sale_proceeds(inputs):
    buy = deepcopy(inputs["p"]["ordered_intents"][0])
    buy["code"] = "SH600001"
    sell(inputs, quantity=100)
    inputs["p"]["ordered_intents"][0]["order_index"] = 1
    inputs["p"]["ordered_intents"].insert(0, buy)
    r = run(inputs)
    assert r["status"] == "VALID"
    assert [o["side"] for o in r["accepted_orders"]] == ["SELL", "BUY"]
    inputs["account"]["available_cash_fen"] = 0
    assert run(inputs)["status"] == "INVALID_PLAN"


@pytest.mark.parametrize(
    "change", ["duplicate_code", "duplicate_index", "six_codes", "contradictory", "missing_reason"]
)
def test_structural_plan_rejection_before_skip(inputs, change):
    i = inputs["p"]["ordered_intents"][0]
    if change == "duplicate_code":
        inputs["p"]["ordered_intents"].append({**i, "order_index": 1})
    elif change == "duplicate_index":
        inputs["p"]["ordered_intents"].append({**i, "code": "SH600001"})
    elif change == "six_codes":
        inputs["p"]["ordered_intents"] = [
            {**i, "code": f"SH60000{n}", "order_index": n} for n in range(6)
        ]
        query(inputs, "security", CODE)["result"]["data"][0]["board"] = "UNSUPPORTED"
    elif change == "contradictory":
        i["quantity"] = 100
    else:
        i["reason"] = ""
    assert run(inputs)["status"] in {"INVALID_PLAN", "INVALID_INPUT"}


@pytest.mark.parametrize(
    "change,reason",
    [
        ("st", "ST"),
        ("suspended", "SUSPENDED"),
        ("outside_pool", "OUTSIDE_POOL"),
        ("board", "NOT_MAIN_BOARD"),
        ("ipo", "NEW_LISTING"),
        ("delisted", "SECURITY_NOT_TRADABLE"),
        ("delisting", "DELISTING"),
        ("complex", "KNOWN_COMPLEX_ACTION"),
    ],
)
def test_known_buy_exclusions_are_explained_not_unknown(inputs, change, reason):
    sec = query(inputs, "security", CODE)["result"]["data"][0]
    bar = query(inputs, "prices", CODE)["result"]["data"][0]
    if change == "st":
        bar["isST"] = "1"
    elif change == "suspended":
        bar["tradestatus"] = 0
    elif change == "outside_pool":
        query(inputs, "pool")["result"]["data"]["selected"] = []
    elif change == "board":
        sec["board"] = "CHINEXT"
    elif change == "ipo":
        sec["listed"] = "2026-10-01"
    elif change == "delisted":
        sec["delisted"] = DAY
    elif change == "delisting":
        sec["delisting_start_date"] = "2026-10-01"
    else:
        query(inputs, "events", CODE)["result"]["data"] = [
            {"code": CODE, "kind": "COMPLEX", "status": "IMPLEMENTED", "ex_date": "2026-11-10"}
        ]
    r = run(inputs)
    assert r["status"] == "VALID" and not r["accepted_orders"], r
    assert r["skipped_intents"][0]["reason_code"] == reason


def test_listing_exact_window_boundary(inputs):
    window = next(q for q in inputs["evidence"] if q["kwargs"].get("operation") == "window")[
        "result"
    ]["data"]
    query(inputs, "security", CODE)["result"]["data"][0]["listed"] = window[0]
    assert run(inputs)["status"] == "VALID" and run(inputs)["accepted_orders"]


def test_sell_does_not_require_pool_non_st_or_long_history(inputs):
    sell(inputs, quantity=100)
    query(inputs, "prices", CODE)["result"]["data"][0]["isST"] = 1
    inputs["evidence"] = [
        q
        for q in inputs["evidence"]
        if q["method"] not in {"pool", "events", "event_coverage"}
        and q["kwargs"].get("operation") != "window"
    ]
    assert run(inputs)["status"] == "VALID"


@pytest.mark.parametrize("method", ["prices", "security", "pool", "event_coverage"])
def test_missing_required_fact_blocks_entire_proposal(inputs, method):
    query(inputs, method)["result"]["state"] = "MISSING"
    r = run(inputs)
    assert r["status"] == "WAITING_VALIDATION" and r["plan_envelope"] is None
    assert r["required_inputs"]


def test_one_pending_intent_is_not_silently_dropped(inputs):
    i = inputs["p"]["ordered_intents"][0]
    inputs["p"]["ordered_intents"].append({**i, "code": "SH600001", "order_index": 1})
    query(inputs, "prices", "SH600001")["result"]["state"] = "MISSING"
    r = run(inputs)
    assert r["status"] == "WAITING_VALIDATION"
    assert len(r["accepted_orders"]) == 1 and r["plan_envelope"] is None


def test_old_cash_event_conflict_is_irrelevant_but_undated_conflict_is_not(inputs):
    q = query(inputs, "events", CODE)
    q["result"].update(
        state="CONFLICT",
        data=[
            {
                "code": CODE,
                "kind": "DISTRIBUTION",
                "status": "IMPLEMENTED",
                "ex_date": "2010-01-01",
                "conflicts": ["CONFLICT_CASH"],
            }
        ],
    )
    assert run(inputs)["status"] == "VALID"
    q["result"]["data"][0]["ex_date"] = None
    assert run(inputs)["status"] == "WAITING_VALIDATION"


@pytest.mark.parametrize("method", ["prices", "security", "pool", "events", "event_coverage"])
def test_used_corrupt_proof_is_integrity_error(inputs, method):
    query(inputs, method)["result"]["state"] = "CORRUPT"
    assert run(inputs)["status"] == "INTEGRITY_ERROR"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("cutoff", "INVALID_PLAN"),
        ("future_clock", "INVALID_INPUT"),
        ("future_data", "INVALID_INPUT"),
        ("before_close", "INVALID_INPUT"),
        ("wrong_previous", "INVALID_PLAN"),
        ("stale_revision", "STALE_ACCOUNT"),
        ("wrong_account", "INVALID_INPUT"),
        ("policy_tamper", "INTEGRITY_ERROR"),
    ],
)
def test_time_and_identity_boundaries(inputs, change, expected):
    if change == "cutoff":
        inputs["now"] = datetime.fromisoformat(DAY + "T09:00:00+08:00")
    elif change == "future_clock":
        inputs["p"]["generated_at"] = DAY + "T08:00:00+08:00"
    elif change == "future_data":
        query(inputs, "prices", CODE)["kwargs"]["end"] = DAY
    elif change == "before_close":
        inputs["p"]["input_cutoff"] = "2026-10-29T18:09:59+08:00"
    elif change == "wrong_previous":
        query(inputs, "calendar_operation")["result"]["data"] = "2026-10-28"
    elif change == "stale_revision":
        inputs["p"]["account_revision"] += 1
    elif change == "wrong_account":
        inputs["p"]["account_id"] = "S1"
    else:
        inputs["bundle"]["fee_policy"]["commission_rate"] = "0"
    assert run(inputs)["status"] == expected


def test_empty_hold_and_skipped_plan_have_reasons_and_freeze(inputs):
    inputs["p"]["ordered_intents"] = []
    assert run(inputs)["status"] == "INVALID_PLAN"
    inputs["p"]["no_order_reason"] = "NO_SIGNAL"
    r = run(inputs)
    assert r["status"] == "VALID" and r["plan_envelope"].orders == ()
    assert call(inputs["book"], "freeze_plan", r["plan_envelope"])["state"] == "APPLIED"


def test_input_not_mutated_and_identity_deterministic(inputs):
    before = deepcopy({k: v for k, v in inputs.items() if k != "book"})
    a, b = run(inputs), run(inputs)
    assert a == b
    assert before == {k: v for k, v in inputs.items() if k != "book"}


def test_parent_task_id_mapped_without_claiming_freeze_success(inputs):
    inputs["p"]["rebalance_task_id"] = "monthly-task"
    r = run(inputs)
    assert r["plan_envelope"].task_id == "monthly-task"
    receipt = call(inputs["book"], "freeze_plan", r["plan_envelope"])
    assert receipt["state"] == "INVALID_INPUT"  # final task existence/quantity is transactional


def test_reason_credentials_never_echoed(inputs):
    inputs["p"]["ordered_intents"][0]["reason"] = "password=not-a-real-secret"
    r = run(inputs)
    assert r["status"] == "INVALID_INPUT"
    assert "not-a-real-secret" not in str(r)


@pytest.mark.parametrize(
    "change,expected",
    [
        ("nontrading", "INVALID_PLAN"),
        ("missing", "WAITING_VALIDATION"),
        ("future_fact", "INVALID_INPUT"),
        ("status", "WAITING_VALIDATION"),
    ],
)
def test_explicit_target_calendar_and_knowledge_proofs(inputs, change, expected):
    if change == "nontrading":
        query(inputs, "calendar")["result"]["data"][0]["is_trading_day"] = "0"
    elif change == "missing":
        inputs["evidence"] = [q for q in inputs["evidence"] if q["method"] != "calendar"]
    elif change == "future_fact":
        query(inputs, "prices", CODE)["result"]["data"][0]["_provenance"] = {
            "available_at": "2026-10-30T10:00:00+08:00"
        }
    else:
        query(inputs, "security", CODE)["result"]["data"][0]["source_status"] = None
    assert run(inputs)["status"] == expected


def test_five_codes_accepted_and_hold_not_counted(inputs):
    i = inputs["p"]["ordered_intents"][0]
    inputs["p"]["ordered_intents"] = [
        {**i, "code": f"SH60000{n}", "budget_fen": 100501, "order_index": n} for n in range(5)
    ]
    inputs["p"]["ordered_intents"].append(
        {
            "code": "SZ000001",
            "side": "HOLD",
            "order_index": 5,
            "reason": "keep",
            "limit_price": None,
        }
    )
    r = run(inputs)
    assert r["status"] == "VALID" and len(r["accepted_orders"]) == 5


def test_unavailable_account_does_not_create_partial_plan(inputs):
    inputs["account"]["can_plan"] = False
    inputs["account"]["blocking_reasons"] = [{"kind": "PRIOR_ACCOUNTING_INCOMPLETE"}]
    r = run(inputs)
    assert r["status"] == "WAITING_VALIDATION" and r["plan_envelope"] is None


def test_explicit_invalid_quantity_is_rejected_even_when_st(inputs):
    i = inputs["p"]["ordered_intents"][0]
    i.pop("budget_fen")
    i["quantity"] = 125
    query(inputs, "prices", CODE)["result"]["data"][0]["isST"] = 1
    assert run(inputs)["status"] == "INVALID_PLAN"


@pytest.mark.parametrize("indices", [[1], [1, 0], [0, 2]])
def test_order_index_cannot_reorder_or_disagree_with_input_sequence(inputs, indices):
    i = inputs["p"]["ordered_intents"][0]
    inputs["p"]["ordered_intents"] = [
        {**i, "code": f"SH60000{n}", "order_index": index} for n, index in enumerate(indices)
    ]
    assert run(inputs)["status"] == "INVALID_PLAN"


def test_public_normalization_matches_validator_replay_identity(inputs):
    from china_stock_sim.common.policies import policy_digest
    from china_stock_sim.execution.validation import normalize_proposal

    original = deepcopy(inputs["p"])
    normalized = normalize_proposal(inputs["p"])
    assert policy_digest(normalized) == run(inputs)["proposal_hash"]
    assert inputs["p"] == original
    normalized["no_order_reason"] = "other"
    assert inputs["p"] == original
