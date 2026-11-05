"""Pure, narrow compatibility contracts for explicitly adopted policy changes.

Only a verified authoritative policy may supply this contract. Mutable progress
is deliberately excluded: children always use the original live parent task.
"""

import re
from copy import deepcopy

from china_stock_sim.common.policies import policy_digest

TRANSITION_KIND = 'DAILY_SYMBOL_LIMIT_SCOPE_V1'
MAINTENANCE_KIND = 'IMPLEMENTATION_MAINTENANCE'
MAINTENANCE_FILES = frozenset({
    'src/china_stock_sim/common/wire.py',
    'src/china_stock_sim/application/strategy.py',
    'src/china_stock_sim/application/strategy_inputs.py',
    'src/china_stock_sim/common/strategy_transitions.py',
})
_MUTABLE_TASK_FIELDS = {'id', 'account_id', 'completed_quantities', 'child_plan_ids',
                        'status', 'remaining_targets', 'reason', 'policy_transition'}


def task_contract_hash(task):
    return policy_digest({k: v for k, v in task.items() if k not in _MUTABLE_TASK_FIELDS})


def compatible_parameters(strategy_id, before, after):
    if strategy_id in {'S1', 'S1-LT'}:
        return (type(before.get('max_daily_symbols')) is int
                and before['max_daily_symbols'] == 5
                and type(after.get('max_daily_symbols')) is int
                and after['max_daily_symbols'] == 0
                and {**before, 'max_daily_symbols': 0} == after)
    return strategy_id == 'LLM-Daily' and before == after


def maintenance_definition_matches(before, after):
    """Only the declared shared maintenance files may change; algorithms stay fixed."""
    fields = {'implementation_hash', 'implementation_files'}
    old_files, new_files = before.get('implementation_files'), after.get('implementation_files')
    if (before.get('implementation_scope') != 'current_source_files'
            or after.get('implementation_scope') != 'current_source_files'
            or {k: v for k, v in before.items() if k not in fields | {'defaults'}}
                != {k: v for k, v in after.items() if k not in fields | {'defaults'}}
            or not isinstance(old_files, dict) or not isinstance(new_files, dict)
            or set(old_files) != set(new_files) or not old_files
            or any(not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None
                   for value in [*old_files.values(), *new_files.values(),
                                 before.get('implementation_hash'), after.get('implementation_hash')])
            or before['implementation_hash'] == after['implementation_hash']):
        return False
    changed = {name for name in old_files if old_files[name] != new_files[name]}
    return bool(changed) and changed <= MAINTENANCE_FILES


def verify_transition_contract(row, previous, tasks, *, previous_transition=None):
    """Validate saved cross-record links without using today's code/config defaults."""
    transition = row['transition']
    required = {'kind', 'from_policy_id', 'from_policy_hash', 'from_policy_document',
                'to_policy_id', 'to_policy_hash', 'effective_session', 'recorded_at', 'tasks'}
    maintenance = isinstance(transition, dict) and transition.get('kind') == MAINTENANCE_KIND
    if maintenance:
        required.add('parameters_hash')
    if not isinstance(transition, dict) or set(transition) != required:
        raise ValueError('政策衔接字段不完整或含未知键')
    old, new = previous['policy'], row['policy']
    strategy = new['strategy_id']
    if (transition['kind'] not in {TRANSITION_KIND, MAINTENANCE_KIND}
            or transition['from_policy_id'] != previous['policy_id']
            or transition['from_policy_hash'] != previous['policy_hash']
            or transition['from_policy_document'] != previous['document']
            or transition['to_policy_id'] != row['policy_id']
            or transition['to_policy_hash'] != row['policy_hash']
            or transition['effective_session'] != row['effective_session']
            or transition['recorded_at'] != new['known_at']
            or type(previous['policy_revision']) is not int or type(row['policy_revision']) is not int
            or previous['policy_revision'] + 1 != row['policy_revision']
            or old['account_id'] != new['account_id'] or old['strategy_id'] != strategy
            or previous['definition'].get('uses_model') != (strategy == 'LLM-Daily')
            or row['definition'].get('uses_model') != (strategy == 'LLM-Daily')
            or previous['display_name'] != row['display_name']):
        raise ValueError('政策衔接不符合限定的身份或参数升级')
    if maintenance:
        if (old['strategy_version'] != new['strategy_version']
                or new['strategy_version'] != strategy + '-implementation-2'
                or previous['definition_status'] != 'REGISTERED' or row['definition_status'] != 'REGISTERED'
                or row['definition'].get('strategy_id') != strategy
                or row['definition'].get('strategy_version') != new['strategy_version']
                or old['parameters'] != new['parameters']
                or transition['parameters_hash'] != policy_digest(new['parameters'])
                or not maintenance_definition_matches(previous['definition'], row['definition'])):
            raise ValueError('维护衔接只允许同参数、同版本和指定实现文件改变')
    elif (old['strategy_version'] != strategy + '-implementation-1'
            or new['strategy_version'] != strategy + '-implementation-2'
            or not compatible_parameters(strategy, old['parameters'], new['parameters'])):
        raise ValueError('政策衔接不符合限定的身份或参数升级')
    bindings = transition['tasks']
    if not isinstance(bindings, list) or strategy == 'LLM-Daily' and bindings:
        raise ValueError('模型策略不能衔接调仓父任务')
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {'task_id', 'immutable_contract_hash'}:
            raise ValueError('父任务衔接引用无效')
        identity = binding['task_id']
        if identity in seen:
            raise ValueError('父任务衔接引用重复')
        seen.add(identity)
        task = tasks[identity]
        direct = (task.get('policy_hash') == previous['policy_hash']
                  and task.get('strategy_implementation_hash') == previous['definition']['implementation_hash']
                  and task.get('parameters') == old['parameters'])
        inherited = (maintenance and previous_transition is not None
                     and previous_transition.get('to_policy_id') == previous['policy_id']
                     and previous_transition.get('to_policy_hash') == previous['policy_hash']
                     and binding in previous_transition.get('tasks', []))
        if (task.get('account_id', new['account_id']) != new['account_id']
                or not (direct or inherited)
                or binding['immutable_contract_hash'] != task_contract_hash(task)
                or task['end_session'] < row['effective_session']):
            raise ValueError('原父任务身份、目标或截止已改变')
    return deepcopy(transition)


def verify_transition_chain(row, policies, tasks):
    """Validate every predecessor before permitting inherited parent bindings.

    Callers must authenticate policy originals first. The return is a verified
    projection, never a certificate based on a predecessor's self-reported tasks.
    """
    chain, seen = [], set()
    current = row
    while 'transition' in current:
        identity = current['policy_id']
        if identity in seen:
            raise ValueError('政策衔接链存在循环')
        seen.add(identity)
        previous = policies[current['transition']['from_policy_id']]
        if (type(current['policy_revision']) is not int
                or type(previous['policy_revision']) is not int
                or previous['policy_revision'] != current['policy_revision'] - 1):
            raise ValueError('政策衔接修订必须逐层递减')
        chain.append((current, previous))
        current = previous
    proof = None
    for current, previous in reversed(chain):
        proof = verify_transition_contract(current, previous, tasks, previous_transition=proof)
    return proof


def parent_transition_matches(task, policy, *, target_session):
    """Pure second check of the verified projection in frozen strategy inputs."""
    proof = task.get('policy_transition', {})
    compatible = (compatible_parameters(policy.get('strategy_id'), task['parameters'], policy['parameters'])
                  if proof.get('kind') == TRANSITION_KIND else
                  proof.get('kind') == MAINTENANCE_KIND
                  and proof.get('parameters_hash') == policy_digest(policy['parameters']))
    return (proof.get('kind') in {TRANSITION_KIND, MAINTENANCE_KIND}
            and proof.get('to_policy_id') == policy.get('policy_id')
            and proof.get('to_policy_hash') == policy_digest(policy)
            and proof.get('effective_session', '9999') <= target_session
            and target_session in task['sessions']
            and {'task_id': task['task_id'], 'immutable_contract_hash': task_contract_hash(task)}
                in proof.get('tasks', [])
            and compatible)
