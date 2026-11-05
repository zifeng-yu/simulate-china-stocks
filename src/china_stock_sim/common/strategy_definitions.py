"""Strategy metadata, parameter schemas and current source-file identities.

Reading an explicit source manifest never imports a strategy or calls a model.
Persisted policies retain their historical identity until explicitly superseded.
"""

import hashlib
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path

from china_stock_sim.common.policies import policy_digest

_IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[3]
_SHARED_FILES = (
    'src/china_stock_sim/strategies/base.py',
    'src/china_stock_sim/strategies/features.py',
    'src/china_stock_sim/application/strategy.py',
    'src/china_stock_sim/application/strategy_inputs.py',
    'src/china_stock_sim/common/strategy_definitions.py',
    'src/china_stock_sim/common/strategy_transitions.py',
    'src/china_stock_sim/common/fees.py',
    'src/china_stock_sim/common/sizing.py',
    'src/china_stock_sim/common/policies.py',
    'src/china_stock_sim/common/wire.py',
    'src/china_stock_sim/common/decisions.py',
    'src/china_stock_sim/common/plan_validation.py',
    'src/china_stock_sim/common/planning_evidence.py',
    'src/china_stock_sim/common/execution_controls.py',
    'src/china_stock_sim/domain.py',
)
_RULE_FILES = (
    'src/china_stock_sim/strategies/rules.py',
    'src/china_stock_sim/strategies/sizing.py',
)
_MODEL_FILES = (
    'src/china_stock_sim/strategies/llm.py',
    'src/china_stock_sim/strategies/llm_prompt.py',
    'src/china_stock_sim/strategies/llm_call.py',
    'src/china_stock_sim/models/base.py',
    'src/china_stock_sim/models/audit.py',
    'src/china_stock_sim/models/settings.py',
    'src/china_stock_sim/models/openai_compatible.py',
)
IMPLEMENTATION_FILES = {
    'S1': _SHARED_FILES + _RULE_FILES,
    'S1-LT': _SHARED_FILES + _RULE_FILES,
    'LLM-Daily': _SHARED_FILES + _MODEL_FILES,
}


def _implementation_files(strategy_id):
    files = {}
    root = _IMPLEMENTATION_ROOT.resolve()
    for relative in IMPLEMENTATION_FILES[strategy_id]:
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('当前策略实现文件缺失或路径越界: ' + relative)
        try:
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise ValueError('当前策略实现文件不可读: ' + relative) from None
    return files

_RULE = {
    'lookback_sessions': 121, 'momentum_skip_sessions': 20, 'volatility_sessions': 60,
    'momentum_weight': '0.5', 'low_volatility_weight': '0.5',
    'target_count': 10, 'target_weight': '0.095', 'rebalance': 'month_end',
    'initial_build_when_ready': True, 'rebalance_window_sessions': 5,
    'buy_limit_offset': '0.01', 'sell_limit_offset': '0.01', 'max_daily_symbols': 0,
}
STRATEGY_DEFINITIONS = {
    'S1': {'strategy_version': 'S1-implementation-2', 'uses_model': False, 'defaults': _RULE},
    'S1-LT': {'strategy_version': 'S1-LT-implementation-2', 'uses_model': False,
              'defaults': {**_RULE, 'enter_rank': 10, 'retain_rank': 20,
                           'trim_trigger': '0.12', 'trim_target': '0.095'}},
    'LLM-Daily': {'strategy_version': 'LLM-Daily-implementation-2', 'uses_model': True,
        'defaults': {'model_ref': 'daily_planner', 'max_candidates': 20, 'max_holdings': 5,
                     'max_target_weight': '0.20', 'max_total_target_weight': '0.80',
                     'max_attempts_per_target_session': 2, 'timeout_seconds': 120,
                     'allow_tools': False, 'max_daily_symbols': 5}},
}


def get_definition(strategy_id, settings=None):
    if strategy_id not in STRATEGY_DEFINITIONS:
        raise ValueError('策略尚未登记')
    original = STRATEGY_DEFINITIONS[strategy_id]
    result = deepcopy({k: v for k, v in original.items() if k != 'validator'})
    if (not isinstance(result.get('strategy_version'), str)
            or not result['strategy_version'].strip() or type(result.get('uses_model')) is not bool
            or not isinstance(result.get('defaults'), dict)):
        raise ValueError('策略描述注册无效')
    result['strategy_id'] = strategy_id
    if 'implementation_hash' in original:
        if (not isinstance(original['implementation_hash'], str)
                or re.fullmatch('[0-9a-f]{64}', original['implementation_hash']) is None):
            raise ValueError('显式策略实现摘要须为64位小写SHA256')
        result['implementation_scope'] = original.get('implementation_scope', 'explicit_registered_hash')
    elif strategy_id in IMPLEMENTATION_FILES:
        files = _implementation_files(strategy_id)
        # Reading this module's bytes is not a self-referential hash: the digest
        # is returned to the caller and is never written back into any source.
        result['implementation_hash'] = policy_digest({'definition': result, 'files': files})
        result['implementation_files'] = files
        result['implementation_scope'] = 'current_source_files'
    else:
        result['implementation_hash'] = policy_digest(result)
        result['implementation_scope'] = original.get('implementation_scope', 'registered_descriptor')
    if settings is not None and strategy_id in {'S1', 'S1-LT', 'LLM-Daily'}:
        keys = {'S1': ['s1'], 'S1-LT': ['s1', 's1_lt'], 'LLM-Daily': ['llm']}[strategy_id]
        for key in keys:
            for field, value in settings.raw.get('strategies', {}).get(key, {}).items():
                if field != 'enabled':
                    if field not in result['defaults']:
                        raise ValueError('默认配置含未支持的策略参数')
                    result['defaults'][field] = value
    return result


def normalize_parameters(definition, changes=None):
    changes = {} if changes is None else changes
    defaults = definition['defaults']
    if not isinstance(changes, dict) or set(changes) - set(defaults):
        raise ValueError('策略参数含未知键')
    p = deepcopy({**defaults, **changes})
    policy_digest(p)  # no floats/non-finite numbers/unsupported objects

    def integer(key, lo, hi):
        if type(p[key]) is not int or not lo <= p[key] <= hi:
            raise ValueError(f'策略参数{key}超出支持范围')
        return p[key]

    def ratio(key, lo='0', hi='1'):
        if not isinstance(p[key], str):
            raise ValueError(f'策略参数{key}须为十进制字符串')
        try:
            value = Decimal(p[key])
        except InvalidOperation:
            raise ValueError(f'策略参数{key}无效') from None
        if not value.is_finite() or not Decimal(lo) <= value <= Decimal(hi):
            raise ValueError(f'策略参数{key}超出支持范围')
        return value

    identity = definition['strategy_id']
    if identity in {'S1', 'S1-LT'}:
        window = integer('lookback_sessions', 2, 500)
        skip = integer('momentum_skip_sessions', 0, 498)
        volatility = integer('volatility_sessions', 2, 499)
        count = integer('target_count', 1, 100)
        if window < skip + 2 or volatility + 1 > window:
            raise ValueError('历史窗口不足以支持动量或波动参数')
        if ratio('momentum_weight') + ratio('low_volatility_weight') != 1:
            raise ValueError('两项排名权重之和须为1')
        if not 0 < ratio('target_weight') * count <= 1:
            raise ValueError('目标总权重无效')
        for key in ['buy_limit_offset', 'sell_limit_offset']:
            ratio(key, hi='0.10')
        integer('rebalance_window_sessions', 1, 5)
        if p['rebalance'] != 'month_end' or p['initial_build_when_ready'] is not True:
            raise ValueError('仅支持首次及月末调仓')
        if identity == 'S1-LT':
            enter, retain = integer('enter_rank', 1, 100), integer('retain_rank', 1, 100)
            target, trigger = ratio('trim_target'), ratio('trim_trigger')
            if not count <= enter <= retain or not 0 < target <= trigger or target * count > 1:
                raise ValueError('低换手参数相互矛盾')
    elif identity == 'LLM-Daily':
        integer('max_candidates', 1, 20)
        integer('max_holdings', 1, 100)
        if ratio('max_target_weight') > ratio('max_total_target_weight'):
            raise ValueError('单只权重不得超过总权重')
        integer('max_attempts_per_target_session', 1, 2)
        integer('timeout_seconds', 1, 120)
        if p['allow_tools'] is not False:
            raise ValueError('模型策略不能自主调用工具')
    if 'max_daily_symbols' in p:
        if identity in {'S1', 'S1-LT'} and (defaults.get('max_daily_symbols') == 0
                or definition.get('strategy_version') == identity + '-implementation-2'):
            integer('max_daily_symbols', 0, 0)
        else:
            integer('max_daily_symbols', 1, 5)
    if definition['uses_model']:
        if not isinstance(p.get('model_ref'), str) or not p['model_ref'].strip():
            raise ValueError('模型策略必须指定model_ref')
    validator = STRATEGY_DEFINITIONS[identity].get('validator')
    if validator is not None:
        validator(p)
    return p
