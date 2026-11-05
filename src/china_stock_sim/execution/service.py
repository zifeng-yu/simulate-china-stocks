"""Replaceable pure execution components. No service or storage construction."""

from china_stock_sim.execution.matching import DailyOpenMatcher
from china_stock_sim.execution.validation import DefaultPlanValidator


class ExecutionEngine:
    def __init__(self, *, validator=None, matcher=None, clock=None):
        self.validator = validator if validator is not None else DefaultPlanValidator()
        self.matcher = matcher if matcher is not None else DailyOpenMatcher(clock=clock)

    def validate_plan(self, proposal, account_snapshot, market_evidence, policy_bundle, *, now,
                      decision_core=None):
        return self.validator.validate_plan(proposal, account_snapshot, market_evidence,
                                            policy_bundle, now=now, decision_core=decision_core)

    def match(self, plan, account, market):
        return self.matcher.match(plan, account, market)
