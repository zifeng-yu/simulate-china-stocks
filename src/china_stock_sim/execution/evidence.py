"""Read-only composition of public market queries; never collect or freeze.

The caller supplies a reader. Calculators receive only the resulting values.
"""

from china_stock_sim.common.fees import ExecutionSupportError, aware_time
from china_stock_sim.common.planning_evidence import _query
from china_stock_sim.common.planning_evidence import (
    collect_planning_market as collect_planning_market,
)
from china_stock_sim.common.policies import derive_execution_context, policy_digest
from china_stock_sim.execution.utils import to_wire


def _rows(result):
    data = result.get('data')
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


def _ready(query, label):
    if query['result'].get('state') != 'READY':
        raise ExecutionSupportError('WAITING_DATA', label, source=query['result'])
    return query['result']


def _context(plan, code, target, prices, calendar, current):
    frozen = plan.get('security_facts', {}).get(code)
    if not frozen or not _rows(frozen['result']):
        raise ExecutionSupportError('WAITING_DATA', '原计划缺盘前证券身份')
    security = _rows(frozen['result'])[0]
    bars = [r for r in _rows(_ready(prices, '目标日行情待核'))
            if r.get('code') == code and r.get('date') == target]
    window = _ready(calendar, '上市交易日窗口待核')['data']
    if len(bars) != 1 or not isinstance(window, list) or not window or window[-1] != target:
        raise ExecutionSupportError('WAITING_DATA', '目标日行情或交易窗口不完整')
    listed = security.get('listed')
    if not listed or listed > target:
        raise ExecutionSupportError('WAITING_DATA', '原证券上市日期未知')
    status = {
        'exchange': 'SSE' if code.startswith('SH') else 'SZSE',
        'board': 'SSE_MAIN_A' if code.startswith('SH60') else
                 'SZSE_MAIN_A' if code.startswith('SZ00') else 'UNSUPPORTED',
        'is_st': bars[0].get('isST'),
        'listing_session_number': len([d for d in window if d >= listed]),
        'listing_session_lower_bound': listed < window[0],
        'special_status_unknown': security.get('special_status_unknown') is True,
    }
    rows = _rows(_ready(current, '目标日身份待核'))
    if len(rows) != 1 or rows[0].get('code') != code:
        raise ExecutionSupportError('INTEGRITY_ERROR', '目标日证券身份不符')
    for field, date_field in (('relisting_first_session', 'relisting_date'),
                              ('delisting_first_session', 'delisting_start_date')):
        if rows[0].get(date_field) == target:
            status[field] = True
    if rows[0].get('special_status_unknown') is True:
        status['special_status_unknown'] = True
    ids = sorted(set(frozen['result'].get('observation_ids', [])).union(
        *(q['result'].get('observation_ids', []) for q in (prices, calendar, current))))
    return derive_execution_context(plan['ruleset'], code=code, target_session=target,
        status=status, status_evidence=[{'observation_id': i} for i in ids])


def collect_execution_market(reader, plan, *, knowledge_cutoff):
    cutoff = to_wire(knowledge_cutoff)
    aware_time(cutoff)
    target = plan['target_session']
    by_code, contexts, context_issues = {}, {}, {}
    for code in dict.fromkeys(o['code'] for o in plan['orders']):
        queries = []
        def query(method, **kwargs):
            value = _query(reader, method, **kwargs, knowledge_cutoff=cutoff)
            queries.append(value)
            return value
        prices = query('prices', code=code, start=target, end=target)
        calendar = query('calendar_operation', operation='window', day=target, size=6)
        current = query('security', code=code)
        query('events', code=code)
        query('event_coverage', code=code, start=target, end=target)
        query('calendar_operation', operation='previous_session', day=target)
        if any(o['code'] == code and o['side'] == 'BUY' for o in plan['orders']):
            query('calendar_operation', operation='window', day=plan['market_as_of'], size=121)
        try:
            contexts[code] = _context(plan, code, target, prices, calendar, current)
        except ExecutionSupportError as exc:
            context_issues[code] = {'kind': exc.code, 'reason': str(exc), **exc.details}
        else:
            query('readiness', code=code, as_of=target, purpose='execution',
                  rule_context=contexts[code])
        by_code[code] = queries
    body = {'target_session': target, 'knowledge_cutoff': cutoff, 'by_code': by_code,
            'execution_contexts': contexts, 'context_issues': context_issues,
            'market_snapshot_ids': [], 'observation_ids': sorted({i
                for queries in by_code.values() for q in queries
                for i in q['result'].get('observation_ids', [])})}
    return {**body, 'bundle_hash': policy_digest(body)}
