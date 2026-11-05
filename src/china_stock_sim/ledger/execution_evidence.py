"""Frozen input binding and minimal terminal proofs, not an execution matcher."""

from decimal import Decimal

from china_stock_sim.common.fees import ExecutionSupportError
from china_stock_sim.common.policies import derive_execution_context, load_policy_bundle
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest
from china_stock_sim.market.contracts import aware_time


def account_input_hash(snapshot):
    """Ignore read/render timestamps, but bind every financial and planning field."""
    return digest({k: v for k, v in snapshot.items() if k not in {
        'snapshot_at', 'report_revision', 'last_rendered_revision'}})


def bind_account_snapshot(payload, current, now):
    supplied = payload.get('account_snapshot')
    if not isinstance(supplied, dict):
        raise LedgerError('INVALID_INPUT', '封存须携带原账户快照')
    if (payload.get('account_revision') != current['account_revision']
            or supplied.get('account_revision') != current['account_revision']):
        raise LedgerError('STALE_ACCOUNT', '规划账户输入已过期，须重新校验意图')
    if (payload.get('account_snapshot_id') != current['state_id']
            or supplied.get('state_id') != current['state_id']
            or supplied.get('account_id') != current['account_id']):
        raise LedgerError('INVALID_INPUT', '账户快照身份不匹配')
    if aware_time(supplied.get('snapshot_at')) > now:
        raise LedgerError('INVALID_INPUT', '账户快照时点在未来')
    expected = account_input_hash(current)
    if payload.get('account_snapshot_hash') != account_input_hash(supplied):
        raise LedgerError('INTEGRITY_ERROR', '账户输入摘要与原快照不符')
    if payload['account_snapshot_hash'] != expected:
        raise LedgerError('STALE_ACCOUNT', '账户资源或规划条件已变化，不能只更新修订号')
    return supplied


def bind_policy_bundle(service, payload, target, cutoff, fees, rules):
    try:
        expected = load_policy_bundle(service.root / 'configs/execution_rules.toml',
            target_session=target, knowledge_cutoff=cutoff,
            execution_policy=rules, fee_policy=fees)
    except ExecutionSupportError as exc:
        state = 'WAITING_DATA' if exc.code in {'WAITING_RULE', 'RULE_NOT_COVERED'} else exc.code
        raise LedgerError(state, str(exc), **exc.details) from exc
    except OSError as exc:
        raise LedgerError('WAITING_DATA', '缺少当前执行规则登记文件') from exc
    if not isinstance(payload.get('ruleset'), dict):
        raise LedgerError('INVALID_INPUT', '封存须携带完整规则包')
    if canonical(expected) != canonical(payload['ruleset']):
        raise LedgerError('INVALID_INPUT', '提交规则包与目标日已知的当前登记规则不符')
    return expected


def _rows(result):
    data = result.get('data')
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


def _ready(result, label):
    if result.get('state') == 'CORRUPT':
        raise LedgerError('INTEGRITY_ERROR', f'{label}证据损坏')
    if result.get('state') != 'READY' or not result.get('observation_ids'):
        raise LedgerError('WAITING_DATA', f'{label}事实或来源证据未齐')
    return result


def capture_security_facts(service, codes, cutoff):
    facts = {}
    for code in codes:
        result = service.market.security(code, knowledge_cutoff=cutoff).as_dict()
        _ready(result, '盘前证券身份')
        rows = _rows(result)
        if len(rows) != 1 or rows[0].get('code') != code:
            raise LedgerError('INTEGRITY_ERROR', '盘前证券身份不匹配')
        facts[code] = {'method': 'security', 'kwargs': {'code': code,
                       'knowledge_cutoff': cutoff}, 'result': result}
    return facts


def rule_context_from_market(service, plan, code, target, cutoff):
    """Reproduce status from public facts; a submitted status flag is not proof.

    Identity is frozen at Kp. Only dated target-session facts may override it;
    a later name or an undated current label cannot rewrite historical identity.
    The returned query evidence must accompany a submitted terminal result.
    """
    frozen = plan.get('security_facts', {}).get(code)
    if not frozen:
        raise LedgerError('WAITING_DATA', '原计划缺盘前身份，禁止事后补造')
    security = _rows(frozen['result'])[0]
    queries = []
    def query(method, **kwargs):
        kwargs['knowledge_cutoff'] = cutoff
        result = getattr(service.market, method)(**kwargs).as_dict()
        queries.append({'method': method, 'kwargs': kwargs, 'result': result})
        return result
    prices = _ready(query('prices', code=code, start=target, end=target), '目标日状态')
    bars = [b for b in _rows(prices) if b.get('code') == code and b.get('date') == target]
    if len(bars) != 1:
        raise LedgerError('WAITING_DATA', '缺目标日唯一行情')
    bar = bars[0]
    calendar = _ready(query('calendar_operation', operation='window', day=target, size=6),
                      '上市交易日窗口')
    window = calendar['data']
    if not isinstance(window, list) or not window or window[-1] != target:
        raise LedgerError('WAITING_DATA', '上市交易日窗口不完整')
    listed = security.get('listed')
    if not listed or listed > target:
        raise LedgerError('WAITING_DATA', '原证券上市日期未知或目标日尚未上市')
    status = {'exchange': 'SSE' if code.startswith('SH') else 'SZSE',
              'board': 'SSE_MAIN_A' if code.startswith('SH60') else
                       'SZSE_MAIN_A' if code.startswith('SZ00') else 'UNSUPPORTED',
              'is_st': bar.get('isST'),
              'listing_session_number': len([day for day in window if day >= listed]),
              'listing_session_lower_bound': listed < window[0],
              'special_status_unknown': security.get('special_status_unknown') is True}
    current = query('security', code=code)
    _ready(current, '目标日身份核查')
    current_rows = _rows(current)
    if len(current_rows) != 1 or current_rows[0].get('code') != code:
        raise LedgerError('INTEGRITY_ERROR', '目标日身份核查证券不符')
    row = current_rows[0]
    for field, date_field in [('relisting_first_session', 'relisting_date'),
                             ('delisting_first_session', 'delisting_start_date')]:
        if row.get(date_field) == target:
            status[field] = True
    if row.get('special_status_unknown') is True:
        status['special_status_unknown'] = True
    ids = sorted(set(frozen['result']['observation_ids']).union(
        *(q['result'].get('observation_ids', []) for q in queries)))
    try:
        context = derive_execution_context(plan['ruleset'], code=code,
            target_session=target, status=status, status_evidence=[{'observation_id': i} for i in ids])
    except ExecutionSupportError as exc:
        raise LedgerError('WAITING_DATA', str(exc), **exc.details) from exc
    return context, queries


def _query_is_bound(verified, expected):
    return any(canonical(q) == canonical(expected) for q in verified['queries'])


def validate_account_view_reference(tx, plan, payload):
    view = payload.get('account_view_evidence')
    if not isinstance(view, dict):
        raise LedgerError('INVALID_INPUT', '执行结果须携带原执行视图证据')
    original_hash = digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
    if original_hash != view.get('view_hash') or original_hash != payload.get('account_view_hash'):
        raise LedgerError('INTEGRITY_ERROR', '执行视图摘要不匹配')
    revision = view.get('account_revision')
    if (type(revision) is not int or payload.get('account_revision') != revision
            or not plan['account_revision_at_freeze'] < revision <= tx.state['account_revision']):
        raise LedgerError('STALE_ACCOUNT', '执行视图修订不在原计划到当前的有效范围')
    if (view.get('account_id') != plan['account_id'] or view.get('plan_id') != plan['plan_id']
            or view.get('target_session') != plan['target_session']
            or view.get('plan_hash') != plan['plan_hash']):
        raise LedgerError('INVALID_INPUT', '执行视图跨账户、计划或日期')
    from china_stock_sim.common.execution_views import plan_binding
    if view.get('evidence_schema') == 2:
        frozen_matches = view.get('frozen_plan_binding') == plan_binding(plan)
        if 'frozen_plan' in view:
            raise LedgerError('INVALID_INPUT', '紧凑执行证据不能内嵌完整计划')
    else:
        frozen_matches = canonical(view.get('frozen_plan', {})) == canonical(plan)
    if (not frozen_matches or
            canonical(view.get('original_resources')) != canonical(plan.get('original_resources'))):
        raise LedgerError('INTEGRITY_ERROR', '执行视图中的原计划或原资源被修改')
    if not (aware_time(plan['frozen_at']) <= aware_time(view.get('snapshot_at'))
            <= aware_time(payload['computed_at'])):
        raise LedgerError('INVALID_INPUT', '执行视图时点须介于封存与计算之间')


def validate_result_contract(plan, payload):
    required = {'result_id', 'order_id', 'plan_id', 'account_id', 'code', 'side',
                'target_session', 'state', 'reason_code', 'reason_details', 'reason_evidence',
                'execution_model', 'fee_policy_hash', 'rule_policy_hash', 'ruleset_hash',
                'execution_context', 'computed_at', 'knowledge_cutoff', 'account_revision',
                'account_view_hash', 'account_view_evidence', 'quantity', 'price', 'amount_fen',
                'commission_fen', 'stamp_fen', 'transfer_fen', 'fees_fen', 'net_cash_fen'}
    if not required <= payload.keys():
        raise LedgerError('INVALID_INPUT', '执行结果缺少必填契约字段', missing=sorted(required-payload.keys()))
    if (not isinstance(payload['result_id'], str) or not payload['result_id']
            or not isinstance(payload['reason_code'], str) or not payload['reason_code']
            or not isinstance(payload['reason_details'], dict)
            or not isinstance(payload['reason_evidence'], dict)):
        raise LedgerError('INVALID_INPUT', '执行结果身份或原因契约无效')
    for key in ('fee_policy_hash', 'rule_policy_hash', 'ruleset_hash'):
        if not payload[key] or payload[key] != plan.get(key):
            raise LedgerError('INVALID_INPUT', '结果不匹配盘前冻结政策', field=key)
    if payload['execution_model'] != plan['rule_policy']['model']:
        raise LedgerError('INVALID_INPUT', '执行模型与冻结政策不符')
    aware_time(payload['computed_at'])
    aware_time(payload['knowledge_cutoff'])


def _target_rows(verified, code, target):
    rows, ids = [], set()
    for snapshot in verified['snapshots']:
        body, content = snapshot['body'], snapshot['body']['content']
        if content.get('as_of') != target:
            continue
        result = content.get('results', {}).get(code, {}).get('valuation', {})
        if (result.get('state') in {'READY', 'PARTIAL', 'WAITING_DATA'}
                and set(result.get('observation_ids', [])).issubset(body.get('observation_ids', []))):
            rows.extend(_rows(result))
            ids.update(result.get('observation_ids', []))
        status = content.get('results', {}).get(code, {}).get('execution', {})
        facts = status.get('data')
        if (status.get('state') in {'READY', 'PARTIAL', 'WAITING_DATA'}
                and isinstance(facts, dict) and facts.get('code') == code
                and facts.get('date') == target and str(facts.get('tradestatus')) == '0'
                and set(status.get('observation_ids', [])).issubset(body.get('observation_ids', []))):
            rows.append({'code': code, 'date': target, 'tradestatus': 0})
            ids.update(status.get('observation_ids', []))
    for query in verified['queries']:
        result, kwargs = query['result'], query['kwargs']
        if (query['method'] == 'prices' and kwargs.get('code') == code
                and kwargs.get('start') == target and kwargs.get('end') == target
                and result.get('state') in {'READY', 'PARTIAL', 'WAITING_DATA'}):
            rows.extend(_rows(result))
            ids.update(result.get('observation_ids', []))
    rows = [r for r in rows if r.get('code') == code and r.get('date') == target]
    merged = {}
    meaningful = {'code', 'date', 'open', 'high', 'low', 'close', 'preclose',
                  'volume', 'amount', 'tradestatus', 'isST'}
    for row in rows:
        for key, value in row.items():
            if key not in meaningful:
                continue
            try:
                same = (key not in merged or
                        (merged[key] is value if value is None or merged[key] is None else
                         str(merged[key]) == str(value) if key in {'code', 'date'} else
                         Decimal(str(merged[key])) == Decimal(str(value))))
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise LedgerError('WAITING_DATA', '目标日证据含无效行情值', field=key) from exc
            if key in merged and not same:
                raise LedgerError('WAITING_DATA', '目标日拒单证据含不一致行情', field=key)
            merged[key] = value
    return merged, ids


def validate_terminal_proof(service, plan, order, payload, verified):
    """Check the specific claim needed to release funds, never choose a trade."""
    for key in ('fee_policy_hash', 'rule_policy_hash', 'ruleset_hash'):
        if not payload.get(key) or payload[key] != plan.get(key):
            raise LedgerError('INVALID_INPUT', '结果不匹配盘前冻结政策', field=key)
    if payload.get('execution_model') != plan['rule_policy']['model']:
        raise LedgerError('INVALID_INPUT', '执行模型与冻结政策不符')
    reason = payload.get('reason_code')
    if not isinstance(reason, str) or not reason or not isinstance(payload.get('reason_details'), dict):
        raise LedgerError('INVALID_INPUT', '结果须有稳定原因码与明细')
    proof = payload.get('reason_evidence')
    if not isinstance(proof, dict) or not isinstance(proof.get('observation_ids'), list):
        raise LedgerError('INVALID_INPUT', '终态须有对应原因的观测证明')
    code, target = order['code'], order['target_session']
    bar, bar_ids = _target_rows(verified, code, target)
    observed = set(proof['observation_ids'])
    all_ids = set(bar_ids)
    for q in verified['queries']:
        all_ids.update(q['result'].get('observation_ids', []))
    for snap in verified['snapshots']:
        all_ids.update(snap['body'].get('observation_ids', []))
    original_events = plan.get('event_facts', {}).get(code, {}).get('result', {})
    original_event_ids = set(original_events.get('observation_ids', []))
    all_ids.update(original_event_ids)
    all_ids.update(plan.get('security_facts', {}).get(code, {}).get('result', {}).get('observation_ids', []))
    if not observed or not observed <= all_ids:
        raise LedgerError('INTEGRITY_ERROR', '原因引用了缺失或无关来源证据')
    bar_proved = bool(bar_ids) and bar_ids <= observed
    if payload['state'] == 'NOT_FILLED' and reason == 'SUSPENDED':
        if str(bar.get('tradestatus')) != '0' or not bar_proved:
            raise LedgerError('WAITING_DATA', '未提供同证券目标日明确停牌证明')
        return
    # A dated final delisting can prove non-tradability without an OHLC row.
    if payload['state'] == 'NOT_FILLED' and reason == 'SECURITY_NOT_TRADABLE':
        for q in verified['queries']:
            if q['method'] != 'security' or q['kwargs'].get('code') != code:
                continue
            if q['result'].get('state') != 'READY':
                continue
            for row in _rows(q['result']):
                if (row.get('code') == code and row.get('delisted')
                        and row['delisted'] <= target
                        and q['result'].get('observation_ids')
                        and set(q['result']['observation_ids']) <= observed):
                    return
        raise LedgerError('WAITING_DATA', '缺目标日已退市的日期证据')
    qualification = payload['reason_details'].get('qualification')
    if payload['state'] == 'NOT_FILLED' and reason == 'BUY_INELIGIBLE' and order['side'] == 'BUY':
        if qualification in {'NOT_MAIN_BOARD', 'DELISTING'}:
            for q in verified['queries']:
                if (q['method'] != 'security' or q['kwargs'].get('code') != code
                        or q['result'].get('state') != 'READY'):
                    continue
                for row in _rows(q['result']):
                    unsupported = (not code.startswith(('SH60', 'SZ00'))
                                   if qualification == 'NOT_MAIN_BOARD' else
                                   bool(row.get('delisting_start_date')
                                        and row['delisting_start_date'] <= target))
                    if (row.get('code') == code and unsupported
                            and q['result'].get('observation_ids')
                            and set(q['result']['observation_ids']) <= observed):
                        return
            raise LedgerError('WAITING_DATA', '缺买入资格排除的证券身份/生效日证明')
    # Some negative claims are already conclusive without an ST/limit context.
    # Null explicitly says no such context was used; arbitrary context is not ignored.
    if payload['state'] == 'NOT_FILLED' and payload.get('execution_context') is None:
        volume = bar.get('volume')
        valid = False
        if reason in {'NO_TRADES', 'INSUFFICIENT_DAILY_VOLUME'}:
            valid = (type(volume) is int and volume >= 0 and
                     (volume == 0 if reason == 'NO_TRADES' else order['quantity'] > volume))
        elif reason == 'LIMIT_NOT_MET':
            try:
                opening = Decimal(bar['open'])
                valid = opening.is_finite() and opening > 0 and (
                    opening > Decimal(order['limit_price']) if order['side'] == 'BUY'
                    else opening < Decimal(order['limit_price']))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                valid = False
        if valid and bar_proved:
            return
        raise LedgerError('WAITING_DATA', '缺少对应未成交原因的最小事实证明')
    context, queries = rule_context_from_market(service, plan, code, target,
                                                payload['knowledge_cutoff'])
    if any(not _query_is_bound(verified, q) for q in queries):
        raise LedgerError('WAITING_DATA', '缺少价格规则状态推导的公开查询证据')
    if canonical(context) != canonical(payload.get('execution_context')):
        raise LedgerError('INVALID_INPUT', '执行上下文不是由原规则包及同日事实推导')
    if context.get('special_status_unknown'):
        raise LedgerError('WAITING_DATA', '目标日特殊状态未确定')
    if payload['state'] == 'SIMULATED_FILL':
        if reason != 'OPEN_PRICE_MATCH':
            raise LedgerError('INVALID_INPUT', '确定成交须声明开盘参考原因码')
        try:
            matches_open = Decimal(payload['price']) == Decimal(bar['open'])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            matches_open = False
        if not matches_open or not bar_proved:
            raise LedgerError('INVALID_INPUT', '开盘成交声明与目标日真实open不符')
        volume = bar.get('volume')
        if (str(bar.get('tradestatus')) != '1' or type(volume) is not int
                or volume < order['quantity'] or context.get('no_price_limit') is True):
            raise LedgerError('INVALID_INPUT', '成交声明与停牌、总量或模型支持范围矛盾')
        return
    valid = False
    if reason == 'MODEL_UNSUPPORTED_SESSION':
        valid = (context.get('no_price_limit') is True and
                 {item['observation_id'] for item in context['status_evidence']} <= observed)
    elif reason == 'BUY_INELIGIBLE' and order['side'] == 'BUY':
        if qualification == 'ST':
            valid = str(bar.get('isST')) == '1' and bar_proved
        elif qualification == 'NEW_LISTING':
            kwargs = {'operation': 'window', 'day': plan['market_as_of'], 'size': 121,
                      'knowledge_cutoff': payload['knowledge_cutoff']}
            result = _ready(service.market.calendar_operation(**kwargs).as_dict(), '121日资格窗口')
            query = {'method': 'calendar_operation', 'kwargs': kwargs, 'result': result}
            listed = _rows(plan['security_facts'][code]['result'])[0].get('listed')
            valid = (_query_is_bound(verified, query) and isinstance(result['data'], list)
                     and len(result['data']) == 121 and listed is not None
                     and listed > result['data'][0] and set(result['observation_ids']) <= observed)
        elif qualification == 'KNOWN_COMPLEX_ACTION':
            valid = (original_events.get('state') == 'READY' and
                     bool(original_event_ids) and original_event_ids <= observed and any(
                         e.get('code') == code and e.get('kind') == 'COMPLEX'
                         and e.get('status') != 'CANCELLED'
                         and (not e.get('ex_date') or e['ex_date'] >= target)
                         for e in _rows(original_events)))
            for q in verified['queries']:
                if (q['method'] == 'events' and q['kwargs'].get('code') == code
                        and q['result'].get('state') == 'READY'
                        and q['result'].get('observation_ids')
                        and set(q['result']['observation_ids']) <= observed):
                    valid = valid or any(e.get('code') == code and e.get('kind') == 'COMPLEX'
                                and e.get('status') != 'CANCELLED'
                                and e.get('ex_date') == target
                                for e in _rows(q['result']))
    elif reason in {'NO_TRADES', 'INSUFFICIENT_DAILY_VOLUME'}:
        volume = bar.get('volume')
        valid = (bar_proved and type(volume) is int and volume >= 0 and
                 (volume == 0 if reason == 'NO_TRADES' else order['quantity'] > volume))
    elif reason == 'LIMIT_NOT_MET':
        try:
            opening, limit = Decimal(bar['open']), Decimal(order['limit_price'])
            valid = bar_proved and opening.is_finite() and opening > 0 and (
                opening > limit if order['side'] == 'BUY' else opening < limit)
        except (KeyError, ValueError, ArithmeticError):
            valid = False
    elif reason in {'ORDER_PRICE_OUT_OF_RANGE', 'LIMIT_UP_BUY_BLOCKED', 'LIMIT_DOWN_SELL_BLOCKED'}:
        service._execution_evidence(verified, payload, code, target)
        facts = service.market.readiness(code, target, 'execution',
            knowledge_cutoff=payload['knowledge_cutoff'], rule_context=context).data
        if facts.get('limit_up') is not None and facts.get('limit_down') is not None:
            upper, lower = Decimal(facts['limit_up']), Decimal(facts['limit_down'])
            if reason == 'ORDER_PRICE_OUT_OF_RANGE':
                valid = not lower <= Decimal(order['limit_price']) <= upper
            elif reason == 'LIMIT_UP_BUY_BLOCKED':
                valid = order['side'] == 'BUY' and Decimal(facts['open']) == upper
            else:
                valid = order['side'] == 'SELL' and Decimal(facts['open']) == lower
        if not bar_proved:
            valid = False
    if not valid:
        raise LedgerError('WAITING_DATA', '未成交原因没有对应的最小事实证明', reason_code=reason)
