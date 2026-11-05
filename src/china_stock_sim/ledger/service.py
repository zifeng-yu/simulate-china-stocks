"""One public ledger service. It uses no collector, strategy, model or renderer."""

import sqlite3
from dataclasses import is_dataclass
from functools import wraps

from china_stock_sim.config import load_settings
from china_stock_sim.ledger.account_policies import AccountPoliciesMixin
from china_stock_sim.ledger.corporate import CorporateActionsMixin
from china_stock_sim.ledger.evidence import EvidenceMixin
from china_stock_sim.ledger.kernel import LedgerError, LedgerKernel
from china_stock_sim.ledger.maintenance import MaintenanceMixin
from china_stock_sim.ledger.queries import QueriesMixin
from china_stock_sim.ledger.statement_inputs import StatementInputsMixin
from china_stock_sim.ledger.statements import StatementsMixin
from china_stock_sim.ledger.strategy_decisions import StrategyDecisionsMixin
from china_stock_sim.ledger.trading import TradingMixin
from china_stock_sim.ledger.workflow import WorkflowMixin
from china_stock_sim.market.service import MarketDataService


class LedgerService(StatementInputsMixin, StrategyDecisionsMixin, AccountPoliciesMixin, EvidenceMixin, TradingMixin, CorporateActionsMixin, QueriesMixin,
                    StatementsMixin, WorkflowMixin, MaintenanceMixin, LedgerKernel):
    def __init__(self, settings, *, market=None, clock=None, test_mode=False):
        if not hasattr(settings, 'database'):
            settings = load_settings(settings)
        super().__init__(settings, market=market if market is not None else MarketDataService(settings.root),
                         clock=clock, test_mode=test_mode)


def _write_result(method):
    """Input/evidence errors have the same contract before and within a transaction."""
    @wraps(method)
    def checked(self, *args, **kwargs):
        try:
            def normalize(value):
                if not is_dataclass(value):
                    return value
                normalized = self._payload(value)
                for key in ('operation', 'case_id', 'task_id'):
                    if normalized.get(key) is None:
                        normalized.pop(key, None)
                return normalized
            args = tuple(normalize(value) for value in args)
            kwargs = {key: normalize(value) for key, value in kwargs.items()}
            return method(self, *args, **kwargs)
        except LedgerError as exc:
            return exc.as_dict()
        except (ValueError, KeyError, TypeError) as exc:
            return {'state': 'INVALID_INPUT', 'reason': str(exc)}
        except (OSError, sqlite3.Error) as exc:
            return {'state': 'INTEGRITY_ERROR', 'reason': str(exc)}
    return checked


for _name in ('prepare_strategy_decision', 'save_strategy_decision', 'create_account', 'schedule_account_policy', 'activate_simulation', 'freeze_plan', 'record_no_plan', 'apply_execution_results',
              'register_entitlements', 'recognize_entitlements', 'settle_entitlements', 'record_tax',
              'resolve_tax_holding_rule', 'resolve_event_identity',
              'close_session', 'refresh_statement_inputs', 'save_rebalance_task', 'claim_model_attempt', 'finish_model_attempt',
              'record_step_result', 'apply_correction'):
    setattr(LedgerService, _name, _write_result(getattr(LedgerService, _name)))
