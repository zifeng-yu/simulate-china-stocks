"""Bounded, read-only trading display facts; never another financial authority."""

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation

from china_stock_sim.common.event_accounting_scope import event_accounting_scope
from china_stock_sim.market.contracts import aware_time
from china_stock_sim.reporting.trading_history import holding_lots, projection

MAX_QUOTE_PAIRS = 512
_ORDER_FIELDS = (
    'id', 'account_id', 'order_id', 'plan_id', 'order_index', 'target_session', 'code',
    'side', 'quantity', 'limit_price', 'reserved_cash_fen', 'reserved_shares', 'status',
    'reason', 'result_hash', 'audit_state',
)
_FILL_FIELDS = (
    'id', 'account_id', 'fill_id', 'order_id', 'plan_id', 'target_session', 'effective_date',
    'code', 'side', 'quantity', 'price', 'amount_fen', 'fees_fen', 'commission_fen',
    'stamp_fen', 'transfer_fen', 'net_cash_fen', 'cost_fen', 'realized_pnl_fen',
    'state', 'status', 'reason', 'reason_code', 'processed_at', 'computed_at', 'audit_state',
)
_TRADE_CASH_KINDS = {'BUY', 'SELL', 'COMMISSION_FEN', 'STAMP_FEN', 'TRANSFER_FEN'}


def _integer(value):
    return value if type(value) is int else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _day(value):
    try:
        return value if isinstance(value, str) and date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _select(row, fields):
    # Deliberately do not copy nested result, input_bundle, lots or original_record_ref.
    return {key: value for key in fields if key in row
            and isinstance(value := row[key], (str, int, bool, type(None)))}


def _result(value):
    if isinstance(value, dict):
        return value.get('state'), value.get('data')
    return getattr(value, 'state', None), getattr(value, 'data', None)


def _query(market, method, *args, **kwargs):
    try:
        call = getattr(market, method, None)
        return _result(call(*args, **kwargs)) if callable(call) else ('UNAVAILABLE', None)
    except Exception:
        # Display is best effort; never expose exception contents or repair a source.
        return 'UNAVAILABLE', None


class _Quotes:
    def __init__(self, market, cutoff, cache):
        self.market, self.cutoff, self.cache = market, cutoff, cache
        self.used = {}

    def name(self, code):
        key = ('trading-name', self.cutoff, code)
        if key not in self.cache:
            budget = ('trading-name-count', self.cutoff)
            if self.cache.get(budget, 0) >= MAX_QUOTE_PAIRS:
                return None
            self.cache[budget] = self.cache.get(budget, 0) + 1
            state, data = _query(self.market, 'security', code, knowledge_cutoff=self.cutoff)
            name = data.get('name') if state == 'READY' and isinstance(data, dict) else None
            self.cache[key] = name if isinstance(name, str) and name.strip() else None
        return self.cache[key]

    def previous(self, session):
        key = ('trading-previous', self.cutoff, session)
        if key not in self.cache:
            state, previous = _query(self.market, 'calendar_operation', 'previous_session',
                                    session, knowledge_cutoff=self.cutoff)
            self.cache[key] = previous if state == 'READY' and _day(previous) and previous < session else None
        return self.cache[key]

    def bar(self, code, session):
        key = ('trading-bar', self.cutoff, code, session)
        if key not in self.cache:
            state, data = _query(self.market, 'prices', code, session, session,
                                 knowledge_cutoff=self.cutoff)
            rows = [r for r in data if isinstance(r, dict) and r.get('date') == session
                    and r.get('code', code) == code] if isinstance(data, list) else []
            row = rows[0] if state == 'READY' and len(rows) == 1 and not rows[0].get('conflicts') else {}
            if state == 'READY' and not row:
                state = 'UNAVAILABLE'
            self.cache[key] = (state, _select(row, ('open', 'high', 'low', 'close', 'preclose')))
        return self.cache[key]

    def quote(self, code, session):
        empty = {'code': code, 'session': session, 'open': None, 'high': None, 'low': None,
                 'close': None, 'source_preclose': None, 'previous_session': None,
                 'previous_close': None, 'previous_state': 'UNAVAILABLE', 'state': 'UNAVAILABLE',
                 'reason': '行情日期未记录'}
        if not _day(session):
            return empty
        key = ('trading-quote', self.cutoff, code, session)
        if key not in self.cache:
            budget = ('trading-quote-count', self.cutoff)
            if self.cache.get(budget, 0) >= MAX_QUOTE_PAIRS:
                return {**empty, 'reason': '本页行情查询范围已达上限，保留原交易记录'}
            self.cache[budget] = self.cache.get(budget, 0) + 1
            state, bar = self.bar(code, session)
            previous = self.previous(session)
            prior_state, prior = self.bar(code, previous) if previous else ('UNAVAILABLE', {})
            previous_close = prior.get('close')
            if (_number(previous_close) is None or _number(previous_close) <= 0):
                previous_close = None
            reason = (None if state == 'READY' else
                      '行情存在来源冲突，价格待核' if state == 'CONFLICT' else
                      '行情原件不可用，价格待核' if state == 'CORRUPT' else
                      '尚无该日可用收盘行情')
            if state == 'READY' and previous_close is None:
                reason = '前一交易日收盘价未核实；当日行情单独列示'
            self.cache[key] = {**empty, **{k: bar.get(k) for k in ('open', 'high', 'low', 'close')},
                'source_preclose': bar.get('preclose'), 'previous_session': previous,
                'previous_close': previous_close, 'previous_state': prior_state,
                'state': state or 'UNAVAILABLE', 'reason': reason}
        result = deepcopy(self.cache[key])
        self.used[(code, session)] = result
        return result


def _latest_statements(report):
    result = {}
    for row in report.get('statements', []):
        day = _day(row.get('session'))
        if day and row.get('statement_revision', 0) >= result.get(day, {}).get('statement_revision', -1):
            result[day] = row
    return result


def _position_map(statement):
    return {p['code']: p for p in (statement.get('positions') or []) if isinstance(p, dict) and p.get('code')}


def _final(statement):
    return (statement.get('accounting_status') == 'COMPLETE'
            and statement.get('valuation_status') == 'FINAL' and not statement.get('requires_restatement'))


def _holdings(report, current, quotes):
    account = report.get('account', {})
    live = {p['code']: p for p in (account.get('positions') or []) if isinstance(p, dict) and p.get('code')}
    positions = _position_map(current)
    result = []
    for code in sorted(set(live) | set(positions)):
        p, current_position = positions.get(code, {}), live.get(code, {})
        shares, value, cost = p.get('shares'), _integer(p.get('market_value_fen')), _integer(p.get('cost_fen'))
        current_shares = current_position.get('total_shares', shares)
        mismatch = shares is not None and current_shares != shares
        status = p.get('status', 'UNAVAILABLE')
        reason = None
        if mismatch:
            status, reason = 'PROVISIONAL', '当前股数已变化，以下估值仍属已保存账单日期'
        elif current.get('requires_restatement'):
            status, reason = 'PROVISIONAL', '账单待重述，保留原估值但不作为最终损益'
        elif status != 'FINAL' or p.get('price_date') != current.get('session'):
            reason = '估值缺失或使用旧价，不能视作当天最终估值'
        receivable_value = _integer(p.get('share_receivable_value_fen'))
        receivable_cost = _integer(p.get('receivable_cost_fen'))
        result.append({'code': code, 'name': quotes.name(code), 'shares': shares,
            'total_shares': current_shares, 'sellable_shares': current_position.get('sellable_shares'),
            'reserved_shares': current_position.get('reserved_shares'), 'cost_fen': cost,
            'price': p.get('price'), 'price_date': p.get('price_date'), 'market_value_fen': value,
            'floating_pnl_fen': value - cost if value is not None and cost is not None else None,
            'receivable_quantity': p.get('receivable_quantity'), 'receivable_cost_fen': receivable_cost,
            'share_receivable_value_fen': receivable_value,
            'receivable_floating_pnl_fen': receivable_value - receivable_cost
                if receivable_value is not None and receivable_cost is not None else None,
            'valuation_session': current.get('session'), 'status': status, 'reason': reason,
            'quote': quotes.quote(code, current.get('session'))})
    return result


def _observation_case(row, report, day):
    """Do not turn an explicitly non-economic proposal into a P/L restriction."""
    event = row.get('event', row)
    if (row.get('status') in {'IDENTITY_PENDING', 'REVISION_PENDING', 'RULE_PENDING'}
            or event.get('conflicts') or row.get('revision_candidate') or row.get('eligibility')):
        return False
    identity = row.get('case_id', row.get('id'))
    for collection in ('entitlements', 'tax_items'):
        if any((identity and item.get('case_id') == identity) or
               (not identity and item.get('code') == row.get('code'))
               for item in report.get(collection, [])):
            return False
    if identity and any(item.get('reference_id') == identity
                        for item in report.get('cash_entries', [])):
        return False
    actions = row.get('actions') or {}
    if actions and all(isinstance(action, dict) and (
            action.get('state') == 'NOT_APPLICABLE' or
            (action.get('state') == 'NOT_DUE' and _day(action.get('operation_date'))
             and action['operation_date'] > day)) for action in actions.values()):
        return True
    return not actions and event_accounting_scope(event, day) in {'OBSERVE', 'INACTIVE'}


def _event_codes(report, day):
    """Only the ordinary trade/valuation attribution is claimed by this display."""
    result = set()
    dates = ('record_date', 'ex_date', 'recognition_date', 'trigger_date', 'effective_date')
    for collection in ('cases', 'entitlements', 'tax_items'):
        for row in report.get(collection, []):
            if collection == 'cases' and _observation_case(row, report, day):
                continue
            values = [row.get(k) for k in dates if row.get(k)]
            if values and all(_day(v) and v > day for v in values):
                continue  # Explicitly future economics cannot affect this day's attribution.
            if row.get('code'):
                result.add(row['code'])
    return result


def _cash_day(row):
    return row.get('effective_date') or (row.get('recorded_at') or '')[:10]


def _daily_pnl(report, current, statements, quotes):
    day = current.get('session')
    previous = quotes.previous(day) if day else None
    prior = statements.get(previous, {})
    initial = day is not None and day == report.get('account', {}).get('simulation_start_session')
    before, after = _position_map(prior), _position_map(current)
    fills = report.get('_daily_fills')
    if fills is None:
        fills = [f for f in report.get('fills', []) if f.get('effective_date', f.get('target_session')) == day]
    codes = set(before) | set(after) | {f['code'] for f in fills if f.get('code')}
    reason = None
    if not day or not _final(current):
        reason = '本日账务或估值尚未最终确定'
    elif not isinstance(current.get('positions'), list) or (not initial and prior and
            not isinstance(prior.get('positions'), list)):
        reason = '账单持仓明细缺失，不能把缺失数据作为零持仓'
    elif not initial and (not prior or not _final(prior)):
        reason = '缺少连续的前一交易日最终账单'
    elif _integer(current.get('daily_pnl_fen')) is None:
        reason = '账户当日盈亏尚未确定'
    elif current.get('correction_pnl_fen', 0) != prior.get('correction_pnl_fen', 0):
        reason = '本日含现金或收益更正，单股归属待核'

    fill_ids = {f.get('order_id', f.get('id')) for f in fills}
    cash = [e for e in report.get('_daily_cash', report.get('cash_entries', [])) if _cash_day(e) == day
            and e.get('kind') != 'INITIAL_CAPITAL']
    if any(e.get('kind') not in _TRADE_CASH_KINDS or e.get('reference_id') not in fill_ids for e in cash):
        reason = reason or '本日含非交易现金变动或归属不明流水，单股损益待核'
    if any(_integer(f.get('net_cash_fen')) is None for f in fills):
        reason = reason or '成交净现金字段缺失，不能重复推算费用'
    elif fills and (len(fill_ids) != len(fills) or any(
            any(_integer(e.get('amount_fen')) is None for e in cash
                if e.get('reference_id') == f.get('order_id', f.get('id')))
            or sum(e['amount_fen'] for e in cash
                   if e.get('reference_id') == f.get('order_id', f.get('id')))
            != f['net_cash_fen'] for f in fills)):
        reason = reason or '当日成交净现金与可归属流水不一致'

    blocked = _event_codes(report, day) if day else set()
    for statement in (current, prior):
        if ((statement.get('cash_receivable_fen') or statement.get('tax_payable_fen'))
                and not blocked):
            reason = reason or '权益应收或应付税缺少单股归属'
    rows = []
    for code in sorted(codes):
        p, q = before.get(code), after.get(code)
        old_value = _integer(p.get('market_value_fen')) if p else 0
        new_value = _integer(q.get('market_value_fen')) if q else 0
        local_reason = reason
        if code in blocked or any(_number(r.get('receivable_quantity', '0')) != 0
                                  for r in (p, q) if r):
            local_reason = local_reason or '涉及权益或税款，本页未计算该股当日损益'
        for position, session in ((p, previous), (q, day)):
            if position and (position.get('status') != 'FINAL' or position.get('price_date') != session
                             or _integer(position.get('market_value_fen')) is None):
                local_reason = local_reason or '该股估值缺失、暂估或价格日期不连续'
        own = [f for f in fills if f.get('code') == code]
        net = sum(f['net_cash_fen'] for f in own) if all(
            _integer(f.get('net_cash_fen')) is not None for f in own) else None
        old_shares = _integer(p.get('shares')) if p else 0
        new_shares = _integer(q.get('shares')) if q else 0
        valid_trades = all(f.get('side') in {'BUY', 'SELL'} and _integer(f.get('quantity')) is not None
                           and f['quantity'] > 0 for f in own)
        delta = sum(f['quantity'] * (1 if f['side'] == 'BUY' else -1) for f in own) if valid_trades else None
        if (old_shares is None or new_shares is None or delta is None
                or new_shares != old_shares + delta):
            local_reason = local_reason or '股数变化与当日成交不一致，可能含未覆盖调整'
        value = new_value - old_value + net if not local_reason and all(
            v is not None for v in (old_value, new_value, net)) else None
        rows.append({'code': code, 'name': quotes.name(code), 'previous_market_value_fen': old_value,
            'market_value_fen': new_value, 'net_cash_fen': net, 'daily_pnl_fen': value,
            'state': 'READY' if value is not None else 'UNAVAILABLE',
            'reason': local_reason if value is None else None})
    known = sum(r['daily_pnl_fen'] for r in rows if r['daily_pnl_fen'] is not None)
    account_pnl = _integer(current.get('daily_pnl_fen'))
    difference = account_pnl - known if account_pnl is not None else None
    unknown = [r['code'] for r in rows if r['daily_pnl_fen'] is None]
    ready = not reason and not unknown and difference == 0
    return {'session': day, 'previous_session': previous, 'state': 'READY' if ready else 'PARTIAL',
        'rows': rows, 'sum_fen': known, 'account_daily_pnl_fen': account_pnl,
        'unattributed_fen': difference, 'unavailable_codes': unknown,
        'reason': None if ready else reason or '含待核单股或未归属差额，不按账户收益分摊',
        'basis': '本日市值－前一交易日市值＋当日成交净现金；仅已核实普通买卖，费用不重复扣除'}


def build_trading_view(report, *, market, knowledge_cutoff, quote_cache=None):
    """Consume an existing recent report without reading any plan or audit originals.

    The caller may share quote_cache across accounts at the same explicit cutoff.
    Name/quote failures affect display only; no provider, model or mutation is invoked.
    """
    cutoff = aware_time(knowledge_cutoff).isoformat()
    quotes = _Quotes(market, cutoff, quote_cache if quote_cache is not None else {})
    statements = _latest_statements(report)
    current = statements[max(statements)] if statements else {}
    plans = {p.get('plan_id', p.get('id')): p for p in report.get('plans', [])}
    plan_orders, executions = [], []
    for order in report.get('orders', []):
        row = _select(order, _ORDER_FIELDS)
        plan = plans.get(order.get('plan_id'), {})
        code = order.get('code')
        row.update(name=quotes.name(code), market_as_of=plan.get('market_as_of'),
                   frozen_at=plan.get('frozen_at'),
                   reference_quote=quotes.quote(code, plan.get('market_as_of')),
                   target_quote=quotes.quote(code, order.get('target_session')))
        terminal = order.get('status') in {'FILLED', 'NOT_FILLED'}
        attempt = order.get('result') if terminal else order.get('last_attempt')
        reason = (attempt.get('reason_code') or attempt.get('reason')) if isinstance(attempt, dict) else None
        row['execution_reason'] = reason if isinstance(reason, str) and reason else order.get('reason')
        plan_orders.append(row)
    for fill in report.get('fills', []):
        row = _select(fill, _FILL_FIELDS)
        row.update(name=quotes.name(fill.get('code')), quote=quotes.quote(
            fill.get('code'), fill.get('effective_date', fill.get('target_session'))))
        executions.append(row)
    holdings = _holdings(report, current, quotes)
    holding_lots(report, current, holdings)
    source = projection(report)
    dependencies = dict(statements)
    if source:
        for row in source['boundary_statements']:
            day = _day(row.get('session'))
            if day and day not in dependencies:
                dependencies[day] = row
    # Group once, then use only the day's economics. The full history never enters
    # executions/quotes, and a boundary statement never adds a selectable date.
    fills_by_day, cash_by_day = {}, {}
    for row in (source['fills'] if source else report.get('fills', [])):
        fills_by_day.setdefault(row.get('effective_date', row.get('target_session')), []).append(row)
    for row in (source['cash_entries'] if source else report.get('cash_entries', [])):
        cash_by_day.setdefault(_cash_day(row), []).append(row)
    daily = {}
    for day, statement in sorted(statements.items()):
        dated = {**report, '_daily_fills': fills_by_day.get(day, []), '_daily_cash': cash_by_day.get(day, [])}
        daily[day] = _daily_pnl(dated, statement, dependencies, quotes)
    pnl = daily.get(current.get('session')) or _daily_pnl(report, current, statements, quotes)
    return {'schema_version': 1, 'valuation_session': current.get('session'),
        'knowledge_cutoff': cutoff, 'account_target_session': report.get('account', {}).get('target_session'),
        'holdings': holdings, 'stock_daily_pnl': pnl, 'daily_pnl_by_session': daily, 'plan_orders': plan_orders,
        'executions': executions, 'quotes': list(quotes.used.values())}
