"""Deterministic daily-open calculator. No storage, collection or model calls."""

from copy import deepcopy
from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from china_stock_sim.common.execution_views import compact_view_evidence
from china_stock_sim.common.fees import (
    ExecutionSupportError,
    aware_time,
    fee_breakdown,
    integer,
    number,
    session_date,
)
from china_stock_sim.common.policies import (
    MAX_ORDER_SHARES,
    daily_symbol_limit,
    derive_execution_context,
    policy_digest,
    validate_policy_bundle,
)
from china_stock_sim.domain import ExecutionResult

_SHANGHAI = ZoneInfo('Asia/Shanghai')
_TERMINAL = {'FILLED', 'NOT_FILLED'}
_AMOUNTS = ('amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen',
            'fees_fen', 'net_cash_fen')


def _error(code, message, **details):
    raise ExecutionSupportError(code, message, **details)


def _rows(query):
    data = query.get('result', {}).get('data') if query else None
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


def _ids(queries):
    return sorted({i for q in queries for i in q['result'].get('observation_ids', [])})


def _usable(query, *, partial=False):
    return bool(query and query['result'].get('state') in (
        {'READY', 'PARTIAL', 'WAITING_DATA'} if partial else {'READY'})
        and query['result'].get('observation_ids'))


def _find(queries, method, **kwargs):
    matches = [q for q in queries if q['method'] == method
               and all(q['kwargs'].get(k) == v for k, v in kwargs.items())]
    if len(matches) > 1:
        if any(policy_digest(q) != policy_digest(matches[0]) for q in matches[1:]):
            _error('CONFLICT', '同一公开查询出现不一致证据', method=method)
    return matches[0] if matches else None


def _validate_policy_conflicts(plan, market, target, now):
    conflicts = market.get('policy_conflicts', {})
    if not isinstance(conflicts, dict):
        _error('INVALID_INPUT', '规则待核登记须按证券提供')
    codes = {order['code'] for order in plan['orders']}
    required = {'block_id', 'rule_ids', 'effective_from', 'known_at', 'reason', 'source'}
    for code, blocks in conflicts.items():
        if code not in codes or not isinstance(blocks, list):
            _error('INVALID_INPUT', '规则待核登记证券或列表无效')
        exchange = 'SSE' if code.startswith('SH') else 'SZSE'
        rule_ids = {rule['rule_id'] for rule in plan['ruleset']['rules']
                    if rule['exchange'] == exchange}
        for block in blocks:
            if not isinstance(block, dict) or not required <= block.keys():
                _error('INVALID_INPUT', '规则待核登记缺少必要字段')
            if any(not isinstance(block[field], str) or not block[field].strip()
                   for field in ('block_id', 'reason', 'source')):
                _error('INVALID_INPUT', '规则待核身份、原因或来源无效')
            affected = block['rule_ids']
            if (not isinstance(affected, list) or not affected
                    or any(not isinstance(item, str) or not item for item in affected)
                    or not rule_ids.intersection(affected)):
                _error('INVALID_INPUT', '规则待核登记不关联原证券规则')
            start = session_date(block['effective_from'])
            end = (session_date(block['effective_to'])
                   if block.get('effective_to') is not None else None)
            if (start > target or (end is not None and target > end)
                    or aware_time(block['known_at']) > now):
                _error('INVALID_INPUT', '规则待核登记的目标日或知悉时点无效')
            restricted = block.get('codes')
            if restricted is not None and (not isinstance(restricted, list)
                    or any(not isinstance(item, str) for item in restricted)
                    or code not in restricted):
                _error('INVALID_INPUT', '规则待核登记串证券')


def _validate_inputs(plan, account, market, now):
    for label, value in [('计划', plan), ('执行视图', account), ('市场证据', market)]:
        if not isinstance(value, dict):
            _error('INVALID_INPUT', f'{label}须为公共对象')
    target = session_date(plan['target_session'])
    if plan.get('id', plan.get('plan_id')) != plan.get('plan_id'):
        _error('INTEGRITY_ERROR', '计划索引身份不符')
    for key in ('plan_id', 'account_id', 'plan_hash'):
        if not isinstance(plan.get(key), str) or not plan[key] or account.get(key) != plan[key]:
            _error('INVALID_INPUT', '执行视图与冻结计划身份不一致', field=key)
    if target != account.get('target_session') or target != market.get('target_session'):
        _error('INVALID_INPUT', '执行证据目标日不一致')
    if policy_digest(plan) != policy_digest(account.get('frozen_plan')):
        _error('INTEGRITY_ERROR', '执行视图不是该冻结计划')
    if policy_digest({k: v for k, v in plan.items() if k not in {'document', 'id'}}) != plan.get('document', {}).get('content_hash'):
        _error('INTEGRITY_ERROR', '冻结计划内容摘要不符')
    if policy_digest({k: v for k, v in account.items() if k not in {'view_hash', 'snapshot_at'}}) != account.get('view_hash'):
        _error('INTEGRITY_ERROR', '执行视图摘要不符')
    if policy_digest(account.get('original_resources')) != policy_digest(plan.get('original_resources')):
        _error('INTEGRITY_ERROR', '原资源与冻结计划不一致')
    if policy_digest({k: v for k, v in market.items() if k != 'bundle_hash'}) != market.get('bundle_hash'):
        _error('INTEGRITY_ERROR', '市场证据包摘要不符')
    validate_policy_bundle(plan['ruleset'], target_session=target)
    for key, value in [('fee_policy_hash', policy_digest(plan['fee_policy'])),
                       ('rule_policy_hash', policy_digest(plan['rule_policy'])),
                       ('ruleset_hash', plan['ruleset']['ruleset_hash'])]:
        if plan.get(key) != value:
            _error('INTEGRITY_ERROR', '原政策摘要不符', field=key)
    if (plan['ruleset']['fee_policy'] != plan['fee_policy']
            or plan['ruleset']['execution_policy'] != plan['rule_policy']):
        _error('INTEGRITY_ERROR', '计划政策与原规则包不一致')
    frozen, view_time = aware_time(plan['frozen_at']), aware_time(account['snapshot_at'])
    cutoff = aware_time(market['knowledge_cutoff'])
    deadline = datetime.combine(datetime.fromisoformat(target).date(), time(18, 10), _SHANGHAI)
    preopen = datetime.combine(datetime.fromisoformat(target).date(), time(9), _SHANGHAI)
    if frozen >= preopen:
        _error('INVALID_INPUT', '原计划没有在目标日09:00前封存')
    if not frozen <= view_time <= now or not frozen <= cutoff <= now:
        _error('INVALID_INPUT', '视图或知识时点不合法')
    if now < deadline:
        _error('NOT_DUE', '目标日18:10之前不能计算全日结果')
    if cutoff < deadline:
        _error('INVALID_INPUT', '知识截止早于目标日日终处理门槛')
    revision = integer(account['account_revision'], minimum=0)
    if revision <= integer(plan['account_revision_at_freeze'], minimum=0):
        _error('STALE_ACCOUNT', '执行视图早于计划封存')
    orders = plan['orders']
    limit = daily_symbol_limit(plan['rule_policy'], plan['strategy_id'])
    if not isinstance(orders, list) or limit is not None and len(orders) > limit:
        _error('INVALID_INPUT', '冻结订单集合非法')
    identities = [o['order_id'] for o in orders]
    if identities != plan['order_ids'] or len(set(identities)) != len(identities):
        _error('INTEGRITY_ERROR', '原订单身份或顺序不一致')
    if len({o['code'] for o in orders}) != len(orders):
        _error('INVALID_INPUT', '同代码重复冻结单')
    if [o['side'] for o in orders] != sorted([o['side'] for o in orders], key=lambda s: s != 'SELL'):
        _error('INVALID_INPUT', '冻结顺序不是SELL后BUY')
    _validate_policy_conflicts(plan, market, target, now)
    current = account['orders']
    if not isinstance(current, list) or [o['order_id'] for o in current] != identities:
        _error('INTEGRITY_ERROR', '当前订单集合与原计划不一致')
    for index, (order, view) in enumerate(zip(orders, current)):
        if (order['target_session'] != target or order['plan_id'] != plan['plan_id']
                or order['order_index'] != index or order['side'] not in {'BUY', 'SELL'}):
            _error('INVALID_INPUT', '原订单身份字段非法')
        quantity = integer(order['quantity'], minimum=1)
        if quantity > MAX_ORDER_SHARES or (order['side'] == 'BUY' and quantity % 100):
            _error('INVALID_INPUT', '原订单数量非法')
        if number(order['limit_price'], positive=True) % Decimal('0.01'):
            _error('INVALID_INPUT', '原订单限价不是合法价位')
        if any(view['order'].get(k) != order[k] for k in (
                'order_id', 'plan_id', 'code', 'side', 'quantity', 'limit_price', 'order_index', 'target_session')):
            _error('INTEGRITY_ERROR', '当前订单修改了冻结条款')
        if view.get('status') != view['order'].get('status'):
            _error('INTEGRITY_ERROR', '订单视图与当前订单状态不一致')
        if view.get('status') not in _TERMINAL | {'PENDING', 'WAITING_DATA'}:
            _error('INVALID_INPUT', '当前订单状态非法')
    for code, queries in market['by_code'].items():
        if not isinstance(queries, list):
            _error('INVALID_INPUT', '逐股证据须为列表')
        for query in queries:
            if not isinstance(query, dict) or not {'method', 'kwargs', 'result'} <= query.keys():
                _error('INVALID_INPUT', '公开查询证据结构不完整')
            if aware_time(query['kwargs'].get('knowledge_cutoff')) != cutoff:
                _error('INVALID_INPUT', '公开查询知识截止不一致')
            if query['kwargs'].get('code', code) != code:
                _error('INVALID_INPUT', '公开查询串证券')
    return target, cutoff


def _context(plan, code, target, queries, supplied):
    """Reproduce the frozen-ruleset status recipe using public query results."""
    price = _find(queries, 'prices', code=code, start=target, end=target)
    calendar = _find(queries, 'calendar_operation', operation='window', day=target, size=6)
    security = _find(queries, 'security', code=code)
    required = [price, calendar, security]
    original = plan.get('security_facts', {}).get(code)
    if not all(_usable(q) for q in required) or not _usable(original):
        return None, []
    bars = [r for r in _rows(price) if r.get('code') == code and r.get('date') == target]
    old = _rows(original)
    current = _rows(security)
    window = calendar['result']['data']
    if (len(bars) != 1 or len(old) != 1 or len(current) != 1
            or current[0].get('code') != code or old[0].get('code') != code
            or not isinstance(window, list) or not window or window[-1] != target
            or not old[0].get('listed') or old[0]['listed'] > target):
        return None, []
    listed = old[0]['listed']
    status = {'exchange': 'SSE' if code.startswith('SH') else 'SZSE',
              'board': 'SSE_MAIN_A' if code.startswith('SH60') else
                       'SZSE_MAIN_A' if code.startswith('SZ00') else 'UNSUPPORTED',
              'is_st': bars[0].get('isST'),
              'listing_session_number': len([day for day in window if day >= listed]),
              'listing_session_lower_bound': listed < window[0],
              'special_status_unknown': old[0].get('special_status_unknown') is True}
    for flag, field in [('relisting_first_session', 'relisting_date'),
                        ('delisting_first_session', 'delisting_start_date')]:
        if current[0].get(field) == target:
            status[flag] = True
    if current[0].get('special_status_unknown') is True:
        status['special_status_unknown'] = True
    ids = sorted(set(_ids(required)) | set(original['result']['observation_ids']))
    try:
        context = derive_execution_context(plan['ruleset'], code=code, target_session=target,
            status=status, status_evidence=[{'observation_id': i} for i in ids])
    except ExecutionSupportError as exc:
        if exc.code in {'RULE_NOT_COVERED', 'WAITING_RULE'}:
            return None, []
        raise
    if supplied is not None and policy_digest(context) != policy_digest(supplied):
        _error('INVALID_INPUT', '上下文不是由原规则及当前公开证据推导', code=code)
    return context, required


class DailyOpenMatcher:
    """Pure full-order daily-open matcher; callers own evidence reads and commits."""

    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def match(self, plan, account, market):
        now = aware_time(self.clock())
        try:
            target, cutoff = _validate_inputs(plan, account, market, now)
            return tuple(self._order(plan, account, row, market, target, cutoff, now)
                         for row in account['orders'] if row['status'] not in _TERMINAL)
        except (KeyError, TypeError, AttributeError, IndexError) as exc:
            raise ExecutionSupportError('INVALID_INPUT', '执行输入缺字段或结构非法') from exc

    def _order(self, plan, account, row, market, target, cutoff, now):
        order = row['order']
        code = order['code']
        queries = market['by_code'].get(code, [])
        price = _find(queries, 'prices', code=code, start=target, end=target)
        security = _find(queries, 'security', code=code)
        events = _find(queries, 'events', code=code)
        coverage = _find(queries, 'event_coverage', code=code, start=target, end=target)
        bars = [r for r in _rows(price) if r.get('code') == code and r.get('date') == target]
        bar = bars[0] if _usable(price, partial=True) and len(bars) == 1 else {}
        sec = _rows(security)[0] if _usable(security) and len(_rows(security)) == 1 else {}

        def result(state, reason, used=(), context=None, details=None, extra_ids=(), fees=None):
            used = deepcopy([q for q in used if q])
            details = deepcopy(details or {})
            ids = sorted(set(_ids(used)) | set(extra_ids))
            values = {key: None if state == 'WAITING_DATA' else 0 for key in _AMOUNTS}
            quantity, opening = (None, None) if state == 'WAITING_DATA' else (0, None)
            if state == 'SIMULATED_FILL':
                values = fees
                quantity = order['quantity']
                opening = str(bar['open'])
            witness = compact_view_evidence(account)
            body = dict(order_id=order['order_id'], plan_id=plan['plan_id'],
                account_id=plan['account_id'], code=code, side=order['side'], target_session=target,
                state=state, execution_model=plan['rule_policy']['model'],
                fee_policy_hash=plan['fee_policy_hash'], rule_policy_hash=plan['rule_policy_hash'],
                ruleset_hash=plan['ruleset_hash'], computed_at=now.isoformat(),
                knowledge_cutoff=cutoff.isoformat(), account_view_hash=witness['view_hash'],
                account_view_evidence=witness, account_revision=account['account_revision'],
                reason_code=reason, reason_details=details,
                reason_evidence={'observation_ids': ids}, execution_context=deepcopy(context),
                market_snapshot_ids=(), data_evidence=tuple(used), quantity=quantity, price=opening,
                reason=reason, **values)
            identity = {k: v for k, v in body.items()
                        if k not in {'computed_at', 'account_revision', 'account_view_evidence'}}
            return ExecutionResult(result_id='execution:' + policy_digest(identity), **body)

        if market.get('policy_conflicts', {}).get(code):
            return result('WAITING_DATA', 'RULE_POLICY_CONFLICT', details={
                'policy_conflicts': market['policy_conflicts'][code]})
        if str(bar.get('tradestatus')) == '0':
            return result('NOT_FILLED', 'SUSPENDED', [price])
        if sec.get('delisted') and sec['delisted'] <= target:
            return result('NOT_FILLED', 'SECURITY_NOT_TRADABLE', [security])
        context, context_queries = _context(plan, code, target, queries,
                                           market['execution_contexts'].get(code))
        known_context = context and not context.get('special_status_unknown')
        if known_context and context.get('no_price_limit') is True:
            return result('NOT_FILLED', 'MODEL_UNSUPPORTED_SESSION', context_queries,
                          context, extra_ids=[x['observation_id'] for x in context['status_evidence']])
        if order['side'] == 'BUY':
            qualification, used, extra = None, [], []
            if sec and not code.startswith(('SH60', 'SZ00')):
                qualification, used = 'NOT_MAIN_BOARD', [security]
            elif sec.get('delisting_start_date') and sec['delisting_start_date'] <= target:
                qualification, used = 'DELISTING', [security]
            if qualification:
                return result('NOT_FILLED', 'BUY_INELIGIBLE', used,
                              details={'qualification': qualification})
            if known_context:
                if str(bar.get('isST')) == '1':
                    qualification, used = 'ST', context_queries
                original_events = plan.get('event_facts', {}).get(code)
                if not qualification and _usable(original_events) and any(
                        e.get('code') == code and e.get('kind') == 'COMPLEX'
                        and e.get('status') != 'CANCELLED'
                        and (not e.get('ex_date') or e['ex_date'] >= target)
                        for e in _rows(original_events)):
                    qualification, used = 'KNOWN_COMPLEX_ACTION', context_queries
                    extra = original_events['result']['observation_ids']
                if not qualification and _usable(events) and any(
                        e.get('code') == code and e.get('kind') == 'COMPLEX'
                        and e.get('status') != 'CANCELLED' and e.get('ex_date') == target
                        for e in _rows(events)):
                    qualification, used = 'KNOWN_COMPLEX_ACTION', [*context_queries, events]
                window = _find(queries, 'calendar_operation', operation='window',
                               day=plan['market_as_of'], size=121)
                old = _rows(plan.get('security_facts', {}).get(code))
                if not qualification and _usable(window) and old:
                    days = window['result']['data']
                    listed = old[0].get('listed')
                    if isinstance(days, list) and len(days) == 121 and listed and listed > days[0]:
                        qualification, used = 'NEW_LISTING', [*context_queries, window]
                if qualification:
                    return result('NOT_FILLED', 'BUY_INELIGIBLE', used, context,
                                  {'qualification': qualification}, extra)
        volume = bar.get('volume')
        if type(volume) is int and volume >= 0:
            if volume == 0:
                return result('NOT_FILLED', 'NO_TRADES', [price])
            if order['quantity'] > volume:
                return result('NOT_FILLED', 'INSUFFICIENT_DAILY_VOLUME', [price])
        readiness = _find(queries, 'readiness', code=code, as_of=target, purpose='execution')
        facts = readiness['result'].get('data') if _usable(readiness) else None
        limits_ready = known_context and isinstance(facts, dict)
        if limits_ready:
            if (facts.get('code') != code or facts.get('date') != target
                    or facts.get('rule_id') != context['rule_id']
                    or policy_digest(readiness['kwargs'].get('rule_context')) != policy_digest(context)
                    or aware_time(facts['knowledge_cutoff']) != cutoff):
                _error('INTEGRITY_ERROR', '执行用途事实与规则上下文不一致', code=code)
            upper = number(facts.get('limit_up'), positive=True)
            lower = number(facts.get('limit_down'), positive=True)
            if lower > upper:
                _error('INTEGRITY_ERROR', '涨跌停价格倒置', code=code)
            if not lower <= number(order['limit_price']) <= upper:
                return result('NOT_FILLED', 'ORDER_PRICE_OUT_OF_RANGE',
                              [*context_queries, readiness], context)
        try:
            opening = number(bar.get('open'), positive=True)
        except ExecutionSupportError:
            opening = None
        limit = number(order['limit_price'], positive=True)
        if opening is not None and (opening > limit if order['side'] == 'BUY' else opening < limit):
            return result('NOT_FILLED', 'LIMIT_NOT_MET', [price])
        if limits_ready and opening is not None:
            if order['side'] == 'BUY' and opening == upper:
                return result('NOT_FILLED', 'LIMIT_UP_BUY_BLOCKED',
                              [*context_queries, readiness], context)
            if order['side'] == 'SELL' and opening == lower:
                return result('NOT_FILLED', 'LIMIT_DOWN_SELL_BLOCKED',
                              [*context_queries, readiness], context)
        missing = []
        for name, query in [('prices', price), ('security', security), ('event_coverage', coverage),
                            ('readiness', readiness)]:
            if not _usable(query):
                missing.append({'input': name, 'state': query['result'].get('state') if query else 'MISSING',
                                'issues': query['result'].get('issues', []) if query else []})
        if not known_context:
            missing.append({'input': 'execution_context', 'issues': context.get('issues', []) if context else []})
        if (opening is not None and opening % Decimal('0.01')):
            missing.append({'input': 'open_price_tick'})
        if opening is None or type(volume) is not int or volume <= 0 or str(bar.get('tradestatus')) != '1':
            missing.append({'input': 'open_volume_tradestatus'})
        if limits_ready and opening is not None and (not lower <= opening <= upper
                or number(facts.get('open'), positive=True) != opening):
            missing.append({'input': 'price_fact_consistency'})
        if missing:
            reason = 'INTEGRITY_ERROR' if any(item.get('state') == 'CORRUPT' for item in missing) else 'WAITING_INPUTS'
            return result('WAITING_DATA', reason, details={'required_inputs': missing,
                'retry_condition': 'repair_evidence' if reason == 'INTEGRITY_ERROR' else 'new_market_evidence'})
        fees = fee_breakdown(opening, order['quantity'], order['side'], plan['fee_policy'], target)
        if (row.get('issues') or row.get('can_submit') is not True
                or (order['side'] == 'BUY' and -fees['net_cash_fen'] > row.get('max_payable_fen', 0))):
            return result('WAITING_DATA', 'ACCOUNT_RESOURCE_REVIEW', details={
                'issues': row.get('issues', []), 'max_payable_fen': row.get('max_payable_fen'),
                'retry_condition': 'account_or_prior_stage_progress'})
        return result('SIMULATED_FILL', 'OPEN_PRICE_MATCH',
                      [*context_queries, readiness, coverage], context, fees=fees)
