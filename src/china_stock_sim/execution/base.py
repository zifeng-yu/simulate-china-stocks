"""成交模型可替换接口：纯业务结果交账本提交，不调用模型或数据源。"""

from datetime import datetime
from typing import Protocol

from china_stock_sim.domain import (
    ExecutionAccountView,
    ExecutionMarketBundle,
    ExecutionResult,
    FrozenPlan,
    MarketQueryEvidence,
    PlanValidationResult,
    StrategyProposal,
)


class PlanValidator(Protocol):
    def validate_plan(
        self, proposal: StrategyProposal | dict, account_snapshot: dict,
        market_evidence: tuple[MarketQueryEvidence, ...], policy_bundle: dict,
        *, now: datetime, decision_core: dict | None = None,
    ) -> PlanValidationResult: ...


class ExecutionModel(Protocol):
    def match(
        self,
        plan: FrozenPlan,
        account: ExecutionAccountView,
        market: ExecutionMarketBundle,
    ) -> tuple[ExecutionResult, ...]: ...
