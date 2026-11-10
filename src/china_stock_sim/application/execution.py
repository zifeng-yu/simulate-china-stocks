"""Compose public services; only freeze/settle authorize ledger writes.

This is the execution part of a future daily workflow, not a strategy or timer.
Corporate actions and session close remain explicit upstream/downstream steps.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from china_stock_sim.application.execution_controls import execution_blocks
from china_stock_sim.common.execution_views import waiting_identity
from china_stock_sim.common.fees import ExecutionSupportError, aware_time
from china_stock_sim.common.policies import load_policy_bundle, policy_digest
from china_stock_sim.config import load_settings, local_path
from china_stock_sim.execution.evidence import collect_execution_market, collect_planning_market
from china_stock_sim.execution.service import ExecutionEngine
from china_stock_sim.execution.utils import reject_credentials, to_wire
from china_stock_sim.execution.validation import normalize_proposal
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.decision_evidence import (
    read_business_document,
    read_decision_core,
    verify_plan_decision,
)
from china_stock_sim.ledger.kernel import LedgerError
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.runtime.recording import stage

TERMINAL = {'FILLED', 'NOT_FILLED'}


def _error(exc):
    from china_stock_sim.runtime.recording import record_exception
    record_exception(exc, component='execution')
    if isinstance(exc, (ExecutionSupportError, LedgerError)):
        return {'state': getattr(exc, 'code', getattr(exc, 'state', 'ERROR')), 'reason': str(exc),
                'details': getattr(exc, 'details', {})}
    if isinstance(exc, (OSError, sqlite3.Error)):
        return {'state': 'IO_ERROR', 'reason': '本地读取或保存失败', 'error_type': type(exc).__name__}
    return {'state': 'INVALID_INPUT', 'reason': '执行输入结构或字段无效',
            'error_type': type(exc).__name__}


def _waiting_identity(value):
    """An unchanged waiting decision needs no second financial mutation."""
    return waiting_identity(value)


class ExecutionApplication:
    def __init__(self, settings, *, market=None, ledger=None, clock=None, test_mode=False,
                 engine=None):
        self.settings = settings if hasattr(settings, 'database') else load_settings(settings)
        self.root = self.settings.root
        actual = Path(__file__).resolve().parents[3]
        if clock is not None and (not test_mode or self.settings.database.resolve() ==
                                  actual / 'data/state/ledger.sqlite3'):
            raise ValueError('受控时钟仅允许隔离测试项目')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.market = market if market is not None else MarketDataService(self.root)
        self.ledger = ledger if ledger is not None else LedgerService(
            self.settings, market=self.market, clock=clock, test_mode=test_mode)
        self.engine = engine if engine is not None else ExecutionEngine(clock=self.clock)

    def _now(self):
        return aware_time(self.clock())

    def validate(self, proposal):
        try:
            reject_credentials(proposal)
            proposal = normalize_proposal(proposal)
            core = (read_decision_core(self.root, proposal['decision_ref'])
                    if proposal.get('decision_ref') is not None else None)
            account = self.ledger.account_state(proposal['account_id'], proposal['target_session'])
            policy = load_policy_bundle(self.root / 'configs/execution_rules.toml',
                target_session=proposal['target_session'], knowledge_cutoff=proposal['input_cutoff'],
                execution_policy=self.settings.raw['execution'], fee_policy=self.settings.raw['fees'])
            evidence = collect_planning_market(self.market, proposal)
            result = self.engine.validate_plan(proposal, account, evidence, policy, now=self._now(),
                                               decision_core=core)
            if result['status'] == 'VALID':
                blocks = execution_blocks(self.root / 'configs/execution_rules.toml', policy,
                    result['accepted_orders'], proposal['target_session'], now=self._now())
                if blocks:
                    result = {**result, 'status': 'WAITING_VALIDATION', 'plan_envelope': None,
                              'issues': [{'kind': 'RULE_POLICY_CONFLICT', 'blocks': blocks}],
                              'required_inputs': [{'kind': 'RULE_MAINTENANCE_RESOLUTION'}]}
                    result['validation_id'] = 'validation:' + policy_digest({
                        k: v for k, v in result.items() if k not in {'validation_id', 'plan_envelope'}})
            return result
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            error = _error(exc)
            return {'status': error['state'], 'plan_envelope': None, 'accepted_orders': [],
                    'skipped_intents': [], 'issues': [error], 'required_inputs': []}

    def _view(self, account_id, plan_id):
        reject_credentials({'account_id': account_id, 'plan_id': plan_id})
        view = self.ledger.execution_view(account_id, plan_id)
        plan = view['frozen_plan']
        reference = plan.get('document', {})
        path = local_path(self.root, reference['path'])
        if not path.is_relative_to((self.root / 'data/plans/frozen').resolve()):
            raise ExecutionSupportError('INTEGRITY_ERROR', '封存原件路径无效')
        read_business_document(self.root, reference)
        expected = {k: v for k, v in plan.items() if k not in {'document', 'id'}}
        if policy_digest(expected) != reference.get('content_hash'):
            raise ExecutionSupportError('INTEGRITY_ERROR', '封存原件与账本内容不一致')
        verify_plan_decision(self.root, plan, market=self.market, materialize=False)
        if plan.get('decision_ref'):
            decision = self.ledger.get_strategy_decision(account_id,
                decision_id=plan['decision_ref']['decision_id'])
            if decision is not None and (decision['state'] != 'FROZEN'
                    or decision['plan_id'] != plan_id or decision['decision_ref'] != plan['decision_ref']):
                raise ExecutionSupportError('INTEGRITY_ERROR', '冻结计划与权威决策不一致')
        return view

    @staticmethod
    def _summary(view):
        rows = view['orders']
        complete = all(row['status'] in TERMINAL for row in rows)
        return {'completion_status': 'COMPLETE' if complete else 'PARTIAL',
                'plan_status': 'EMPTY' if not rows else 'COMPLETE' if complete else 'PENDING',
                'order_states': {row['order_id']: row['status'] for row in rows},
                'account_revision': view['account_revision']}

    def match(self, account_id, plan_id, *, knowledge_cutoff=None):
        try:
            view = self._view(account_id, plan_id)
            plan = view['frozen_plan']
            cutoff = to_wire(knowledge_cutoff or self._now())
            if aware_time(cutoff) > self._now():
                raise ExecutionSupportError('INVALID_INPUT', '知识截止不得晚于实际时点')
            if all(o['status'] in TERMINAL for o in view['orders']):
                return {'state': 'CALCULATED', 'results': [], 'submission_status': 'NOT_SUBMITTED',
                        'account_id': account_id, 'plan_id': plan_id, 'plan_hash': plan['plan_hash'],
                        'account_view': view, **self._summary(view)}
            market = collect_execution_market(self.market, plan, knowledge_cutoff=cutoff)
            market['policy_conflicts'] = execution_blocks(
                self.root / 'configs/execution_rules.toml', plan['ruleset'],
                [o['order'] for o in view['orders'] if o['status'] not in TERMINAL],
                plan['target_session'], now=self._now())
            market['bundle_hash'] = policy_digest({k: v for k, v in market.items() if k != 'bundle_hash'})
            results = to_wire(self.engine.match(plan, view, market))
            reject_credentials(results)
            return {'state': 'CALCULATED', 'results': results, 'submission_status': 'NOT_SUBMITTED',
                    'account_id': account_id, 'plan_id': plan_id, 'plan_hash': plan['plan_hash'],
                    'market_bundle_hash': market['bundle_hash'], 'account_view': view,
                    'calculation_status': 'PARTIAL' if any(r['state'] == 'WAITING_DATA'
                                                        for r in results) else 'COMPLETE',
                    **self._summary(view)}
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            return {**_error(exc), 'results': [], 'submission_status': 'NOT_SUBMITTED',
                    'completion_status': 'ERROR', 'account_id': account_id, 'plan_id': plan_id}

    def freeze(self, proposal, *, operation_id, summary=False):
        """Explicit write path; validation is never itself a booking receipt."""
        try:
            reject_credentials({'proposal': proposal, 'operation_id': operation_id})
            request = normalize_proposal(proposal)
            existing = self.ledger.get_plan(request['account_id'],
                                            target_session=request['target_session'])
            if existing:
                if existing.get('validation_report', {}).get('proposal_hash') == policy_digest(request):
                    self._view(request['account_id'], existing['plan_id'])
                    if summary:
                        return {'state': 'ALREADY_APPLIED', 'plan_id': existing['plan_id'],
                                'audit_scope': 'FROZEN_COMMIT_RECEIPT'}
                    return {'state': 'ALREADY_APPLIED', 'validation': existing['validation_report'],
                            'commit': {'state': 'ALREADY_APPLIED', 'plan_id': existing['plan_id']},
                            'plan': existing}
                raise ExecutionSupportError('CONFLICT', '该目标日已有不同的冻结计划')
            identity = {'account_id': request['account_id'], 'target_session': request['target_session']}
            validation = stage('execution.plan_validate', lambda: self.validate(proposal), **identity)
            if validation['status'] != 'VALID':
                return {'state': validation['status'], 'validation': validation,
                        'commit': None, 'plan': None}
            envelope = validation['plan_envelope']
            commit = stage('execution.freeze_commit', lambda: self.ledger.freeze_plan(
                request['account_id'], envelope, operation_id=operation_id,
                expected_revision=validation['account_revision']), **identity)
            if summary and commit['state'] in {'APPLIED', 'ALREADY_APPLIED'}:
                return {'state': commit['state'], 'plan_id': commit['plan_id'],
                        'audit_scope': 'FROZEN_COMMIT_RECEIPT', 'commit': commit}
            plan = (stage('execution.freeze_result', lambda: self.ledger.get_plan(
                request['account_id'], target_session=request['target_session']), **identity)
                    if commit['state'] in {'APPLIED', 'ALREADY_APPLIED'} else None)
            return {'state': commit['state'], 'validation': validation, 'commit': commit, 'plan': plan}
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            return {**_error(exc), 'validation': None, 'commit': None, 'plan': None}

    def settle(self, account_id, plan_id, *, operation_id, knowledge_cutoff=None,
               should_continue=None):
        """Commit proven results, reread guards after progress, then stop on WAIT.

        No timer, collection, strategy invocation, corporate action or session close
        is hidden here. Those remain the caller's explicit workflow responsibilities.
        """
        try:
            reject_credentials({'operation_id': operation_id})
            if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 256:
                raise ExecutionSupportError('INVALID_INPUT', '操作ID须为非空短字符串')
            return self._settle(account_id, plan_id, operation_id=operation_id,
                                knowledge_cutoff=knowledge_cutoff, should_continue=should_continue)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            return {**_error(exc), 'completion_status': 'ERROR', 'receipts': []}

    def _settle(self, account_id, plan_id, *, operation_id, knowledge_cutoff=None,
                should_continue=None):
        cutoff = to_wire(knowledge_cutoff or self._now())
        if aware_time(cutoff) > self._now():
            raise ExecutionSupportError('INVALID_INPUT', '知识截止不得晚于实际时点')
        receipts, calculations = [], []
        def exhausted():
            return should_continue is not None and not should_continue()

        def stopped():
            return {'state': 'PARTIAL', 'reason': 'RUN_BUDGET_EXHAUSTED',
                'budget_exhausted': True, 'completion_status': 'PARTIAL',
                'account_id': account_id, 'plan_id': plan_id,
                'receipts': receipts, 'calculations': calculations}

        if exhausted():
            return stopped()
        terminal = self.ledger.terminal_execution_status(account_id, plan_id)
        if terminal['state'] == 'READY':
            return {**terminal, 'state': 'APPLIED', 'submission_status': 'UNCHANGED',
                    'receipts': [], 'calculations': [], 'results': []}
        initial = self.match(account_id, plan_id, knowledge_cutoff=cutoff)
        if initial.get('completion_status') == 'COMPLETE':
            terminal = self.ledger.terminal_execution_status(account_id, plan_id)
            if terminal['state'] == 'READY':
                return {**terminal, 'state': 'APPLIED', 'submission_status': 'UNCHANGED',
                        'receipts': [], 'calculations': [], 'results': []}
        current = initial
        if initial['state'] != 'CALCULATED':
            return {**initial, 'receipts': receipts}
        maximum = len(initial['account_view']['orders']) + 2
        for _ in range(maximum):
            if exhausted():
                return stopped()
            if current['state'] != 'CALCULATED':
                return {**current, 'receipts': receipts}
            if current['completion_status'] == 'COMPLETE':
                break
            calculations.append({'bundle_hash': current.get('market_bundle_hash'),
                                 'results': current['results']})
            view = current['account_view']
            terminal = [r for r in current['results'] if r['state'] != 'WAITING_DATA']
            if terminal:
                selected = terminal
            else:
                prior = {o['order_id']: o['order'].get('last_attempt_identity') or (
                    _waiting_identity(o['order']['last_attempt']) if
                    o['order'].get('last_attempt') else None) for o in view['orders']}
                selected = [r for r in current['results'] if not prior.get(r['order_id']) or
                            _waiting_identity(r) != prior[r['order_id']]]
            if not selected:
                break
            receipt = self.ledger.apply_execution_results(account_id, {'results': selected},
                operation_id=operation_id, expected_revision=view['account_revision'])
            receipts.append(receipt)
            if exhausted():
                return stopped()
            fresh = self.match(account_id, plan_id, knowledge_cutoff=cutoff)
            if fresh['state'] != 'CALCULATED':
                return {**fresh, 'receipts': receipts, 'calculations': calculations}
            before = sum(s in TERMINAL for s in current['order_states'].values())
            after = sum(s in TERMINAL for s in fresh.get('order_states', {}).values())
            current = fresh
            errors = [r for r in receipt.get('results', [receipt])
                      if r['state'] not in {'APPLIED', 'ALREADY_APPLIED', 'WAITING_DATA'}]
            if errors:
                return {**current, 'state': 'ERROR', 'completion_status': 'ERROR',
                        'errors': errors, 'receipts': receipts, 'calculations': calculations}
            if not terminal or after <= before:
                break
        result = {**current, 'state': 'APPLIED' if current.get('completion_status') == 'COMPLETE'
                  else 'PARTIAL', 'submission_status': 'SUBMITTED' if receipts else 'UNCHANGED',
                  'receipts': receipts, 'calculations': calculations}
        if current['state'] == 'CALCULATED' and current['account_view']['orders']:
            step = {'effective_date': current['account_view']['target_session'], 'step': 'execution',
                    'status': 'SUCCEEDED' if result['completion_status'] == 'COMPLETE' else 'WAITING_DATA',
                    'plan_id': plan_id, 'order_states': result['order_states']}
            result['step_receipt'] = self.ledger.record_step_result(account_id, step,
                operation_id='execution-step:' + policy_digest(step),
                expected_revision=current['account_revision'])
            if result['step_receipt']['state'] not in {'APPLIED', 'ALREADY_APPLIED'}:
                result['workflow_warning'] = {'kind': 'STEP_RECORD_FAILED',
                    'retry': '调用相同settle补记步骤；已终态订单不重新撮合',
                    'receipt': result['step_receipt']}
        return result
