"""Authoritative account creation and immutable, future-effective policy records."""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from zoneinfo import ZoneInfo

from china_stock_sim.common.strategy_definitions import (
    STRATEGY_DEFINITIONS,
    get_definition,
    normalize_parameters,
)
from china_stock_sim.common.strategy_transitions import (
    MAINTENANCE_KIND,
    TRANSITION_KIND,
    compatible_parameters,
    maintenance_definition_matches,
    task_contract_hash,
    verify_transition_chain,
)
from china_stock_sim.config import MAX_CASH_FEN, valid_account_label
from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import (
    LedgerError,
    canonical,
    digest,
    identifier,
    ledger_lock,
    reject_credentials,
)
from china_stock_sim.ledger.queries import rebalance_views
from china_stock_sim.ledger.schema import check_schema, initial_state, validate_accounts
from china_stock_sim.market.contracts import aware_time, session_date


def _definition(settings, strategy_id, *, allow_unknown=False):
    try:
        definition = get_definition(strategy_id, settings)
    except ValueError:
        if not allow_unknown or strategy_id in STRATEGY_DEFINITIONS:
            raise
        return {'strategy_id': strategy_id, 'strategy_version': 'UNREGISTERED',
                'uses_model': None, 'defaults': {}, 'implementation_hash': None,
                'implementation_scope': 'unregistered'}, 'UNREGISTERED'
    return definition, 'REGISTERED'


def _policy_row(root, account, definition, parameters, revision, effective, recorded_at,
                display_name, reason, status='REGISTERED', transition=None):
    identity = 'policy:' + digest({'account_id': account['account_id'], 'revision': revision})
    policy = {'policy_id': identity, 'account_id': account['account_id'],
              'strategy_id': account['strategy_id'],
              'strategy_version': definition['strategy_version'], 'effective_from': effective,
              'effective_to': None, 'known_at': recorded_at, 'parameters': deepcopy(parameters)}
    public_definition = {k: v for k, v in definition.items() if k != 'defaults'}
    body = {'policy': policy, 'policy_revision': revision, 'definition': public_definition,
            'definition_status': status, 'display_name': display_name, 'reason': reason}
    if transition is not None:
        body['transition'] = {**deepcopy(transition), 'to_policy_id': identity,
            'to_policy_hash': digest(policy), 'effective_session': effective, 'recorded_at': recorded_at}
    reject_credentials(body)
    return {**body, 'policy_id': identity, 'effective_session': effective,
            'policy_hash': digest(policy), 'document': document(root, 'policies', body)}


def initialize_base_policy(db, *, root, settings, account, recorded_at):
    """Insert one base row and immutable original inside the caller's transaction."""
    account = dict(account)
    definition, status = _definition(settings, account['strategy_id'], allow_unknown=True)
    parameters = normalize_parameters(definition) if status == 'REGISTERED' else {}
    row = _policy_row(root, account, definition, parameters, 0, None, recorded_at,
                      account['account_id'], 'INITIAL_POLICY', status)
    db.execute('INSERT INTO account_policies(id,account_id,data) VALUES(?,?,?)',
               (row['policy_id'], account['account_id'], canonical(row)))
    return row


def verified_policy(root, row):
    """The DB index must agree with its original; a matching file alone is insufficient."""
    try:
        body, _ = read_business_document(root, row['document'])
        expected = {k: row[k] for k in ['policy', 'policy_revision', 'definition',
                                        'definition_status', 'display_name', 'reason']}
        if 'transition' in row:
            expected['transition'] = row['transition']
        policy = row['policy']
        if (canonical(body) != canonical(expected) or row['policy_hash'] != digest(policy)
                or row['policy_id'] != policy['policy_id']
                or row.get('id', row['policy_id']) != row['policy_id']
                or row.get('account_id', policy['account_id']) != policy['account_id']
                or row['effective_session'] != policy['effective_from']
                or policy['effective_to'] is not None):
            raise ValueError('index mismatch')
        return deepcopy(row)
    except (KeyError, TypeError, ValueError):
        raise LedgerError('INTEGRITY_ERROR', '账户政策索引与不可变原件不一致') from None


class AccountPoliciesMixin:
    def _account_policy_tx(self, tx, target):
        target = session_date(target)
        rows = tx.list('account_policies')
        if not rows:
            raise LedgerError('INTEGRITY_ERROR', '账户缺少初始政策')
        applicable = [row for row in rows if row['effective_session'] is None
                      or row['effective_session'] <= target]
        if not applicable:
            raise LedgerError('INTEGRITY_ERROR', '账户缺少目标日适用政策')
        row = max(applicable, key=lambda p: (p['effective_session'] or '', p['policy_revision']))
        result = verified_policy(self.root, row)
        if result['policy']['strategy_id'] != tx.account['strategy_id']:
            raise LedgerError('INTEGRITY_ERROR', '政策策略与持久账户身份不一致')
        self._verify_policy_transition_tx(tx, result)
        return result

    def _verify_policy_transition_tx(self, tx, row):
        if 'transition' not in row:
            return None
        try:
            policies, seen, current = {row['policy_id']: row}, set(), row
            while 'transition' in current:
                if current['policy_id'] in seen:
                    raise ValueError('政策衔接链存在循环')
                seen.add(current['policy_id'])
                old = verified_policy(self.root, tx.get('account_policies',
                    current['transition']['from_policy_id']))
                if (type(current['policy_revision']) is not int
                        or type(old['policy_revision']) is not int
                        or old['policy_revision'] != current['policy_revision'] - 1):
                    raise ValueError('政策衔接修订必须逐层递减')
                policies[old['policy_id']] = old
                current = old
            return verify_transition_chain(row, policies,
                {task['task_id']: task for task in tx.list('rebalance_tasks')})
        except (KeyError, TypeError, ValueError):
            raise LedgerError('INTEGRITY_ERROR', '政策衔接引用与原父任务不一致') from None

    def _rebalance_policy_tx(self, tx, task, target):
        row = self._account_policy_tx(tx, target)
        if (task.get('policy_hash') == row['policy_hash']
                and task.get('strategy_implementation_hash') == row['definition']['implementation_hash']
                and task.get('parameters') == row['policy']['parameters']):
            return None
        proof = self._verify_policy_transition_tx(tx, row)
        binding = {'task_id': task['task_id'], 'immutable_contract_hash': task_contract_hash(task)}
        if (proof is None or binding not in proof['tasks'] or target not in task['sessions']
                or target < row['effective_session']):
            raise LedgerError('CONFLICT', '父任务未获得目标日政策的明确兼容衔接')
        return proof

    def get_rebalance_policy_compatibility(self, account_id, task_id, target_session):
        """Read-only check; its projection is revalidated in the freeze transaction."""
        target = session_date(target_session)

        def read(tx):
            task = tx.get('rebalance_tasks', task_id)
            if task is None:
                raise LedgerError('INVALID_INPUT', '调仓父任务不存在')
            return {'state': 'READY', 'task_id': task_id,
                    'transition': self._rebalance_policy_tx(tx, task, target)}
        return self._read(account_id, read)

    def get_account_policy(self, account_id, target_session):
        return self._read(account_id, lambda tx: self._account_policy_tx(tx, target_session))

    def account_policies(self, account_id):
        return self._read(account_id, lambda tx: [verified_policy(self.root, r)
                                                 for r in tx.list('account_policies')])

    def create_account(self, payload, *, operation_id):
        payload = self._payload(payload)
        reject_credentials(payload)
        identifier(operation_id, '操作ID')
        required = {'account_id', 'initial_cash_fen', 'strategy_id'}
        allowed = required | {'display_name', 'strategy_parameters', 'model_ref'}
        if not required <= payload.keys() or payload.keys() - allowed:
            raise LedgerError('INVALID_INPUT', '开户字段缺失或含不支持字段')
        account_id, strategy = payload['account_id'], payload['strategy_id']
        name = payload.get('display_name', account_id)
        if any(not valid_account_label(v) for v in [account_id, strategy, name]):
            raise LedgerError('INVALID_INPUT', '账户、策略及展示名称须为合法非空文字')
        principal = payload['initial_cash_fen']
        if type(principal) is not int or not 0 < principal <= MAX_CASH_FEN:
            raise LedgerError('INVALID_INPUT', '开户本金须为范围内正整数分')
        changes = deepcopy(payload.get('strategy_parameters', {}))
        if not isinstance(changes, dict):
            raise LedgerError('INVALID_INPUT', '策略参数须为对象')
        if 'model_ref' in payload:
            if 'model_ref' in changes and changes['model_ref'] != payload['model_ref']:
                raise LedgerError('INVALID_INPUT', '模型引用相互矛盾')
            changes['model_ref'] = payload['model_ref']
        request_hash = digest({'account_id': account_id, 'strategy_id': strategy,
                               'initial_cash_fen': principal, 'display_name': name,
                               'strategy_parameters': changes})
        with ledger_lock(self.root), closing(sqlite3.connect(
                self.settings.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                check_schema(db)
                validate_accounts(db, self.settings)
                old = db.execute('SELECT * FROM accounts WHERE account_id=?', (account_id,)).fetchone()
                if old:
                    state = json.loads(db.execute('SELECT data FROM account_state WHERE account_id=?',
                                                  (account_id,)).fetchone()[0])
                    op = db.execute('SELECT * FROM operations WHERE account_id=? AND operation_id=?',
                                     (account_id, operation_id)).fetchone()
                    if state.get('account_creation_request_hash') == request_hash:
                        if op and op['payload_hash'] != state.get('account_creation_hash'):
                            raise LedgerError('CONFLICT', '操作ID已用于其他业务')
                        saved = json.loads(op['result']) if op else {'account_id': account_id}
                        db.rollback()
                        return {**saved, 'state': 'ALREADY_APPLIED', 'account_id': account_id}
                definition, _ = _definition(self.settings, strategy)
                if 'model_ref' in changes and not definition['uses_model']:
                    raise LedgerError('INVALID_INPUT', '非模型策略不能配置模型')
                parameters = normalize_parameters(definition, changes)
                business = {'account_id': account_id, 'strategy_id': strategy,
                            'initial_cash_fen': principal, 'display_name': name,
                            'parameters': parameters, 'definition': definition}
                payload_hash = digest(business)
                if old:
                    op = db.execute('SELECT * FROM operations WHERE account_id=? AND operation_id=?',
                                     (account_id, operation_id)).fetchone()
                    state = json.loads(db.execute('SELECT data FROM account_state WHERE account_id=?',
                                                  (account_id,)).fetchone()[0])
                    if (op and op['payload_hash'] != payload_hash
                            or state.get('account_creation_hash') != payload_hash):
                        raise LedgerError('CONFLICT', '账户已存在且开户内容不同')
                    saved = json.loads(op['result']) if op else {'account_id': account_id}
                    db.rollback()
                    return {**saved, 'state': 'ALREADY_APPLIED', 'account_id': account_id}
                now = self._now().isoformat()
                account = {'account_id': account_id, 'strategy_id': strategy, 'currency': 'CNY',
                           'initial_cash_fen': principal, 'created_at': now}
                row = _policy_row(self.root, account, definition, parameters, 0, None, now,
                                  name, 'ACCOUNT_CREATED')
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',
                           (account_id, strategy, 'CNY', principal, now))
                db.execute('''INSERT INTO cash_entries(entry_id,account_id,kind,amount_fen,recorded_at)
                    VALUES(?,?,'INITIAL_CAPITAL',?,?)''', ('initial:'+account_id, account_id, principal, now))
                state = {**initial_state(), 'display_name': name, 'account_creation_hash': payload_hash,
                         'account_creation_request_hash': request_hash}
                db.execute('INSERT INTO account_state VALUES(?,?)', (account_id, canonical(state)))
                db.execute('INSERT INTO account_policies VALUES(?,?,?)',
                           (row['policy_id'], account_id, canonical(row)))
                result = {'state': 'APPLIED', 'account_id': account_id, 'policy_id': row['policy_id'],
                          'initial_cash_fen': principal, 'account_revision': 0,
                          'report_revision': 0, 'simulation_start_session': None,
                          'operation_id': operation_id, 'processed_at': now}
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',
                           (account_id, operation_id, payload_hash, canonical(result), now))
                validate_accounts(db, self.settings)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def schedule_account_policy(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        allowed = {'effective_session', 'strategy_parameters', 'model_ref', 'display_name', 'reason',
                   'transition'}
        if payload.keys() - allowed or not {'effective_session', 'reason'} <= payload.keys():
            raise LedgerError('INVALID_INPUT', '政策更新字段无效；必须明确生效日与原因')
        if 'transition' in payload and not isinstance(payload['transition'], dict):
            raise LedgerError('INVALID_INPUT', '政策衔接须为明确对象')
        target = session_date(payload['effective_session'])
        reason = identifier(payload['reason'], '政策修改理由')

        def apply(tx):
            self._session(target)
            self._before_cutoff(target)
            transition_request = payload.get('transition')
            pending = [o for o in tx.list('orders') if o['status'] in {'PENDING', 'WAITING_DATA'}]
            active = [t for t in rebalance_views(tx, self._now())
                      if t['status'] not in {'COMPLETE', 'EXPIRED'}]
            if transition_request is None and pending:
                raise LedgerError('CONFLICT', '存在未决订单，不可修改政策')
            if transition_request is None and active:
                raise LedgerError('CONFLICT', '存在进行中调仓任务，不可修改政策')
            for table in ['strategy_decisions', 'model_attempts', 'plans']:
                if any(row['target_session'] >= target for row in tx.list(table)):
                    raise LedgerError('CONFLICT', '生效区间已有决策、模型尝试或计划，不可改政策')
            policies = tx.list('account_policies')
            if any(p['effective_session'] == target for p in policies):
                raise LedgerError('CONFLICT', '同一生效日已有不可变政策')
            old = self._account_policy_tx(tx, target)
            definition, _ = _definition(self.settings, tx.account['strategy_id'])
            changes = deepcopy(payload.get('strategy_parameters', {}))
            if not isinstance(changes, dict):
                raise LedgerError('INVALID_INPUT', '策略参数须为对象')
            if 'model_ref' in payload:
                if not definition['uses_model']:
                    raise LedgerError('INVALID_INPUT', '非模型策略不能配置模型')
                if 'model_ref' in changes and changes['model_ref'] != payload['model_ref']:
                    raise LedgerError('INVALID_INPUT', '模型引用相互矛盾')
                changes['model_ref'] = payload['model_ref']
            # Defaults from registration do not overwrite explicit saved account choices.
            parameters = normalize_parameters(definition, {**old['policy']['parameters'], **changes})
            name = payload.get('display_name', old['display_name'])
            if not valid_account_label(name):
                raise LedgerError('INVALID_INPUT', '展示名称无效')
            transition = None
            if transition_request is not None:
                if (not isinstance(transition_request, dict)
                        or set(transition_request) != {'kind', 'from_policy_id', 'from_policy_hash'}
                        or transition_request.get('kind') not in {TRANSITION_KIND, MAINTENANCE_KIND}):
                    raise LedgerError('INVALID_INPUT', '未知或不完整的政策衔接请求')
                latest = max(policies, key=lambda r: r['policy_revision'])
                if (old['policy_id'] != latest['policy_id']
                        or transition_request['from_policy_id'] != latest['policy_id']
                        or transition_request['from_policy_hash'] != latest['policy_hash']):
                    raise LedgerError('CONFLICT', '政策衔接的原政策CAS已失效')
                if (target <= self._now().astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                        or any(o['target_session'] >= target for o in pending)):
                    raise LedgerError('CONFLICT', '衔接仅允许未来生效且旧未决订单严格早于生效日')
                strategy = tx.account['strategy_id']
                maintenance = transition_request['kind'] == MAINTENANCE_KIND
                if (self.settings.raw['execution'].get('daily_symbol_limit_scope') != 'LLM_ONLY'
                        or old['definition'].get('uses_model') != definition['uses_model']
                        or name != old['display_name'] or 'model_ref' in payload
                        or strategy == 'LLM-Daily' and active):
                    raise LedgerError('INVALID_INPUT', '衔接只接受每日只数限制与共享实现身份的指定升级')
                if maintenance:
                    if (old['policy']['strategy_version'] != strategy + '-implementation-2'
                            or old['policy']['parameters'] != parameters
                            or not maintenance_definition_matches(old['definition'], definition)):
                        raise LedgerError('INVALID_INPUT', '维护衔接只允许同参数、同版本和指定实现文件改变')
                elif (old['policy']['strategy_version'] != strategy + '-implementation-1'
                        or definition['strategy_version'] != strategy + '-implementation-2'
                        or not compatible_parameters(strategy, old['policy']['parameters'], parameters)):
                    raise LedgerError('INVALID_INPUT', '衔接只接受每日只数限制与共享实现身份的指定升级')
                old_proof = self._verify_policy_transition_tx(tx, old) if maintenance else None
                bindings = []
                for task in active:
                    binding = {'task_id': task['task_id'],
                               'immutable_contract_hash': task_contract_hash(task)}
                    direct = (task.get('policy_hash') == old['policy_hash']
                              and task.get('strategy_implementation_hash') == old['definition']['implementation_hash']
                              and task.get('parameters') == old['policy']['parameters'])
                    inherited = maintenance and old_proof is not None and binding in old_proof['tasks']
                    if (not (direct or inherited) or task['end_session'] < target):
                        raise LedgerError('CONFLICT', '原父任务身份或剩余窗口无法兼容衔接')
                    bindings.append(binding)
                transition = {**transition_request, 'from_policy_document': old['document'],
                              'tasks': bindings}
                if maintenance:
                    transition['parameters_hash'] = digest(parameters)
            revision = max(row['policy_revision'] for row in policies) + 1
            row = _policy_row(self.root, tx.account, definition, parameters, revision, target,
                              tx.now, name, reason, transition=transition)
            tx.append('account_policies', row['policy_id'], row)
            return {'policy_id': row['policy_id'], 'policy_hash': row['policy_hash'],
                    'policy_revision': revision, 'effective_session': target}
        return self._mutate(account_id, operation_id, {'action': 'schedule_account_policy', **payload},
                            expected_revision, target, apply, financial=False)

    def _registered_policy_tx(self, tx, target, cutoff=None):
        row = self._account_policy_tx(tx, target)
        if row['definition_status'] != 'REGISTERED':
            raise LedgerError('INVALID_INPUT', '账户策略尚未登记，禁止规划')
        if cutoff is not None and aware_time(row['policy']['known_at']) > aware_time(cutoff):
            raise LedgerError('INVALID_INPUT', '账户政策晚于决策知识截止')
        return row
