"""Explicit legal-rule maintenance holds, evaluated against the actual clock."""

import tomllib

from china_stock_sim.common.fees import ExecutionSupportError, aware_time, session_date
from china_stock_sim.common.wire import reject_credentials
from china_stock_sim.market.quality import normalize_code


def execution_blocks(path, ruleset, orders, target_session, *, now):
    with path.open('rb') as stream:
        raw = tomllib.load(stream).get('execution_blocks', [])
    if not isinstance(raw, list):
        raise ExecutionSupportError('INTEGRITY_ERROR', '执行维护阻断须为列表')
    target, actual = session_date(target_session), aware_time(now)
    selected, identities = {}, set()
    required = {'block_id', 'rule_ids', 'effective_from', 'effective_to', 'known_at', 'reason', 'source'}
    for block in raw:
        if not isinstance(block, dict) or not required <= block.keys() or set(block) - required - {'codes'}:
            raise ExecutionSupportError('INTEGRITY_ERROR', '执行维护阻断字段不完整')
        reject_credentials(block)
        for field in ('block_id', 'reason', 'source'):
            if not isinstance(block[field], str) or not block[field].strip():
                raise ExecutionSupportError('INTEGRITY_ERROR', '执行维护阻断身份/依据为空')
        if block['block_id'] in identities:
            raise ExecutionSupportError('INTEGRITY_ERROR', '执行维护阻断ID重复')
        identities.add(block['block_id'])
        rule_ids = block['rule_ids']
        if not isinstance(rule_ids, list) or not rule_ids or any(not isinstance(i, str) or not i for i in rule_ids):
            raise ExecutionSupportError('INTEGRITY_ERROR', '阻断须指定规则ID')
        codes = block.get('codes')
        if codes is not None and (not isinstance(codes, list) or not codes or
                                  any(normalize_code(i) != i for i in codes)):
            raise ExecutionSupportError('INTEGRITY_ERROR', '阻断证券范围无效')
        start, end = session_date(block['effective_from']), session_date(block['effective_to'])
        if start > end:
            raise ExecutionSupportError('INTEGRITY_ERROR', '阻断适用日期倒置')
        known = aware_time(block['known_at'])
        if not start <= target <= end or known > actual:
            continue
        for order in orders:
            code = order['code']
            exchange = 'SSE' if code.startswith('SH') else 'SZSE'
            affected = any(r['rule_id'] in rule_ids and r['exchange'] == exchange
                           for r in ruleset['rules'])
            if affected and (codes is None or code in codes):
                selected.setdefault(code, []).append(block)
    return selected
