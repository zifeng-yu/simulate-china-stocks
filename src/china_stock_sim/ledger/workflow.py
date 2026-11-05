"""Durable workflow facts; this module never schedules or calls a model."""

from china_stock_sim.ledger.evidence import document, integer
from china_stock_sim.ledger.kernel import LedgerError, digest, identifier
from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import session_date
from china_stock_sim.market.quality import normalize_code


class WorkflowMixin:
    def claim_model_attempt(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        target = payload.get('target_session')

        def apply(tx):
            self._session(target)
            self._before_cutoff(target)
            policy_row = self._account_policy_tx(tx, target)
            policy = policy_row['policy']
            definition = policy_row['definition']
            if (policy_row['definition_status'] != 'REGISTERED'
                    or not isinstance(definition, dict) or definition.get('uses_model') is not True):
                raise LedgerError('INVALID_INPUT', '账户目标日政策未登记模型能力')
            if 'uses_model' in payload or 'definition' in payload:
                raise LedgerError('INVALID_INPUT', '模型能力只能来自持久账户政策')
            model_ref = policy['parameters'].get('model_ref')
            if not isinstance(model_ref, str) or not model_ref.strip():
                raise LedgerError('INTEGRITY_ERROR', '模型政策缺少有效模型引用')
            if 'model_ref' in payload and payload['model_ref'] != model_ref:
                raise LedgerError('INVALID_INPUT', '模型引用与目标日持久政策不符')
            identity = {
                'strategy_id': policy['strategy_id'],
                'strategy_version': policy['strategy_version'],
                'account_policy_id': policy['policy_id'],
                'account_policy_hash': policy_row['policy_hash'],
            }
            if any(key in payload and payload[key] != value for key, value in identity.items()):
                raise LedgerError('INVALID_INPUT', '模型尝试身份与目标日持久政策不符')
            decisions = tx.list('strategy_decisions', target_session=target)
            if len(decisions) > 1:
                raise LedgerError('INTEGRITY_ERROR', '同账户目标日存在重复决策记录')
            decision = decisions[0] if decisions else None
            if decision:
                from china_stock_sim.ledger.strategy_decisions import verify_decision_record
                decision = verify_decision_record(self.root, decision)
                if decision['state'] != 'PREPARED':
                    raise LedgerError('CONFLICT', '已有非准备状态的决策，不可重新调用模型')
                if (payload.get('decision_id') != decision['decision_id']
                        or payload.get('input_hash') != decision['input_hash']
                        or decision['policy_id'] != policy['policy_id']
                        or decision['policy_hash'] != policy_row['policy_hash']):
                    raise LedgerError('CONFLICT', '模型尝试须绑定已准备决策的输入和政策')
                if decision['account_revision'] != tx.state['account_revision']:
                    raise LedgerError('STALE_ACCOUNT', '准备决策的账户输入已失效')
                if 'expected_sequence' in payload and payload['expected_sequence'] != decision['sequence']:
                    raise LedgerError('CONFLICT', '准备决策序列已变化')
            elif payload.get('decision_id') is not None or payload.get('input_hash') is not None:
                raise LedgerError('INVALID_INPUT', '模型尝试引用的准备决策不存在')
            if not tx.state.get('simulation_start_session') or target < tx.state['simulation_start_session']:
                raise LedgerError('INVALID_INPUT', '尚未开始观察')
            if tx.list('plans', target_session=target):
                raise LedgerError('CONFLICT', '已有冻结计划，不可再调用模型')
            attempts = tx.list('model_attempts', target_session=target)
            attempt_limit = policy['parameters'].get('max_attempts_per_target_session', 2)
            if type(attempt_limit) is not int or not 1 <= attempt_limit <= 2:
                raise LedgerError('INTEGRITY_ERROR', '持久政策的模型尝试上限无效')
            if len(attempts) >= attempt_limit:
                raise LedgerError('CONFLICT', f'该账户目标日模型尝试次数已达{attempt_limit}次')
            if attempts and attempts[-1]['status'] not in {'FAILED', 'TIMEOUT', 'INVALID', 'INTERRUPTED'}:
                raise LedgerError('CONFLICT', '已有进行中或成功响应，应先恢复处理，不重问模型')
            if any(attempt.get('account_policy_hash') != policy_row['policy_hash']
                   or attempt.get('decision_id') != (decision['decision_id'] if decision else None)
                   or attempt.get('input_hash') != (decision['input_hash'] if decision else None)
                   for attempt in attempts):
                raise LedgerError('CONFLICT', '同目标日模型尝试不能变更政策或决策输入')
            account = self._account_state_tx(tx, target)
            if not account['can_plan']:
                raise LedgerError('WAITING_DATA', '账户不允许新计划', blocking_reasons=account['blocking_reasons'])
            attempt_id = identifier(payload.get('attempt_id', f'{target}:{len(attempts)+1}'), '尝试ID')
            row = {'attempt_id': attempt_id, 'target_session': target, 'attempt_number': len(attempts)+1,
                   'status': 'CLAIMED', 'claimed_at': tx.now, 'model_ref': model_ref,
                   **identity, 'account_policy': policy,
                   'strategy_definition': definition,
                   'definition_status': policy_row['definition_status'],
                   'account_revision_at_claim': tx.state['account_revision'],
                   'decision_id': decision['decision_id'] if decision else None,
                   'input_hash': decision['input_hash'] if decision else None,
                   'decision_sequence': decision['sequence'] if decision else None,
                   'input_document': decision['input_document'] if decision else None,
                   'request_hash': digest(payload.get('request', {})),
                   'request_document': document(self.root, 'model-requests', payload.get('request', {})),
                   'response_document': None}
            tx.append('model_attempts', attempt_id, row)
            return {'attempt_id': attempt_id, 'attempt_number': row['attempt_number'],
                    'attempt_state': 'CLAIMED', 'model_ref': model_ref, **identity,
                    'decision_id': row['decision_id'], 'input_hash': row['input_hash']}
        return self._mutate(account_id, operation_id, {'action': 'claim_model_attempt', **payload},
                            expected_revision, target, apply, financial=False)

    def finish_model_attempt(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        target = payload.get('target_session')

        def apply(tx):
            row = tx.get('model_attempts', payload['attempt_id'])
            if not row or row['target_session'] != target:
                raise LedgerError('INVALID_INPUT', '模型尝试不存在或日期不符')
            for key in ('decision_id', 'input_hash', 'account_policy_id', 'account_policy_hash',
                        'model_ref', 'account_revision_at_claim'):
                if key in payload and payload[key] != row.get(key):
                    raise LedgerError('CONFLICT', '模型响应不得更换已占位的输入或政策身份')
            if payload['status'] not in {'RECEIVED', 'SUCCEEDED', 'FAILED', 'TIMEOUT', 'INVALID', 'INTERRUPTED'}:
                raise LedgerError('INVALID_INPUT', '模型尝试结果状态无效')
            if payload['status'] == 'SUCCEEDED' and row.get('decision_id'):
                raise LedgerError('CONFLICT', '关联模型须在接受PROPOSED的同一事务确认成功')
            completion_hash = digest(payload)
            if row['status'] not in {'CLAIMED', 'RECEIVED'}:
                if row.get('completion_hash') == completion_hash:
                    return {'state': 'ALREADY_APPLIED', 'attempt_id': row['attempt_id']}
                raise LedgerError('CONFLICT', '模型尝试响应已记录，不可覆盖')
            if row['status'] == 'RECEIVED':
                if payload['status'] not in {'SUCCEEDED', 'INVALID', 'FAILED'}:
                    raise LedgerError('CONFLICT', '已存响应应继续校验，不可重发或退回网络状态')
                if payload.get('response') is not None and digest(payload['response']) != row['response_hash']:
                    raise LedgerError('CONFLICT', '响应原文已持久化，不可替换')
                response = row['response_document']
            elif payload['status'] in {'SUCCEEDED', 'INVALID'}:
                raise LedgerError('CONFLICT', '必须先保存RECEIVED原响应，再记录语义校验结果')
            elif payload['status'] == 'RECEIVED' and payload.get('response') is None:
                raise LedgerError('INVALID_INPUT', '成功响应须先持久化原文')
            else:
                response = document(self.root, 'model-responses', payload.get('response', {}))
                row['response_hash'] = digest(payload.get('response', {}))
            row.update(status=payload['status'], response_document=response, finished_at=tx.now,
                       completion_hash=completion_hash, reason=payload.get('reason'))
            tx.save('model_attempts', row['attempt_id'], row)
            return {'attempt_id': row['attempt_id'], 'attempt_state': row['status'],
                    'response_document': response}
        return self._mutate(account_id, operation_id, {'action': 'finish_model_attempt', **payload},
                            expected_revision, target, apply, financial=False)

    def save_rebalance_task(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        start = payload.get('start_session', payload.get('effective_date'))

        def apply(tx):
            task_id = identifier(payload['task_id'], '调仓任务ID')
            end = session_date(payload['end_session'])
            self._session(start)
            self._session(end)
            self._before_cutoff(start)
            if not tx.state.get('simulation_start_session') or start < tx.state['simulation_start_session']:
                raise LedgerError('INVALID_INPUT', '调仓任务不在观察期')
            calendar = self.market.calendar(start=start, end=end, knowledge_cutoff=self._now().isoformat())
            if not calendar.ready:
                raise LedgerError('WAITING_DATA', '调仓期间交易日历未知')
            sessions = TradingCalendar(calendar.data).between(start, end)
            if not sessions.ready or not 1 <= len(sessions.data) <= 5:
                raise LedgerError('INVALID_INPUT', '调仓任务最长5个实际交易日')
            targets = payload.get('targets', [])
            if not isinstance(targets, list) or not targets:
                raise LedgerError('INVALID_INPUT', '调仓目标不能为空')
            keys = set()
            for target in targets:
                target['code'] = normalize_code(target['code'])
                if target['side'] not in {'BUY', 'SELL'}:
                    raise LedgerError('INVALID_INPUT', '调仓方向无效')
                integer(target['quantity'], minimum=1)
                key = target['code']
                if key in keys:
                    raise LedgerError('INVALID_INPUT', '父任务同一证券只能一个方向')
                keys.add(key)
            task = {**payload, 'task_id': task_id, 'start_session': start, 'end_session': end,
                    'sessions': sessions.data, 'targets': targets, 'frozen_at': tx.now,
                    'status': 'ACTIVE', 'child_plan_ids': [], 'completed_quantities': {}}
            existing = tx.get('rebalance_tasks', task_id)
            if existing:
                if existing.get('input_hash') == digest(payload):
                    return {'state': 'ALREADY_APPLIED', 'task_id': task_id}
                raise LedgerError('CONFLICT', '调仓任务已冻结，不可覆盖目标')
            task['input_hash'] = digest(payload)
            tx.append('rebalance_tasks', task_id, task)
            return {'task_id': task_id, 'sessions': sessions.data}
        return self._mutate(account_id, operation_id, {'action': 'rebalance', **payload},
                            expected_revision, start, apply, financial=False)

    def record_step_result(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        day = payload.get('effective_date', payload.get('target_session'))
        report_ack = payload.get('step') == 'report' and payload.get('status') == 'SUCCEEDED'

        def apply(tx):
            identifier(payload['step'], '步骤')
            if payload['status'] not in {'RUNNING', 'SUCCEEDED', 'FAILED', 'WAITING_DATA', 'INTERRUPTED'}:
                raise LedgerError('INVALID_INPUT', '步骤状态无效')
            # The same credential guard applies to step errors and external model responses.
            proof = document(self.root, 'steps', payload)
            if report_ack:
                revision = integer(payload['rendered_revision'])
                if revision > tx.state['report_revision']:
                    raise LedgerError('INVALID_INPUT', '发布修订不得超过已有报表修订')
                tx.state['last_rendered_revision'] = max(tx.state['last_rendered_revision'], revision)
            identity = identifier(payload.get('step_id', operation_id), '步骤记录ID')
            row = {**payload, 'step_id': identity, 'kind': 'STEP', 'effective_date': day,
                   'processed_at': tx.now, 'document': proof}
            tx.append('job_runs', identity, row)
            return {'step_id': identity, 'step_state': row['status'],
                    'last_rendered_revision': tx.state['last_rendered_revision']}
        return self._mutate(account_id, operation_id, {'action': 'step', **payload}, expected_revision,
                            day, apply, financial=False, report=not report_ack)
