"""Pure strategy inputs and results; application owns evidence and ledger writes."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PlanningContext:
    account_id: str
    strategy_id: str
    target_session: str
    market_as_of: str
    input_cutoff: str
    generated_at: str
    account: dict
    policy: dict
    net_assets_fen: int
    securities: dict
    fee_policy: dict
    parent_task: dict | None = None
    initial_evaluated: bool = False
    month_end: bool = False
    task_sessions: tuple[str, ...] = ()
    input_bundle: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyDecision:
    state: str = "READY"
    ordered_intents: tuple[dict, ...] = ()
    no_order_reason: str | None = None
    calculation: dict = field(default_factory=dict)
    targets: tuple[dict, ...] = ()
    task_id: str | None = None
    reasons: tuple[dict, ...] = ()


class Strategy(Protocol):
    def plan(self, context: PlanningContext) -> StrategyDecision: ...
