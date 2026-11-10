"""Durable decision indexes, immutable preparation history and atomic freeze binding."""

from copy import deepcopy
from datetime import datetime

from china_stock_sim.common.decisions import validate_decision_core
from china_stock_sim.common.policies import load_policy_bundle
from china_stock_sim.common.strategy_definitions import get_definition
from china_stock_sim.ledger.decision_evidence import (
    cached_validation,
    document_closure,
    read_business_document,
    remember_validation,
    verification_scope,
)
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest, identifier
from china_stock_sim.market.contracts import aware_time, session_date

TERMINAL = {'FROZEN', 'MODEL_FAILED', 'CONFIG_CHANGED', 'STALE_INPUT', 'INVALID', 'MISSED_CUTOFF'}
STATES = TERMINAL | {'WAITING_INPUT', 'PREPARED', 'WAITING_VALIDATION', 'PROPOSED'}
TRANSITIONS = {
    'WAITING_INPUT': {'WAITING_INPUT', 'PREPARED'},
    'PREPARED': {'WAITING_VALIDATION', 'PROPOSED'},
    'WAITING_VALIDATION': {'WAITING_VALIDATION', 'PROPOSED'},
    'PROPOSED': set(),
}


def decision_identity(account_id, target):
    return 'decision:' + digest({'account_id': account_id, 'target_session': target})


def _sequence(payload, expected):
    if type(payload.get('expected_sequence')) is not int or payload['expected_sequence'] != expected:
        raise LedgerError('CONFLICT', '决策过程序号已变化', sequence=expected)


def compact_decision_index(row):
    """Reference the proposal already frozen in the core; do not duplicate it in the index."""
    if row is None or row.get('proposal') is None:
        return row
    proposal = row['proposal']
    if isinstance(proposal, dict) and proposal.get('storage_format') == 'decision-core-proposal-v1':
        return row
    if not isinstance(proposal, dict) or not isinstance(row.get('decision_core'), dict):
        raise LedgerError('INTEGRITY_ERROR', '提案索引缺少决策原件')
    return {**row, 'proposal': {'storage_format': 'decision-core-proposal-v1',
            'source': row['decision_core'], 'content_hash': digest(proposal)}}


def resolve_decision_index(root, row):
    if row is None:
        return None
    proposal = row.get('proposal')
    if not isinstance(proposal, dict) or proposal.get('storage_format') != 'decision-core-proposal-v1':
        return row
    if set(proposal) != {'storage_format', 'source', 'content_hash'} or proposal['source'] != row.get('decision_core'):
        raise LedgerError('INTEGRITY_ERROR', '提案索引引用不一致')
    core, _ = read_business_document(root, proposal['source'])
    expanded = {**core['proposal'], 'decision_ref': row['decision_ref']}
    if digest(expanded) != proposal['content_hash']:
        raise LedgerError('INTEGRITY_ERROR', '提案索引摘要不一致')
    return {**row, 'proposal': expanded}


def _append_history(root, row, now, action):
    row = compact_decision_index(row)
    sequence = row.get('sequence', 0) + 1
    body = {k: v for k, v in row.items() if k not in {'history', 'document', 'id', 'updated_at'}}
    body.update(sequence=sequence, recorded_at=now, action=action)
    ref = document(root, 'strategy-decisions', body)
    return {**row, 'sequence': sequence, 'updated_at': now, 'document': ref,
            'history': [*row.get('history', []), {'sequence': sequence, 'state': row['state'],
                                                'document': ref, 'recorded_at': now}]}


def verify_decision_record(root, row, *, materialize=True):
    if row is None:
        return None
    try:
        stored = row
        content_hash = digest(row)
        if cached_validation(root, 'strategy_decision', content_hash):
            return deepcopy(resolve_decision_index(root, row) if materialize else row)
        row = resolve_decision_index(root, row)
        body, _ = read_business_document(root, row['document'])
        body = resolve_decision_index(root, body)
        for key, value in body.items():
            if key not in {'recorded_at', 'action'} and canonical(row.get(key)) != canonical(value):
                raise ValueError('index mismatch')
        references = {row['document']['path']: {row['document']['content_hash']}}
        for sequence, item in enumerate(row['history'], 1):
            historical, _ = read_business_document(root, item['document'])
            if (item['sequence'] != sequence or historical['sequence'] != sequence
                    or historical['decision_id'] != row['decision_id']
                    or historical['account_id'] != row['account_id']
                    or historical['target_session'] != row['target_session']
                    or historical['state'] != item['state']
                    or historical['recorded_at'] != item['recorded_at']):
                raise ValueError('history mismatch')
            references.setdefault(item['document']['path'], set()).add(item['document']['content_hash'])
        closure = document_closure(root, references)
        if row['decision_id'] != decision_identity(row['account_id'], row['target_session']):
            raise ValueError('identity mismatch')
        if (row['id'] != row['decision_id'] or len(row['history']) != row['sequence']
                or not row['history'] or row['history'][-1]['document'] != row['document']
                or row['history'][-1]['recorded_at'] != row['updated_at']):
            raise ValueError('sequence mismatch')
        remember_validation(root, 'strategy_decision', content_hash, closure['files'])
        return deepcopy(row if materialize else stored)
    except (KeyError, TypeError, ValueError):
        raise LedgerError('INTEGRITY_ERROR', '决策索引、过程或不可变原件不一致') from None


class StrategyDecisionsMixin:
    def initial_strategy_evaluated(self, account_id, target_session):
        """Read the first sufficient, verified earlier evaluation, not all proposals.

        Only an authenticated earlier decision enables the narrow fast path.
        A non-future legacy task without a confirming decision takes the former
        full-read compatibility path; that path does not assert that its mutable
        task fields have gained a new integrity proof.
        """
        target = session_date(target_session)

        def read(tx):
            after = None
            while True:
                bounds = {'lt': target}
                if after is not None:
                    bounds['gt'] = after
                selected = tx.select('strategy_decisions', fields=('id', 'target_session'),
                    ranges={'target_session': bounds}, order_by=('target_session',), limit=1)
                if not selected:
                    break
                row = verify_decision_record(self.root,
                    tx.get('strategy_decisions', selected[0]['id']), materialize=False)
                after = row['target_session']
                if row.get('decision_core'):
                    core, _ = read_business_document(self.root, row['decision_core'])
                    if bool(core['calculation'].get('initial_evaluated')):
                        return True
            if tx.select('rebalance_tasks', fields=('id',),
                         ranges={'start_session': {'lte': target}}, limit=1):
                # Compatibility with saved-parent-before-core recovery and old
                # manually created tasks. Preserve the former verification work
                # instead of treating their index presence as a trusted shortcut.
                for stored in tx.list('strategy_decisions'):
                    row = verify_decision_record(self.root, stored)
                    if row.get('decision_core') and row['target_session'] < target:
                        read_business_document(self.root, row['decision_core'])
                return True
            return False

        with verification_scope(self.root, persist=False):
            return self._read(account_id, read)

    def get_strategy_decision(self, account_id, *, decision_id=None, target_session=None):
        if (decision_id is None) == (target_session is None):
            raise LedgerError('INVALID_INPUT', '决策查询须明确一个决策ID或目标日')
        identity = decision_id or decision_identity(account_id, session_date(target_session))
        return self._read(account_id, lambda tx: verify_decision_record(
            self.root, tx.get('strategy_decisions', identity)))

    def list_strategy_decisions(self, account_id, *, start=None, end=None,
                                strategy_version=None, policy_id=None):
        if start is not None:
            session_date(start)
        if end is not None:
            session_date(end)
        def read(tx):
            rows = tx.list('strategy_decisions')
            return [verify_decision_record(self.root, row) for row in rows
                    if (start is None or row['target_session'] >= start)
                    and (end is None or row['target_session'] <= end)
                    and (strategy_version is None or row['strategy_version'] == strategy_version)
                    and (policy_id is None or row['policy_id'] == policy_id)]
        return self._read(account_id, read)

    def prepare_strategy_decision(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        allowed = {'target_session', 'market_as_of', 'input_cutoff', 'input_bundle', 'input_hash',
                   'decision_id', 'expected_sequence', 'state', 'reasons',
                   'required_inputs', 'parent_task_id'}
        if payload.keys() - allowed:
            raise LedgerError('INVALID_INPUT', '决策准备含不支持字段')
        target = session_date(payload['target_session'])
        identity = decision_identity(account_id, target)
        if payload.get('decision_id', identity) != identity:
            raise LedgerError('INVALID_INPUT', '决策ID须由账户和目标日稳定确定')
        bundle = payload['input_bundle']
        if (not isinstance(bundle, dict) or not {'market_snapshot_ids', 'data_evidence',
                                               'account_revision'} <= bundle.keys()
                or type(bundle['account_revision']) is not int
                or not isinstance(bundle['market_snapshot_ids'], list)
                or not isinstance(bundle['data_evidence'], list)
                or bundle['account_revision'] != expected_revision):
            raise LedgerError('INVALID_INPUT', '输入包须绑定当前账户修订和市场来源列表')
        input_hash = digest(bundle)
        if payload.get('input_hash', input_hash) != input_hash:
            raise LedgerError('INTEGRITY_ERROR', '输入包摘要不一致')
        state = payload.get('state', 'PREPARED')
        if state not in {'WAITING_INPUT', 'PREPARED'}:
            raise LedgerError('INVALID_INPUT', '准备状态只能为WAITING_INPUT或PREPARED')

        def apply(tx):
            self._session(target)
            self._before_cutoff(target)
            cutoff = aware_time(payload['input_cutoff'])
            if (cutoff < datetime.fromisoformat(payload['market_as_of'] + 'T18:10:00+08:00')
                    or cutoff > self._now() or payload['market_as_of'] != self._adjacent(
                    'previous_session', target, cutoff=payload['input_cutoff'])):
                raise LedgerError('INVALID_INPUT', '输入截止或行情日期不满足前向时点')
            if not tx.state['simulation_start_session'] or target < tx.state['simulation_start_session']:
                raise LedgerError('INVALID_INPUT', '账户尚未开始观察')
            account = self._account_state_tx(tx, target)
            if not account['can_plan']:
                raise LedgerError('WAITING_DATA', '账户尚不允许规划')
            if bundle.get('account_state_id', account['state_id']) != account['state_id']:
                raise LedgerError('INVALID_INPUT', '输入包账户快照身份不匹配')
            if tx.list('plans', target_session=target):
                raise LedgerError('CONFLICT', '已有冻结计划，不可重新准备')
            policy = self._registered_policy_tx(tx, target, payload['input_cutoff'])
            registered = get_definition(tx.account['strategy_id'], self.settings)
            if any(registered[k] != policy['definition'][k]
                   for k in ['strategy_version', 'implementation_hash', 'uses_model']):
                raise LedgerError('CONFLICT', 'CONFIG_CHANGED：当前策略定义与已生效政策不同')
            prior = verify_decision_record(self.root, tx.get('strategy_decisions', identity))
            _sequence(payload, prior['sequence'] if prior else 0)
            attempts = tx.list('model_attempts', target_session=target)
            if attempts or prior and prior['state'] not in {'WAITING_INPUT', 'PREPARED'}:
                raise LedgerError('CONFLICT', '已有模型占位或最终提案，禁止替换准备输入')
            row = {'decision_id': identity, 'account_id': account_id, 'target_session': target,
                   'state': state, 'market_as_of': payload['market_as_of'],
                   'input_cutoff': payload['input_cutoff'], 'input_hash': input_hash,
                   'input_document': document(self.root, 'strategy-input', bundle),
                   'policy_id': policy['policy_id'], 'policy_hash': policy['policy_hash'],
                   'policy_document': policy['document'], 'strategy_id': tx.account['strategy_id'],
                   'strategy_version': policy['policy']['strategy_version'],
                   'strategy_implementation_hash': policy['definition']['implementation_hash'],
                   'account_revision': tx.state['account_revision'],
                   'account_snapshot_id': account['state_id'],
                   'account_snapshot_hash': digest(account),
                   'account_document': document(self.root, 'strategy-accounts', account),
                   'proposal': None, 'decision_ref': None, 'plan_id': None,
                   'reasons': payload.get('reasons', []), 'model_attempt_ids': [],
                   'required_inputs': payload.get('required_inputs', []),
                   'parent_task_id': payload.get('parent_task_id'),
                   'sequence': prior['sequence'] if prior else 0,
                   'history': prior['history'] if prior else []}
            row = _append_history(self.root, row, tx.now, 'PREPARE')
            tx.save('strategy_decisions', identity, row)
            return {'decision_id': identity, 'decision_state': state, 'sequence': row['sequence'],
                    'input_hash': input_hash, 'policy_id': row['policy_id'], 'policy_hash': row['policy_hash']}
        return self._mutate(account_id, operation_id, {'action': 'prepare_strategy_decision', **payload},
                            expected_revision, target, apply, financial=False)

    def save_strategy_decision(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        allowed = {'decision_id', 'target_session', 'expected_sequence', 'state',
                   'proposal', 'decision_core', 'reasons'}
        if payload.keys() - allowed:
            raise LedgerError('INVALID_INPUT', '保存决策含不支持字段')
        target = session_date(payload['target_session'])
        identity = identifier(payload['decision_id'])
        desired = payload['state']
        if desired not in STATES or desired in {'PREPARED', 'WAITING_INPUT', 'FROZEN'}:
            raise LedgerError('INVALID_INPUT', '此状态必须经准备或正式封存接口产生')

        def apply(tx):
            row = verify_decision_record(self.root, tx.get('strategy_decisions', identity))
            if row is None or row['target_session'] != target:
                raise LedgerError('INVALID_INPUT', '决策未登记或目标日不符')
            _sequence(payload, row['sequence'])
            if row['state'] in TERMINAL:
                raise LedgerError('CONFLICT', '决策已终态，不可改写')
            if desired not in TRANSITIONS[row['state']] | (TERMINAL - {'FROZEN'}):
                raise LedgerError('CONFLICT', '决策状态不能倒退或覆盖最终提案')
            state = desired
            attempts = tx.list('model_attempts', target_session=target)
            if row['account_revision'] != tx.state['account_revision']:
                fixed_task = any(task.get('decision_id') == identity
                                 for task in tx.list('rebalance_tasks'))
                if desired == 'MISSED_CUTOFF':
                    state = 'MISSED_CUTOFF'
                elif (attempts or fixed_task or row['state'] in {'PROPOSED', 'WAITING_VALIDATION'}
                      or desired in TERMINAL):
                    state = 'STALE_INPUT'
                else:
                    raise LedgerError('STALE_ACCOUNT', '财务修订已变更，须重新准备规则输入')
            if desired not in TERMINAL:
                try:
                    self._before_cutoff(target)
                except LedgerError:
                    state = 'MISSED_CUTOFF'
            policy = self._registered_policy_tx(tx, target)
            registered = get_definition(tx.account['strategy_id'], self.settings)
            if (policy['policy_hash'] != row['policy_hash']
                    or registered['implementation_hash'] != row['strategy_implementation_hash']
                    or registered['strategy_version'] != row['strategy_version']):
                state = 'CONFIG_CHANGED'
            if state in {'PROPOSED', 'WAITING_VALIDATION'}:
                proposal, core = payload.get('proposal'), payload.get('decision_core')
                if not isinstance(proposal, dict) or not isinstance(core, dict):
                    raise LedgerError('INVALID_INPUT', '提案状态必须保存完整提案和无自引用决策原件')
                validate_decision_core(proposal, core)
                reference = proposal['decision_ref']
                if (reference['decision_id'] != identity or reference['input_hash'] != row['input_hash']
                        or reference['account_policy_id'] != row['policy_id']
                        or reference['account_policy_hash'] != row['policy_hash']
                        or reference['strategy_implementation_hash'] != row['strategy_implementation_hash']
                        or canonical(core['account_policy']) != canonical(policy['policy'])
                        or proposal['account_revision'] != row['account_revision']
                        or proposal['target_session'] != target or proposal['account_id'] != account_id
                        or proposal['market_as_of'] != row['market_as_of']
                        or aware_time(proposal['input_cutoff']) != aware_time(row['input_cutoff'])):
                    raise LedgerError('INTEGRITY_ERROR', '提案未绑定权威政策和准备输入')
                bound = [a for a in attempts if a.get('decision_id') == identity
                         and a['status'] in {'RECEIVED', 'SUCCEEDED'}
                         and a.get('input_hash') == row['input_hash']]
                if policy['definition']['uses_model'] and not bound:
                    raise LedgerError('CONFLICT', '模型策略缺少本决策的已持久成功响应')
                if row.get('proposal') is not None and canonical(row['proposal']) != canonical(proposal):
                    raise LedgerError('CONFLICT', '已有候选提案不得改换意图')
                core_ref = document(self.root, 'strategy', core)
                if core_ref != reference['decision_core']:
                    raise LedgerError('INTEGRITY_ERROR', '决策原件引用不符')
                document_closure(self.root, {core_ref['path']: {core_ref['content_hash']}})
                # Reuse the public execution validator with this transaction's account
                # view; caller-supplied validation labels cannot authorize PROPOSED.
                from china_stock_sim.common.execution_controls import execution_blocks
                from china_stock_sim.common.plan_validation import DefaultPlanValidator
                from china_stock_sim.common.planning_evidence import collect_planning_market
                from china_stock_sim.common.wire import to_wire

                rules = load_policy_bundle(self.root / 'configs/execution_rules.toml',
                    target_session=target, knowledge_cutoff=proposal['input_cutoff'],
                    execution_policy=self.settings.raw['execution'], fee_policy=self.settings.raw['fees'])
                validation = DefaultPlanValidator().validate_plan(proposal,
                    self._account_state_tx(tx, target), collect_planning_market(self.market, proposal),
                    rules, now=self._now(), decision_core=core)
                if state == 'PROPOSED':
                    if validation['status'] != 'VALID':
                        raise LedgerError('CONFLICT', '提案尚未通过完整执行验证', validation=to_wire(validation))
                    if execution_blocks(self.root / 'configs/execution_rules.toml', rules,
                                        validation['accepted_orders'], target, now=self._now()):
                        raise LedgerError('CONFLICT', '执行规则尚有未解决维护事项')
                    for attempt in bound:
                        response, _ = read_business_document(self.root, attempt['response_document'])
                        if digest(response) != attempt['response_hash']:
                            raise LedgerError('INTEGRITY_ERROR', '持久模型响应摘要不一致')
                        attempt.update(status='SUCCEEDED', finished_at=tx.now,
                            accepted_decision_id=identity,
                            completion_hash=digest({'decision_id': identity, 'proposal': proposal}))
                        tx.save('model_attempts', attempt['attempt_id'], attempt)
                row.update(proposal=proposal, decision_ref=reference, decision_core=core_ref,
                           validation_document=document(self.root, 'strategy-validation', to_wire(validation)),
                           required_inputs=to_wire(validation.get('required_inputs', [])))
            row.update(state=state, reasons=payload.get('reasons', []),
                       model_attempt_ids=[a['id'] for a in attempts])
            row = _append_history(self.root, row, tx.now, 'SAVE')
            tx.save('strategy_decisions', identity, row)
            return {'decision_id': identity, 'decision_state': state, 'sequence': row['sequence']}
        return self._mutate(account_id, operation_id, {'action': 'save_strategy_decision', **payload},
                            expected_revision, target, apply, financial=False)

    def _verify_registered_decision_tx(self, tx, payload, decision):
        if decision is None:
            if tx.list('strategy_decisions', target_session=payload['target_session']):
                raise LedgerError('CONFLICT', '已有策略决策，不能通过MANUAL绕过登记')
            return None
        ref = payload['decision_ref']
        row = verify_decision_record(self.root, tx.get('strategy_decisions', ref['decision_id']),
                                     materialize=False)
        target = payload['target_session']
        if row is None:
            # Pre-schema3 frozen plans retain their own verified evidence. Only
            # exact replay of an existing plan is compatible; never a new plan.
            old = tx.get('plans', payload['plan_id'])
            plan_hash = digest({k: v for k, v in payload.items()
                                if k not in {'expected_revision', 'frozen_at'}})
            if old and old['plan_hash'] == plan_hash:
                return None
        policy = self._registered_policy_tx(tx, target, payload['input_cutoff'])
        binding = decision['freeze_binding']
        proposal = row.get('proposal') if row is not None else None
        if isinstance(proposal, dict) and proposal.get('storage_format') == 'decision-core-proposal-v1':
            proposal_matches = (set(proposal) == {'storage_format', 'source', 'content_hash'}
                and proposal['source'] == row.get('decision_core') == ref['decision_core']
                and proposal['content_hash'] == binding['source_proposal_hash'])
        else:
            proposal_matches = proposal is not None and digest(proposal) == binding['source_proposal_hash']
        if (row is None or row['target_session'] != target or row['state'] not in {'PROPOSED', 'FROZEN'}
                or row['policy_id'] != policy['policy_id'] or row['policy_hash'] != policy['policy_hash']
                or canonical(ref) != canonical(row['decision_ref'])
                or canonical(binding['account_policy']) != canonical(policy['policy'])
                or not proposal_matches):
            raise LedgerError('CONFLICT', 'STRATEGY封存须有匹配的权威政策及PROPOSED决策')
        if row['state'] == 'FROZEN':
            if row['plan_id'] != payload['plan_id']:
                raise LedgerError('CONFLICT', '决策已经绑定其他冻结计划')
            return row
        if row['account_revision'] != tx.state['account_revision']:
            raise LedgerError('STALE_ACCOUNT', '决策输入所绑定的账户修订已变化')
        registered = get_definition(tx.account['strategy_id'], self.settings)
        if (registered['implementation_hash'] != row['strategy_implementation_hash']
                or registered['strategy_version'] != row['strategy_version']):
            raise LedgerError('CONFLICT', 'CONFIG_CHANGED：策略定义已变化，不可封存旧决定')
        return row

    def _freeze_registered_decision_tx(self, tx, row, plan):
        if row is None:
            return
        row.update(state='FROZEN', plan_id=plan['plan_id'], plan_document=plan['document'])
        row = _append_history(self.root, row, tx.now, 'FREEZE')
        tx.save('strategy_decisions', row['decision_id'], row)
