"""业务层共享的接口数据类型；无供应商、数据库、网络及页面依赖。

账本已实现这些输入/输出的持久化与财务校验；策略和撮合通过公共契约接入。
实际选股和撮合判断仍由相应业务层实现。
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, NotRequired, TypedDict

SessionValue = date | str
TimeValue = datetime | str
DecimalValue = Decimal | str


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}须为非空字符串")


def _required_mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name}须为对象")


def _session_value(value, name):
    if isinstance(value, datetime):
        raise ValueError(f"{name}不能包含时分秒")
    parsed = date.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, date) or (isinstance(value, str) and parsed.isoformat() != value):
        raise ValueError(f"{name}须为ISO日期")


def _time_value(value, name):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError(f"{name}须为带时区时间")


def _integer_value(value, name, *, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name}须为合法整数")


def _price_value(value, name):
    if not isinstance(value, (Decimal, str)):
        raise ValueError(f"{name}须为Decimal或十进制字符串")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name}须为有效十进制数") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name}须为有限正数")


@dataclass(frozen=True)
class Position:
    code: str
    total_shares: int
    sellable_shares: int
    cost_fen: int
    reserved_shares: int = 0
    lots: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AccountState:
    account_id: str
    state_id: str
    settled_through: date | None
    cash_fen: int
    reserved_cash_fen: int
    positions: tuple[Position, ...]
    strategy_id: str = ""
    initial_cash_fen: int = 5_000_000
    simulation_start_session: date | None = None
    account_revision: int = 0
    report_revision: int = 0
    last_rendered_revision: int = 0
    target_session: date | None = None
    snapshot_at: datetime | None = None
    valued_through: date | None = None
    reserved_tax_fen: int = 0
    available_cash_fen: int = 0
    cash_receivable_fen: int = 0
    share_receivables: tuple[dict, ...] = ()
    known_tax_payable_fen: int = 0
    unknown_items: tuple[dict, ...] = ()
    valuation_status: str = "NOT_STARTED"
    blocking_reasons: tuple[dict, ...] = ()
    can_plan: bool = False
    active_plan: dict | None = None


@dataclass(frozen=True)
class ExecutionIntent:
    """Strategy input, before quantity conversion and plan validation."""

    code: str
    side: Literal["BUY", "SELL", "HOLD"]
    reason: str
    order_index: int
    limit_price: DecimalValue | None
    quantity: int | None = None
    budget_fen: int | None = None
    sell_fraction: DecimalValue | None = None

    def __post_init__(self):
        _required_text(self.code, "code")
        _required_text(self.reason, "reason")
        if self.side not in {"BUY", "SELL", "HOLD"}:
            raise ValueError("意图方向无效")
        _integer_value(self.order_index, "order_index", minimum=0)
        if self.limit_price is not None:
            _price_value(self.limit_price, "limit_price")
        for name in ("quantity", "budget_fen"):
            if getattr(self, name) is not None:
                _integer_value(getattr(self, name), name, minimum=0)
        if self.sell_fraction is not None:
            _price_value(self.sell_fraction, "sell_fraction")


@dataclass(frozen=True)
class StrategyProposal:
    proposal_id: str
    account_id: str
    strategy_id: str
    strategy_version: str
    target_session: SessionValue
    market_as_of: SessionValue
    generated_at: TimeValue
    input_cutoff: TimeValue
    account_snapshot_id: str
    account_revision: int
    ordered_intents: tuple[ExecutionIntent, ...]
    no_order_reason: str | None = None
    rebalance_task_id: str | None = None
    market_snapshot_ids: tuple[str, ...] = ()
    data_evidence: tuple[dict, ...] = ()
    provenance_mode: Literal["MANUAL", "STRATEGY"] = "MANUAL"
    decision_ref: dict | None = None

    def __post_init__(self):
        for name in ("proposal_id", "account_id", "strategy_id", "strategy_version",
                     "account_snapshot_id"):
            _required_text(getattr(self, name), name)
        for name in ("target_session", "market_as_of"):
            _session_value(getattr(self, name), name)
        for name in ("generated_at", "input_cutoff"):
            _time_value(getattr(self, name), name)
        _integer_value(self.account_revision, "account_revision", minimum=0)


@dataclass(frozen=True)
class ExecutionResult:
    """Explicit wire input: pending nulls and terminal zeroes are distinct."""

    result_id: str
    order_id: str
    plan_id: str
    account_id: str
    code: str
    side: Literal["BUY", "SELL"]
    target_session: SessionValue
    state: Literal["SIMULATED_FILL", "NOT_FILLED", "WAITING_DATA"]
    execution_model: str
    fee_policy_hash: str
    rule_policy_hash: str
    ruleset_hash: str
    computed_at: TimeValue
    knowledge_cutoff: TimeValue
    account_view_hash: str
    account_view_evidence: dict
    account_revision: int
    reason_code: str
    reason_details: dict
    reason_evidence: dict
    execution_context: dict | None
    market_snapshot_ids: tuple[str, ...]
    data_evidence: tuple[dict, ...]
    quantity: int | None
    price: DecimalValue | None
    amount_fen: int | None
    commission_fen: int | None
    stamp_fen: int | None
    transfer_fen: int | None
    fees_fen: int | None
    net_cash_fen: int | None
    reason: str = ""

    def __post_init__(self):
        for name in ("result_id", "order_id", "plan_id", "account_id", "code",
                     "execution_model", "fee_policy_hash", "rule_policy_hash", "ruleset_hash",
                     "account_view_hash", "reason_code"):
            _required_text(getattr(self, name), name)
        _session_value(self.target_session, "target_session")
        _time_value(self.computed_at, "computed_at")
        _time_value(self.knowledge_cutoff, "knowledge_cutoff")
        _integer_value(self.account_revision, "account_revision", minimum=0)
        for name in ("account_view_evidence", "reason_details", "reason_evidence"):
            _required_mapping(getattr(self, name), name)
        if self.execution_context is not None:
            _required_mapping(self.execution_context, "execution_context")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("执行方向无效")
        amounts = ("amount_fen", "commission_fen", "stamp_fen", "transfer_fen", "fees_fen",
                   "net_cash_fen")
        if self.state == "WAITING_DATA":
            if any(getattr(self, name) is not None for name in ("quantity", "price", *amounts)):
                raise ValueError("待数据结果数量、价格与金额须明确为null")
        elif self.state == "NOT_FILLED":
            if self.price is not None:
                raise ValueError("未成交结果价格须为null")
            for name in ("quantity", *amounts):
                _integer_value(getattr(self, name), name)
                if getattr(self, name) != 0:
                    raise ValueError("未成交结果数量与金额须为0")
        elif self.state == "SIMULATED_FILL":
            _integer_value(self.quantity, "quantity", minimum=1)
            _price_value(self.price, "price")
            for name in amounts:
                _integer_value(getattr(self, name), name,
                               minimum=None if name == "net_cash_fen" else 0)
        else:
            raise ValueError("执行结果状态无效")


@dataclass(frozen=True)
class FrozenOrder:
    order_id: str
    code: str
    side: Literal["BUY", "SELL"]
    quantity: int
    limit_price: Decimal
    reason: str
    reserved_cash_fen: int | None = None
    reserved_shares: int | None = None
    order_index: int = 0


@dataclass(frozen=True)
class PlanEnvelope:
    plan_id: str
    account_id: str
    strategy_id: str
    strategy_version: str
    target_session: date
    market_as_of: date
    input_cutoff: datetime
    generated_at: datetime
    account_revision: int
    market_snapshot_ids: tuple[str, ...]
    orders: tuple[FrozenOrder, ...]
    fee_policy: dict
    rule_policy: dict
    account_snapshot: dict
    account_snapshot_id: str
    account_snapshot_hash: str
    ruleset: dict
    no_order_reason: str | None = None
    task_id: str | None = None
    data_evidence: tuple[dict, ...] = ()
    validation_report: dict = field(default_factory=dict)
    skipped_intents: tuple[dict, ...] = ()
    target_pool_evidence: dict = field(default_factory=dict)
    calendar_evidence: tuple[dict, ...] = ()
    provenance_mode: Literal["MANUAL", "STRATEGY"] = "MANUAL"
    decision_ref: dict | None = None
    strategy_market_snapshot_ids: tuple[str, ...] = ()
    strategy_data_evidence: tuple[dict, ...] = ()

    def __post_init__(self):
        for name in ("account_snapshot_id", "account_snapshot_hash"):
            _required_text(getattr(self, name), name)
        for name in ("account_snapshot", "ruleset"):
            _required_mapping(getattr(self, name), name)


class FrozenPlan(TypedDict):
    """The existing ledger get_plan dictionary, including immutable source evidence."""

    plan_id: str
    account_id: str
    strategy_id: str
    strategy_version: str
    target_session: str
    market_as_of: str
    input_cutoff: str
    generated_at: str
    frozen_at: str
    plan_hash: str
    account_revision: int
    account_revision_at_freeze: int
    account_snapshot: dict
    account_snapshot_id: str
    account_snapshot_hash: str
    security_facts: dict
    event_facts: dict
    fee_policy: dict
    rule_policy: dict
    ruleset: dict
    fee_policy_hash: str
    rule_policy_hash: str
    ruleset_hash: str
    buy_budget_fen: int
    order_ids: list[str]
    orders: list[dict]
    reservations: list[dict]
    original_resources: dict
    status: str
    document: dict
    market_snapshot_ids: NotRequired[list[str]]
    data_evidence: NotRequired[list[dict]]
    no_order_reason: NotRequired[str | None]
    task_id: NotRequired[str | None]
    validation_report: NotRequired[dict]
    skipped_intents: NotRequired[list[dict]]
    target_pool_evidence: NotRequired[dict]
    calendar_evidence: NotRequired[list[dict]]
    provenance_mode: NotRequired[str]
    decision_ref: NotRequired[dict | None]
    strategy_market_snapshot_ids: NotRequired[list[str]]
    strategy_data_evidence: NotRequired[list[dict]]


class ExecutionAccountView(TypedDict):
    """Exactly the ledger execution_view dictionary, not a planning AccountState."""

    account_id: str
    plan_id: str
    target_session: str
    frozen_plan: FrozenPlan
    original_resources: dict
    commit_guards: dict
    orders: list[dict]
    stages: dict
    account_revision: int
    plan_hash: str
    issues: list[dict]
    state: Literal["READY", "WAITING_DATA"]
    view_hash: str
    snapshot_at: str


class MarketQueryEvidence(TypedDict):
    method: str
    kwargs: dict
    result: dict


class ExecutionMarketBundle(TypedDict):
    target_session: str
    knowledge_cutoff: str
    by_code: dict[str, list[MarketQueryEvidence]]
    execution_contexts: dict[str, dict]
    market_snapshot_ids: list[str]
    observation_ids: list[str]
    bundle_hash: str


class PlanValidationResult(TypedDict):
    status: str
    validation_id: str
    proposal_hash: str
    account_snapshot_id: str
    account_snapshot_hash: str
    account_revision: int
    market_as_of: str
    target_session: str
    input_cutoff: str
    accepted_orders: list[dict]
    skipped_intents: list[dict]
    issues: list[dict]
    required_inputs: list[dict]
    policy_bundle: dict
    policy_bundle_hash: str
    market_evidence: list[MarketQueryEvidence]
    plan_envelope: PlanEnvelope | None
    provenance_mode: str
    decision_ref: dict | None
    source_proposal: dict
    strategy_market_snapshot_ids: list[str]
    strategy_data_evidence: list[dict]


class ExecutionBatchResult(TypedDict):
    """Ledger's existing per-item submission response; status follows real orders."""

    state: str
    completion_status: Literal["COMPLETE", "PARTIAL", "ERROR"]
    results: list[dict]
    order_states: list[str | None]
    account_revision: int


@dataclass(frozen=True)
class CorporateActionInput:
    code: str
    effective_date: date
    knowledge_cutoff: datetime
    event_ids: tuple[str, ...] = ()
    case_id: str | None = None
    operation: str | None = None
    market_snapshot_ids: tuple[str, ...] = ()
    data_evidence: tuple[dict, ...] = ()


class DailyStatement(TypedDict):
    account_id: str
    session: str
    statement_revision: int
    accounting_status: str
    valuation_status: str
    plan_status: str
    account_revision: int
    report_status: str
    opening_cash_fen: int | None
    closing_cash_fen: int
    cash_changes: dict
    cash_entries: list[dict]
    positions: list[dict]
    cash_receivable_fen: int
    share_receivable_value_fen: int | None
    market_value_fen: int | None
    tax_payable_fen: int
    cumulative_tax_expense_fen: int
    known_assets_fen: int
    provisional_assets_fen: int | None
    net_assets_fen: int | None
    cumulative_pnl_fen: int | None
    daily_pnl_fen: int | None
    net_value: str | None
    realized_pnl_fen: int
    unrealized_pnl_fen: int | None
    cumulative_dividend_income_fen: int
    correction_pnl_fen: int
    pnl_decomposition_fen: int | None
    cumulative_transaction_fees_fen: int
    orders: list[dict]
    fills: list[dict]
    issues: list[dict]
    knowledge_cutoff: str
    snapshot_ids: list[str]
    data_evidence: list[dict]
    processed_at: str
    supersedes_revision: int | None


class ReportSnapshot(TypedDict):
    account: dict
    account_revision: int
    report_revision: int
    last_rendered_revision: int
    snapshot_at: str
    target_session: str
    statements: list[DailyStatement]
    orders: list[dict]
    fills: list[dict]
    cash_entries: list[dict]
    lots: list[dict]
    cases: list[dict]
    entitlements: list[dict]
    tax_items: list[dict]
    plans: list[dict]
    steps: list[dict]
    model_attempts: list[dict]
    rebalance_tasks: list[dict]
