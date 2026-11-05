"""Versioned forward policy snapshots and deterministic main-board contexts.

This module consumes already verified status evidence. It neither queries sources
nor declares that a caller-supplied security status has been proved by that source.
"""

import hashlib
import json
import re
import tomllib
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .fees import (
    ExecutionSupportError,
    aware_time,
    integer,
    number,
    session_date,
    validate_fee_policy,
)

PARSER_ID = 'main_board_forward_2026_v1'
COVERAGE_MODE = 'KNOWN_ONLY_COVERAGE'
MAX_ORDER_SHARES = 1_000_000
NO_LIMIT_CONDITIONS = (
    'IPO_FIRST_FIVE_SESSIONS', 'RELISTING_FIRST_SESSION', 'DELISTING_FIRST_SESSION',
)
_EXECUTION_CONSTANTS = {
    'model': 'NEXT_SESSION_DAILY_OPEN_V1', 'max_daily_symbols_per_account': 5,
    'one_direction_per_symbol_per_day': True, 'buy_lot_size': 100,
    't_plus_one': True, 'use_same_open_sale_proceeds': False,
    'reject_limit_up_buy': True, 'reject_limit_down_sell': True,
    'order_validity': 'target_session_only', 'partial_fills': False, 'slippage_bps': 0,
}


def _json_value(value):
    if isinstance(value, float):
        raise ExecutionSupportError('INVALID_INPUT', '政策与证据不接受float')
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ExecutionSupportError('INVALID_INPUT', '政策数值必须有限')
        return format(value, 'f')
    if isinstance(value, datetime):
        return aware_time(value).astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ExecutionSupportError('INVALID_INPUT', '政策字段名必须为字符串')
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ExecutionSupportError('INVALID_INPUT', '政策或证据包含不支持类型')


def policy_digest(value):
    encoded = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _required(mapping, fields, label):
    if not isinstance(mapping, dict) or not set(fields) <= mapping.keys():
        raise ExecutionSupportError('INVALID_INPUT', f'{label}缺少必要字段')


def validate_execution_policy(policy):
    _required(policy, {*_EXECUTION_CONSTANTS, 'price_tick_yuan'}, '执行政策')
    allowed = {*_EXECUTION_CONSTANTS, 'price_tick_yuan', 'enabled', 'max_order_shares',
               'daily_symbol_limit_scope'}
    if set(policy) - allowed:
        raise ExecutionSupportError('INVALID_INPUT', '执行政策含未支持的参数',
                                    fields=sorted(set(policy) - allowed))
    for key, expected in _EXECUTION_CONSTANTS.items():
        if type(policy[key]) is not type(expected) or policy[key] != expected:
            raise ExecutionSupportError('INVALID_INPUT', '执行配置与当前支持模型矛盾', field=key)
    if number(policy['price_tick_yuan'], positive=True) != Decimal('0.01'):
        raise ExecutionSupportError('INVALID_INPUT', '当前主板价位必须为0.01')
    if 'enabled' in policy and type(policy['enabled']) is not bool:
        raise ExecutionSupportError('INVALID_INPUT', 'enabled必须为布尔值')
    if 'max_order_shares' in policy and integer(policy['max_order_shares']) != MAX_ORDER_SHARES:
        raise ExecutionSupportError('INVALID_INPUT', '当前普通主板单笔上限必须为100万股')
    scope = policy.get('daily_symbol_limit_scope', 'ALL_ACCOUNTS')
    if not isinstance(scope, str) or scope not in {'ALL_ACCOUNTS', 'LLM_ONLY'}:
        raise ExecutionSupportError('INVALID_INPUT', '每日代码限制范围无效')
    return _json_value(policy)


def daily_symbol_limit(policy, strategy_id):
    """Interpret the retained policy without adding defaults to historical bytes."""
    verified = validate_execution_policy(policy)
    if (verified.get('daily_symbol_limit_scope', 'ALL_ACCOUNTS') == 'LLM_ONLY'
            and strategy_id in {'S1', 'S1-LT'}):
        return None
    return verified['max_daily_symbols_per_account']


def _interval(item, target, cutoff, label):
    _required(item, ('effective_from', 'known_at'), label)
    start = session_date(item['effective_from'])
    end = session_date(item['effective_to']) if item.get('effective_to') is not None else None
    if end and end < start:
        raise ExecutionSupportError('INVALID_INPUT', f'{label}生效区间倒置')
    known = aware_time(item['known_at'])
    return start <= target and (end is None or target <= end) and known <= cutoff


def _validate_rule(rule):
    _required(rule, ('rule_id', 'exchange', 'board', 'effective_from', 'known_at',
                     'source', 'source_clauses', 'price_tick', 'buy_lot_size',
                     'max_order_shares', 'limit_rate', 'risk_warning_limit_rate',
                     'ipo_unlimited_sessions', 'no_price_limit_conditions'), '交易规则')
    if rule['exchange'] not in ('SSE', 'SZSE') or rule['board'] != rule['exchange'] + '_MAIN_A':
        raise ExecutionSupportError('INVALID_INPUT', '规则仅支持沪深普通主板A股')
    if session_date(rule['effective_from']) < '2026-07-06':
        raise ExecutionSupportError('INVALID_INPUT', '当前规则解析器不覆盖2026-07-06之前')
    for field in ('rule_id', 'source'):
        if not isinstance(rule[field], str) or not rule[field].strip():
            raise ExecutionSupportError('INVALID_INPUT', '规则身份或来源为空', field=field)
    if not isinstance(rule['source_clauses'], list) or not rule['source_clauses'] or any(
            not isinstance(item, str) or not item for item in rule['source_clauses']):
        raise ExecutionSupportError('INVALID_INPUT', '交易规则须保存来源条款')
    if (number(rule['price_tick'], positive=True) != Decimal('0.01')
            or integer(rule['buy_lot_size']) != 100
            or integer(rule['max_order_shares']) != MAX_ORDER_SHARES
            or integer(rule['ipo_unlimited_sessions']) != 5
            or number(rule['limit_rate']) != Decimal('0.10')
            or number(rule['risk_warning_limit_rate']) != Decimal('0.10')
            or rule['no_price_limit_conditions'] != list(NO_LIMIT_CONDITIONS)):
        raise ExecutionSupportError('INVALID_INPUT', '交易规则与当前解析器支持范围矛盾')


def build_policy_bundle(registry, execution_policy, fee_policy, *, target_session, knowledge_cutoff):
    """Freeze only rules effective on N and available at Kp; intervals are inclusive."""
    _required(registry, ('schema_version', 'parser_id', 'coverage_mode', 'rules',
                         'fee_policy_metadata'), '规则登记')
    if (type(registry['schema_version']) is not int or registry['schema_version'] != 1
            or registry['parser_id'] != PARSER_ID or registry['coverage_mode'] != COVERAGE_MODE):
        raise ExecutionSupportError('INVALID_INPUT', '不支持的规则登记版本或覆盖契约')
    target, cutoff = session_date(target_session), aware_time(knowledge_cutoff)
    execution = validate_execution_policy(execution_policy)
    if not isinstance(registry['rules'], list):
        raise ExecutionSupportError('INVALID_INPUT', '规则登记须为列表')
    selected, identities = [], set()
    for rule in registry['rules']:
        _validate_rule(rule)
        if rule['rule_id'] in identities:
            raise ExecutionSupportError('INVALID_INPUT', '规则ID重复')
        identities.add(rule['rule_id'])
        if _interval(rule, target, cutoff, '交易规则'):
            selected.append(_json_value(rule))
    for exchange in ('SSE', 'SZSE'):
        count = sum(rule['exchange'] == exchange for rule in selected)
        if count > 1:
            raise ExecutionSupportError('CONFLICT', '目标日存在重叠规则', exchange=exchange)
        if count == 0:
            raise ExecutionSupportError('RULE_NOT_COVERED', '目标日缺少已知有效规则', exchange=exchange)
    metadata = registry['fee_policy_metadata']
    _required(metadata, ('commission_basis', 'stamp_source', 'transfer_source_sse',
                         'transfer_source_szse'), '费用来源')
    for key in ('commission_basis', 'stamp_source', 'transfer_source_sse', 'transfer_source_szse'):
        if not isinstance(metadata[key], str) or not metadata[key].strip():
            raise ExecutionSupportError('INVALID_INPUT', '费用来源标签无效', field=key)
    if not _interval(metadata, target, cutoff, '费用政策'):
        raise ExecutionSupportError('WAITING_RULE', '目标日费用政策元数据未覆盖或尚不可知')
    try:
        validate_fee_policy(fee_policy, target, knowledge_cutoff=cutoff)
    except ExecutionSupportError as exc:
        if exc.code == 'WAITING_DATA':
            raise ExecutionSupportError('WAITING_RULE', str(exc)) from exc
        raise
    body = {'schema_version': 1, 'parser_id': PARSER_ID, 'target_session': target,
            'knowledge_cutoff': cutoff.astimezone(timezone.utc).isoformat(),
            'coverage_mode': COVERAGE_MODE, 'rules': sorted(selected, key=lambda r: r['exchange']),
            'execution_policy': execution, 'fee_policy': _json_value(fee_policy),
            'fee_policy_metadata': _json_value(metadata),
            'rule_policy_hash': policy_digest(execution), 'fee_policy_hash': policy_digest(fee_policy)}
    return {**body, 'ruleset_hash': policy_digest(body)}


def load_policy_bundle(path, *, target_session, knowledge_cutoff, execution_policy, fee_policy):
    with Path(path).open('rb') as stream:
        try:
            registry = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ExecutionSupportError('INVALID_INPUT', '规则登记TOML无效') from exc
    return build_policy_bundle(registry, execution_policy, fee_policy,
                               target_session=target_session, knowledge_cutoff=knowledge_cutoff)


def validate_policy_bundle(bundle, *, target_session=None):
    _required(bundle, ('ruleset_hash', 'target_session', 'knowledge_cutoff',
                       'execution_policy', 'fee_policy'), '冻结政策包')
    expected = build_policy_bundle(bundle, bundle['execution_policy'], bundle['fee_policy'],
                                   target_session=bundle['target_session'],
                                   knowledge_cutoff=bundle['knowledge_cutoff'])
    if _json_value(bundle) != expected:
        raise ExecutionSupportError('INTEGRITY_ERROR', '冻结政策包内容或摘要不一致')
    if target_session is not None and session_date(target_session) != bundle['target_session']:
        raise ExecutionSupportError('INVALID_INPUT', '政策包目标日不匹配')
    return expected


def _status_flag(status, key):
    value = status.get(key)
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is str and value in ('0', '1'):
        return value == '1'
    raise ExecutionSupportError('INVALID_INPUT', '证券状态标记无效', field=key)


def derive_execution_context(bundle, *, code, target_session, status, status_evidence=()):
    """Derive from a frozen ruleset and caller-verified target-session status.

    Missing relisting/delisting flags mean only no *known* special case under the
    frozen KNOWN_ONLY_COVERAGE contract, never a claim of exhaustive absence.
    """
    validated = validate_policy_bundle(bundle, target_session=target_session)
    if not isinstance(code, str) or not re.fullmatch(r'(SH|SZ)\d{6}', code):
        raise ExecutionSupportError('INVALID_INPUT', '证券代码必须含明确沪深市场')
    if not isinstance(status, dict) or not isinstance(status_evidence, (tuple, list)):
        raise ExecutionSupportError('INVALID_INPUT', '证券状态及证据格式无效')
    if any(not isinstance(item, dict) or not item for item in status_evidence):
        raise ExecutionSupportError('INVALID_INPUT', '状态证据须为非空对象')
    exchange = 'SSE' if code.startswith('SH') else 'SZSE'
    if status.get('exchange', exchange) != exchange:
        raise ExecutionSupportError('INVALID_INPUT', '证券市场与代码不一致')
    board = exchange + '_MAIN_A'
    aliases = {board, 'MAIN', '主板', '沪市主板' if exchange == 'SSE' else '深市主板'}
    if status.get('board') is not None and status['board'] not in aliases:
        raise ExecutionSupportError('RULE_NOT_COVERED', '当前规则不覆盖该证券板块')
    rule = next(item for item in validated['rules'] if item['exchange'] == exchange)
    issues = []
    if status.get('board') is None:
        issues.append('SECURITY_BOARD_UNKNOWN')
    if status.get('coverage_mode', COVERAGE_MODE) != COVERAGE_MODE:
        raise ExecutionSupportError('INVALID_INPUT', '证券状态覆盖模式与冻结包不一致')
    risk = _status_flag(status, 'is_st')
    if risk is None:
        issues.append('ST_STATUS_UNKNOWN')
    listing = status.get('listing_session_number')
    if listing is None:
        issues.append('LISTING_SESSION_UNKNOWN')
    else:
        integer(listing, minimum=1)
    lower_bound = _status_flag(status, 'listing_session_lower_bound') is True
    if lower_bound and listing is not None and listing <= 5:
        issues.append('LISTING_SESSION_BOUND_INCONCLUSIVE')
    if _status_flag(status, 'special_status_unknown') is True:
        issues.append('SPECIAL_STATUS_UNKNOWN')
    unlimited = []
    if listing is not None and listing <= 5 and not lower_bound:
        unlimited.append('IPO_FIRST_FIVE_SESSIONS')
    for field, reason in (('relisting_first_session', 'RELISTING_FIRST_SESSION'),
                          ('delisting_first_session', 'DELISTING_FIRST_SESSION')):
        if _status_flag(status, field) is True:
            unlimited.append(reason)
    if unlimited and not status_evidence:
        raise ExecutionSupportError('INVALID_INPUT', '特殊交易日须提供对应状态证据')
    context = {'code': code, 'exchange': exchange, 'board': board,
               'security_class': 'UNKNOWN' if risk is None else ('RISK_WARNING' if risk else 'ORDINARY'),
               'rule_id': rule['rule_id'], 'effective_from': rule['effective_from'],
               'effective_to': rule.get('effective_to'), 'known_at': rule['known_at'],
               'price_tick': rule['price_tick'], 'parser_id': PARSER_ID,
               'ruleset_hash': validated['ruleset_hash'], 'coverage_mode': COVERAGE_MODE,
               'status': _json_value(status), 'status_evidence': _json_value(status_evidence),
               'special_status_unknown': bool(issues), 'issues': sorted(issues)}
    if unlimited:
        context.update(no_price_limit=True, no_price_limit_reasons=unlimited)
    elif not issues:
        context['limit_rate'] = rule['risk_warning_limit_rate'] if risk else rule['limit_rate']
    return context


def validate_execution_context(context, bundle, *, code, target_session, status, status_evidence=()):
    expected = derive_execution_context(bundle, code=code, target_session=target_session,
                                        status=status, status_evidence=status_evidence)
    if _json_value(context) != expected:
        raise ExecutionSupportError('INTEGRITY_ERROR', '执行上下文与冻结规则包及状态证据不一致')
    return expected
