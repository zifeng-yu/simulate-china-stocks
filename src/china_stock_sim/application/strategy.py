"""Explicit strategy planning, durable model attempts and publication recovery.

No scheduler, activation, collection, settlement or hidden account creation lives here.
"""

import fcntl
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from time import monotonic
from zoneinfo import ZoneInfo

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.application.strategy_inputs import assemble_inputs, context_from_bundle
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.common.strategy_definitions import get_definition
from china_stock_sim.common.wire import reject_credentials, to_wire
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError
from china_stock_sim.ledger.strategy_decisions import TERMINAL
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.models import create_model_client, load_model_settings
from china_stock_sim.models.audit import ModelAudit
from china_stock_sim.runtime.recording import stage


def audit_wire(value):
    """Only public ModelAudit values may enter this float-to-decimal conversion."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('公开模型审计数值非有限')
        return str(Decimal(str(value)))
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {key: audit_wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [audit_wire(item) for item in value]
    return to_wire(value)


@contextmanager
def planning_lock(root, account_id, target):
    identity = policy_digest({'account_id': account_id, 'target': target})
    directory = root / 'data/state/strategy-locks'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (identity + '.lock')).open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LedgerError('ALREADY_RUNNING', '该账户目标日已有规划进程') from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class StrategyApplication:
    def __init__(self, settings, *, market=None, ledger=None, clock=None, test_mode=False,
                 model_client=None, model_settings=None, model_caller=None):
        self.settings, self.root = settings, settings.root
        self.clock = clock or (lambda: datetime.now(ZoneInfo('Asia/Shanghai')))
        self.market = market or MarketDataService(self.root)
        self.ledger = ledger or LedgerService(settings, market=self.market,
                                               clock=clock, test_mode=test_mode)
        self.execution = ExecutionApplication(settings, market=self.market, ledger=self.ledger,
                                              clock=clock, test_mode=test_mode)
        self.model_client, self.model_settings, self.model_caller = (
            model_client, model_settings, model_caller)

    def _now(self):
        return aware_time(self.clock())

    def _decision(self, account, target):
        return self.ledger.get_strategy_decision(account, target_session=target)

    def _model(self, reference):
        settings = self.model_settings or load_model_settings(self.root, reference)
        audit = ModelAudit(settings)
        configuration = audit_wire(audit.configuration(reference, json_mode=True,
                                                       max_output_tokens=settings.max_output_tokens))
        return settings, audit, configuration

    def _assemble(self, account, target, cutoff):
        assembled = assemble_inputs(self.settings, self.market, self.ledger, account,
                                     target, self._now(), cutoff)
        bundle = assembled.get('input_bundle')
        if bundle and bundle['definition']['uses_model']:
            from china_stock_sim.strategies.llm import build_messages, build_request

            reference = bundle['context']['policy']['parameters']['model_ref']
            _, audit, configuration = self._model(reference)
            request = build_request(context_from_bundle(bundle))
            bundle['model_configuration'] = configuration
            bundle['planning_request'] = request
            if request['state'] != 'READY':
                assembled.update(state='WAITING_INPUT', reasons=request.get('reasons', []),
                                 required_inputs=request.get('reasons', []))
            else:
                bundle['model_messages'] = audit.messages(build_messages(request))
        elif bundle and assembled['state'] == 'READY':
            from china_stock_sim.strategies.rules import plan

            calculated = plan(context_from_bundle(bundle))
            if calculated.state != 'READY':
                assembled.update(state=calculated.state, reasons=to_wire(calculated.reasons),
                                 required_inputs=to_wire(calculated.reasons))
        if bundle:
            assembled['input_hash'] = policy_digest(bundle)
        return assembled

    @staticmethod
    def _error(exc):
        from china_stock_sim.runtime.recording import record_exception
        record_exception(exc)
        return {'state': getattr(exc, 'state', getattr(exc, 'code', 'INVALID_INPUT')),
                'reasons': [{'kind': type(exc).__name__, 'reason': str(exc)}]}

    def inspect(self, account_id, target_session, *, knowledge_cutoff=None, include_input=False):
        """Read-only, including when the account is unactivated or the target is stale."""
        try:
            target = session_date(target_session)
            reject_credentials({'account_id': account_id, 'target_session': target})
            decision = self._decision(account_id, target)
            existing = self.ledger.get_plan(account_id, target_session=target)
            if existing:
                self.execution._view(account_id, existing['plan_id'])
                return {'state': 'FROZEN', 'plan_id': existing['plan_id'], 'decision': decision}
            if decision and (decision['state'] in TERMINAL | {'PROPOSED', 'WAITING_VALIDATION'}):
                return {'state': decision['state'], 'decision': decision}
            result = self._assemble(account_id, target, knowledge_cutoff)
            result['decision'] = decision
            if not include_input and 'input_bundle' in result:
                bundle = result.pop('input_bundle')
                securities = bundle['context']['securities']
                result['input_summary'] = {'market_as_of': bundle['context']['market_as_of'],
                    'target_session': target, 'input_cutoff': bundle['context']['input_cutoff'],
                    'securities': len(securities),
                    'eligible': sum(s['eligibility'] == 'READY' for s in securities.values()),
                    'required_history_sessions': bundle['required_history_sessions'],
                    'account_revision': bundle['account_revision']}
            return result
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
            return self._error(exc)

    def _write(self, method, account, payload, revision, *, with_operation=False):
        operation = 'strategy:' + policy_digest({'method': method, 'account': account,
                                                'payload': payload, 'revision': revision})
        result = getattr(self.ledger, method)(account, payload,
                            operation_id=operation, expected_revision=revision)
        if result['state'] not in {'APPLIED', 'ALREADY_APPLIED'}:
            raise LedgerError(result['state'], '策略持久操作未完成', receipt=result)
        return (result, operation) if with_operation else result

    def _save(self, row, state, *, reasons=(), proposal=None, core=None, summary=False):
        payload = {'decision_id': row['decision_id'], 'target_session': row['target_session'],
                   'expected_sequence': row['sequence'], 'state': state, 'reasons': to_wire(reasons)}
        if proposal is not None:
            payload.update(proposal=proposal, decision_core=core)
        account = self.ledger.account_state(row['account_id'], row['target_session'])
        if summary and state == 'PROPOSED':
            receipt, operation = self._write('save_strategy_decision', row['account_id'], payload,
                                             account['account_revision'], with_operation=True)
            # This invocation's committed result, not the requested state, authorizes
            # continuing with our proposal. Replays and older adapters read authority.
            if (receipt.get('state') == 'APPLIED' and receipt.get('decision_state') == 'PROPOSED'
                    and receipt.get('decision_id') == row['decision_id']
                    and type(receipt.get('sequence')) is int
                    and receipt['sequence'] == row['sequence'] + 1
                    and receipt.get('operation_id') == operation
                    and isinstance(receipt.get('commit_id'), str) and len(receipt['commit_id']) == 32
                    and all(c in '0123456789abcdef' for c in receipt['commit_id'])
                    and isinstance(receipt.get('payload_hash'), str)
                    and len(receipt['payload_hash']) == 64
                    and all(c in '0123456789abcdef' for c in receipt['payload_hash'])
                    and type(receipt.get('account_revision')) is int
                    and receipt.get('account_revision') == account['account_revision']):
                return {'state': 'PROPOSED', 'decision_id': row['decision_id'],
                        'sequence': receipt['sequence'], 'proposal': proposal,
                        'decision_ref': proposal['decision_ref'],
                        'audit_scope': 'PROPOSED_COMMIT_RECEIPT'}
        else:
            self._write('save_strategy_decision', row['account_id'], payload, account['account_revision'])
        return self._decision(row['account_id'], row['target_session'])

    def _no_plan(self, account, target, reason):
        current = self.ledger.account_state(account, target)
        return self._write('record_no_plan', account, {'target_session': target, 'reason': reason},
                           current['account_revision'])

    def _prepare(self, account, target, cutoff):
        row = self._decision(account, target)
        snapshot = self.ledger.report_snapshot(account, target, mode='recent')
        attempts = [a for a in snapshot['model_attempts'] if a['target_session'] == target]
        if row and (attempts or row['state'] not in {'WAITING_INPUT', 'PREPARED'}):
            return row
        identity = {'account_id': account, 'target_session': target}
        assembled = stage('strategy.assemble', lambda: self._assemble(account, target, cutoff), **identity)
        bundle = assembled.get('input_bundle')
        if bundle is None:
            return assembled
        context = bundle['context']
        stage('strategy.prepare_write', lambda: self._write('prepare_strategy_decision', account, {
            'target_session': target, 'market_as_of': context['market_as_of'],
            'input_cutoff': context['input_cutoff'], 'input_bundle': bundle,
            'expected_sequence': row['sequence'] if row else 0,
            'state': 'PREPARED' if assembled['state'] == 'READY' else 'WAITING_INPUT',
            'reasons': assembled.get('reasons', []), 'required_inputs': assembled.get('required_inputs', []),
            'parent_task_id': (context['parent_task'] or {}).get('task_id')}, bundle['account_revision']),
            **identity)
        return self._decision(account, target)

    def _guard(self, row, bundle):
        if row['state'] in TERMINAL:
            return row
        target, account = row['target_session'], row['account_id']
        if self._now() >= datetime.fromisoformat(target + 'T09:00:00+08:00'):
            return self._save(row, 'MISSED_CUTOFF', reasons=[{'kind': 'MISSED_CUTOFF'}])
        definition = get_definition(row['strategy_id'], self.settings)
        policy = self.ledger.get_account_policy(account, target)
        changed = (definition['implementation_hash'] != row['strategy_implementation_hash']
                   or definition['strategy_version'] != row['strategy_version']
                   or policy['policy_hash'] != row['policy_hash'])
        if bundle['definition']['uses_model']:
            reference = bundle['context']['policy']['parameters']['model_ref']
            try:
                changed |= self._model(reference)[2] != bundle['model_configuration']
            except (ValueError, RuntimeError, OSError):
                changed = True
        if changed:
            return self._save(row, 'CONFIG_CHANGED', reasons=[{'kind': 'CONFIG_CHANGED'}])
        current = self.ledger.account_state(account, target)
        if current['account_revision'] != row['account_revision']:
            return self._save(row, 'STALE_INPUT', reasons=[{'kind': 'STALE_INPUT'}])
        return row

    def _proposal(self, row, bundle, calculated):
        calculation = to_wire(calculated.calculation)
        definition = bundle['definition']
        identity = {'strategy_name': row['strategy_id'], 'program_revision': row['strategy_version'],
                    'uses_model': definition['uses_model'],
                    'implementation_hash': definition['implementation_hash']}
        if definition['uses_model']:
            configuration = bundle['model_configuration']
            identity['model'] = {**configuration, 'generation_parameters': {
                key: configuration[key] for key in ('temperature', 'max_output_tokens', 'json_mode', 'options')
                if key in configuration}}
            identity['prompt_revision'] = bundle['planning_request'].get('prompt_revision', 'LLM-Daily-1')
        calculation['identity'] = identity
        proposal = {'proposal_id': 'proposal:' + policy_digest({'input': row['input_hash'],
                     'calculation': calculation, 'intents': to_wire(calculated.ordered_intents)}),
            'account_id': row['account_id'], 'strategy_id': row['strategy_id'],
            'strategy_version': row['strategy_version'], 'target_session': row['target_session'],
            'market_as_of': row['market_as_of'], 'input_cutoff': row['input_cutoff'],
            'generated_at': self._now().isoformat(), 'account_snapshot_id': row['account_snapshot_id'],
            'account_revision': row['account_revision'], 'ordered_intents': to_wire(calculated.ordered_intents),
            'no_order_reason': calculated.no_order_reason, 'rebalance_task_id': calculated.task_id,
            'market_snapshot_ids': bundle['market_snapshot_ids'], 'data_evidence': bundle['data_evidence'],
            'provenance_mode': 'STRATEGY'}
        # Normalize before hashing: the public validator adds nullable intent fields.
        from china_stock_sim.common.plan_validation import normalize_proposal

        dummy = {'decision_id': row['decision_id'], 'account_policy_id': row['policy_id'],
            'account_policy_hash': row['policy_hash'],
            'strategy_implementation_hash': row['strategy_implementation_hash'],
            'input_hash': row['input_hash'],
            'decision_core': {'path': 'data/plans/strategy/' + '0' * 64 + '.json',
                              'content_hash': '0' * 64}}
        proposal = normalize_proposal({**proposal, 'decision_ref': dummy})
        proposal.pop('decision_ref')
        core = {'schema_version': 1, 'decision_id': row['decision_id'],
                'account_policy': bundle['context']['policy'],
                'strategy_implementation_hash': row['strategy_implementation_hash'],
                'input_bundle': bundle, 'proposal': proposal, 'calculation': calculation}
        core_reference = document(self.root, 'strategy', core)
        return {**proposal, 'decision_ref': {**dummy, 'decision_core': core_reference}}, core

    def _accept(self, row, proposal, core, *, summary=False):
        validation = self.execution.validate(proposal)
        state = validation['status']
        if state == 'VALID':
            updated = self._save(row, 'PROPOSED', proposal=proposal, core=core, summary=summary)
            if updated.get('audit_scope') == 'PROPOSED_COMMIT_RECEIPT':
                return {'state': updated['state'], 'proposed_commit': updated}
            return {'state': updated['state'], 'decision': updated, 'validation': to_wire(validation)}
        if state in {'WAITING_VALIDATION', 'WAITING_DATA'}:
            updated = self._save(row, 'WAITING_VALIDATION', reasons=validation.get('issues', []),
                                 proposal=proposal, core=core)
            return {'state': updated['state'], 'decision': updated, 'validation': to_wire(validation)}
        return {'state': 'INVALID', 'decision': row, 'validation': to_wire(validation),
                'reasons': validation.get('issues', [])}

    def _rules(self, row, bundle, *, summary=False):
        from china_stock_sim.strategies.rules import plan

        context = context_from_bundle(bundle)
        calculated = plan(context)
        if calculated.state != 'READY':
            return {'state': calculated.state, 'decision': row, 'reasons': to_wire(calculated.reasons)}
        if calculated.targets and context.parent_task is None:
            payload = {**to_wire(calculated.calculation.get('new_task', {})),
                'task_id': calculated.task_id, 'start_session': row['target_session'],
                'end_session': context.task_sessions[-1], 'targets': to_wire(calculated.targets),
                'policy_hash': row['policy_hash'],
                'strategy_implementation_hash': row['strategy_implementation_hash'],
                'input_document': row['input_document'], 'decision_id': row['decision_id'],
                'calculation': to_wire(calculated.calculation), 'initial_evaluated': True}
            self._write('save_rebalance_task', row['account_id'], payload, row['account_revision'])
        proposal, core = self._proposal(row, bundle, calculated)
        accepted = self._accept(row, proposal, core, summary=summary)
        if accepted['state'] == 'INVALID':
            updated = self._save(row, 'INVALID', reasons=accepted.get('reasons', []))
            accepted['decision'] = updated
        return accepted

    def _finish_attempt(self, row, attempt, status, *, response=None, reason=None):
        account = self.ledger.account_state(row['account_id'], row['target_session'])
        payload = {'target_session': row['target_session'], 'attempt_id': attempt['attempt_id'],
                   'status': status, 'decision_id': row['decision_id'], 'input_hash': row['input_hash']}
        if response is not None:
            payload['response'] = response
        if reason is not None:
            payload['reason'] = reason
        return self._write('finish_model_attempt', row['account_id'], payload, account['account_revision'])

    def _llm(self, row, bundle, *, summary=False):
        from china_stock_sim.strategies.llm import build_messages, parse_response
        from china_stock_sim.strategies.llm_call import call_with_deadline

        account, target = row['account_id'], row['target_session']
        parameters = bundle['context']['policy']['parameters']
        model_settings, audit, configuration = self._model(parameters['model_ref'])
        client = self.model_client
        attempts = [a for a in self.ledger.report_snapshot(account, target, mode='recent')['model_attempts']
                    if a['target_session'] == target]
        for attempt in attempts:
            if attempt['status'] == 'CLAIMED':
                self._finish_attempt(row, attempt, 'INTERRUPTED', reason='PROCESS_INTERRUPTED')
        first = aware_time(attempts[0]['claimed_at']) if attempts else self._now()
        deadline = min(first + timedelta(seconds=parameters['timeout_seconds']),
                       datetime.fromisoformat(target + 'T08:59:50+08:00'))
        budget_end = monotonic() + max(0, (deadline - self._now()).total_seconds())
        previous, errors = None, None
        while True:
            row = self._guard(row, bundle)
            if row['state'] in TERMINAL:
                return {'state': row['state'], 'decision': row}
            attempts = [a for a in self.ledger.report_snapshot(account, target, mode='recent')['model_attempts']
                        if a['target_session'] == target]
            active = next((a for a in reversed(attempts) if a['status'] in {'RECEIVED', 'SUCCEEDED'}), None)
            if active:
                response, _ = read_business_document(self.root, active['response_document'])
            else:
                remaining = min((deadline - self._now()).total_seconds(), budget_end - monotonic())
                if len(attempts) >= parameters['max_attempts_per_target_session'] or remaining < 1:
                    row = self._save(row, 'MODEL_FAILED', reasons=[{'kind': 'MODEL_ATTEMPT_OR_TIME_LIMIT'}])
                    return {'state': row['state'], 'decision': row}
                if previous is None and attempts and attempts[-1].get('response_document'):
                    prior, _ = read_business_document(self.root, attempts[-1]['response_document'])
                    previous = prior.get('text')
                    errors = ([{'code': attempts[-1].get('reason') or attempts[-1]['status']}]
                              if previous is not None else None)
                messages = build_messages(bundle['planning_request'], previous, errors)
                request = audit_wire(audit.request(parameters['model_ref'], messages,
                    max_output_tokens=model_settings.max_output_tokens, json_mode=True))
                claim = self._write('claim_model_attempt', account, {
                    'target_session': target, 'decision_id': row['decision_id'],
                    'attempt_id': f'{target}:{len(attempts) + 1}',
                    'input_hash': row['input_hash'], 'expected_sequence': row['sequence'],
                    'request': request}, row['account_revision'])
                active = {'attempt_id': claim['attempt_id']}
                try:
                    client = client or create_model_client(model_settings)
                    result = (self.model_caller or call_with_deadline)(client, messages,
                        timeout_seconds=remaining, max_output_tokens=model_settings.max_output_tokens)
                    response = audit_wire(audit.result(result))
                except Exception as exc:
                    # Provider errors may contain private material: only a fixed category is persisted.
                    timed_out = (isinstance(exc, TimeoutError)
                                 or getattr(exc, 'code', '') in {'TIMEOUT', 'MODEL_TIMEOUT'})
                    self._finish_attempt(row, active, 'TIMEOUT' if timed_out else 'FAILED',
                                         reason='MODEL_TIMEOUT' if timed_out else 'MODEL_CALL_FAILED')
                    continue
                self._finish_attempt(row, active, 'RECEIVED', response=response)
            row = self._guard(row, bundle)
            if row['state'] in TERMINAL:
                return {'state': row['state'], 'decision': row}
            if response.get('finish_reason') != 'stop':
                errors = [{'code': 'INCOMPLETE_MODEL_RESPONSE', 'path': 'finish_reason'}]
            else:
                calculated = parse_response(response['text'], bundle['planning_request'])
                if calculated.state == 'READY':
                    calculation = {**calculated.calculation, 'model_attempt_id': active['attempt_id'],
                                   'model_response': next(a['response_document'] for a in
                                     self.ledger.report_snapshot(account, target, mode='recent')['model_attempts']
                                     if a['attempt_id'] == active['attempt_id'])}
                    from dataclasses import replace

                    calculated = replace(calculated, calculation=calculation)
                    proposal, core = self._proposal(row, bundle, calculated)
                    accepted = self._accept(row, proposal, core, summary=summary)
                    if accepted['state'] != 'INVALID':
                        return accepted
                    errors = accepted.get('reasons', [])
                else:
                    errors = to_wire(calculated.reasons)
            document(self.root, 'strategy-model-errors', {'decision_id': row['decision_id'],
                     'attempt_id': active['attempt_id'], 'errors': errors})
            self._finish_attempt(row, active, 'INVALID', reason='MODEL_OUTPUT_INVALID')
            previous = response.get('text')

    def _frozen_receipt(self, account, target):
        """A summary consumer checks immutable completion, not dynamic trade guards."""
        identity = {'account_id': account, 'target_session': target}
        receipt = stage('strategy.frozen_receipt', lambda: self.ledger.frozen_plan_receipt(
            account, target_session=target), **identity)
        if receipt['state'] == 'ABSENT':
            return None
        if receipt['state'] == 'UNAVAILABLE':
            def full_check():
                existing = self.ledger.get_plan(account, target_session=target)
                if existing is None:
                    raise LedgerError('INTEGRITY_ERROR', '冻结计划在核验期间消失')
                self.execution._view(account, existing['plan_id'])
                return existing
            existing = stage('strategy.frozen_verify', full_check, **identity)
            receipt = self.ledger.frozen_plan_receipt(account, target_session=target)
            if receipt['state'] == 'UNAVAILABLE':
                # Derived caches are bounded and disposable. A successful full
                # verification remains valid when no reusable proof can be kept.
                receipt = {'plan_id': existing['plan_id'], 'audit_scope': 'FROZEN_FULL_VERIFIED'}
            elif receipt['state'] != 'READY':
                raise LedgerError('INTEGRITY_ERROR', '冻结计划在核验期间消失')
        return {**receipt, 'state': 'FROZEN', 'replayed': True}

    def _run(self, action, account_id, target_session, cutoff, *, summary=False):
        target = session_date(target_session)
        reject_credentials({'account_id': account_id, 'target_session': target})
        with planning_lock(self.root, account_id, target):
            if summary:
                frozen = self._frozen_receipt(account_id, target)
                if frozen is not None:
                    return frozen
            existing = self.ledger.get_plan(account_id, target_session=target)
            if existing:
                self.execution._view(account_id, existing['plan_id'])
                return {'state': 'FROZEN', 'plan_id': existing['plan_id'], 'plan': existing,
                        'decision': self._decision(account_id, target), 'replayed': True}
            prior = self._decision(account_id, target)
            if prior and prior['state'] in TERMINAL:
                return {'state': prior['state'], 'decision': prior}
            if prior and self._now() >= datetime.fromisoformat(target + 'T09:00:00+08:00'):
                row = self._save(prior, 'MISSED_CUTOFF', reasons=[{'kind': 'MISSED_CUTOFF'}])
                self._no_plan(account_id, target, 'MISSED_CUTOFF')
                return {'state': row['state'], 'decision': row}
            if prior and prior['state'] == 'PREPARED':
                snapshot = self.ledger.report_snapshot(account_id, target, mode='recent')
                occupied = any(a['target_session'] == target for a in snapshot['model_attempts'])
                task_saved = any(t.get('decision_id') == prior['decision_id']
                                 for t in snapshot['rebalance_tasks'])
                definition = get_definition(prior['strategy_id'], self.settings)
                same_definition = (definition['implementation_hash'] == prior['strategy_implementation_hash']
                    and self.ledger.get_account_policy(account_id, target)['policy_hash'] == prior['policy_hash'])
                if (not occupied and not task_saved and same_definition
                        and snapshot['account_revision'] != prior['account_revision']):
                    prior = self._prepare(account_id, target, cutoff)
                    if not prior.get('decision_id'):
                        return prior
            # An existing prepared input is recovered before considering replacement. In particular
            # a saved parent task plus a crashed PREPARED decision must not be ranked again.
            row = prior if prior and prior['state'] != 'WAITING_INPUT' else self._prepare(account_id, target, cutoff)
            if not row.get('decision_id'):
                if prior and row['state'] in TERMINAL - {'FROZEN'}:
                    saved = self._save(prior, row['state'], reasons=row.get('reasons', []))
                    row = {'state': saved['state'], 'decision': saved}
                if row['state'] != 'READY':
                    self._no_plan(account_id, target, row['state'])
                return row
            if row['state'] == 'WAITING_INPUT':
                return {'state': row['state'], 'decision': row}
            bundle, _ = read_business_document(self.root, row['input_document'])
            row = self._guard(row, bundle)
            if row['state'] in TERMINAL:
                return {'state': row['state'], 'decision': row}
            if action == 'prepare':
                return {'state': row['state'], 'decision': row}
            if row['state'] in {'PROPOSED', 'WAITING_VALIDATION'}:
                proposal = row['proposal']
                core, _ = read_business_document(self.root, row['decision_core'])
                outcome = ({'state': 'PROPOSED', 'decision': row} if row['state'] == 'PROPOSED'
                           else self._accept(row, proposal, core, summary=summary and action == 'publish'))
            else:
                uses_model = bundle['definition']['uses_model']
                outcome = stage('strategy.model' if uses_model else 'strategy.rules',
                    lambda: (self._llm if uses_model else self._rules)(
                        row, bundle, summary=summary and action == 'publish'),
                    account_id=account_id, target_session=target)
            if outcome['state'] == 'INVALID' and outcome.get('decision', {}).get('state') not in TERMINAL:
                outcome['decision'] = self._save(outcome['decision'], 'INVALID',
                                                 reasons=outcome.get('reasons', []))
            if action == 'publish' and outcome['state'] == 'PROPOSED':
                row = outcome.get('proposed_commit') or outcome['decision']
                result = stage('strategy.freeze', lambda: self.execution.freeze(row['proposal'],
                    operation_id='strategy-freeze:' + row['decision_ref']['decision_core']['content_hash'],
                    **({'summary': True} if summary else {})),
                    account_id=account_id, target_session=target)
                if result['state'] in {'APPLIED', 'ALREADY_APPLIED'}:
                    if summary:
                        return {'state': 'FROZEN', 'plan_id': result['plan_id'],
                                'decision_id': row['decision_id'], 'audit_scope': 'FROZEN_COMMIT_RECEIPT'}
                    return stage('strategy.result_read', lambda: {
                        'state': 'FROZEN', 'decision': self._decision(account_id, target),
                        'plan_id': result['plan']['plan_id'], 'publication': to_wire(result)},
                        account_id=account_id, target_session=target)
                if 'proposed_commit' in outcome:
                    row = self._decision(account_id, target)
                return {'state': result['state'], 'decision': row, 'publication': to_wire(result)}
            if outcome['state'] in TERMINAL - {'FROZEN'}:
                self._no_plan(account_id, target, outcome['state'])
            return outcome

    def _invoke(self, action, account, target, cutoff, *, summary=False):
        try:
            return self._run(action, account, target, cutoff, summary=summary)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
            return self._error(exc)

    def prepare(self, account_id, target_session, *, knowledge_cutoff=None):
        return self._invoke('prepare', account_id, target_session, knowledge_cutoff)

    def plan(self, account_id, target_session, *, knowledge_cutoff=None):
        return self._invoke('plan', account_id, target_session, knowledge_cutoff)

    def publish(self, account_id, target_session, *, knowledge_cutoff=None, summary=False):
        return self._invoke('publish', account_id, target_session, knowledge_cutoff, summary=summary)

    def recover(self, account_id, target_session, *, knowledge_cutoff=None, summary=False):
        return self.publish(account_id, target_session, knowledge_cutoff=knowledge_cutoff, summary=summary)

    def evaluate(self, input_bundle, *, model_response=None):
        """Offline fixed-input calculation. Never writes or calls a model."""
        reject_credentials(input_bundle)
        definition = get_definition(input_bundle['context']['strategy_id'], self.settings)
        if definition['implementation_hash'] != input_bundle['definition']['implementation_hash']:
            return {'state': 'CONFIG_CHANGED', 'reasons': [{'kind': 'HISTORICAL_IMPLEMENTATION_CHANGED'}]}
        if definition['uses_model']:
            from china_stock_sim.strategies.llm import parse_response

            if model_response is None:
                return {'state': 'WAITING_INPUT', 'reasons': [{'kind': 'STORED_MODEL_RESPONSE_REQUIRED'}]}
            return to_wire(parse_response(model_response, input_bundle['planning_request']))
        from china_stock_sim.strategies.rules import plan

        return to_wire(plan(context_from_bundle(input_bundle)))

    def audit(self, account_id, *, start=None, end=None, strategy_version=None, policy_id=None):
        rows = self.ledger.list_strategy_decisions(account_id, start=start, end=end,
                                                  strategy_version=strategy_version, policy_id=policy_id)
        result = []
        for row in rows:
            body = {**row, 'input_bundle': read_business_document(self.root, row['input_document'])[0]}
            if row.get('decision_core'):
                body['calculation'] = read_business_document(self.root, row['decision_core'])[0]['calculation']
            result.append(body)
        return {'state': 'READY', 'account_id': account_id, 'decisions': result}
