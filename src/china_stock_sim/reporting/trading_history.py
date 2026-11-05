"""Pure, narrow display projection of recorded trades and dated lot balances."""

from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from china_stock_sim.common.event_accounting_scope import event_accounting_scope
from china_stock_sim.reporting.trading_presentation import price, when

CHUNK_SIZE = 1000
CLASS_LABELS = {'HELD': '当前持有', 'CLOSED': '股份已清仓',
                'CLOSED_PENDING': '股份已清仓但权益／税款待处理', 'UNKNOWN': '持仓状态待核'}
SUMMARY_COLUMNS = ['股票', '当前分类', '累计买入股数', '累计卖出股数', '买入成交额（元）',
    '卖出成交额（元）', '原记录交易费用（元）', '原记录净现金（元）', '原记录卖出分配成本（元）',
    '原记录已实现交易盈亏（元）', '更正后已实现交易盈亏（元）', '当前股份', '说明']
TRADE_COLUMNS = ['股票', '成交日期', '方向', '成交股数', '成交价（元）', '成交额（元）',
    '佣金（元）', '印花税（元）', '过户费（元）', '交易费用合计（元）', '净现金（元）',
    '卖出分配成本（元）', '卖出已实现盈亏（元）', '记录时间（北京时间）', '说明']
_ARRAYS = ('fills', 'lots', 'lot_movements', 'cash_entries', 'corrections', 'cases',
           'entitlements', 'tax_items', 'statements', 'boundary_statements')
_FILL_KEYS = ('id', 'fill_id', 'order_id', 'plan_id', 'account_id', 'code', 'side', 'quantity',
    'price', 'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen', 'fees_fen',
    'net_cash_fen', 'cost_fen', 'realized_pnl_fen', 'effective_date', 'target_session',
    'processed_at', 'storage_sequence')


def integer(value):
    return value if type(value) is int else None


def _money(value):
    return '—' if integer(value) is None else f'{Decimal(value) / 100:,.2f}'


def _plain(value):
    return '—' if value is None else str(value)


def _sum(rows, key):
    values = [integer(row.get(key)) for row in rows]
    return sum(values) if all(value is not None for value in values) else None


def projection(report):
    """A missing/inconsistent projection is not evidence of zero history."""
    value = report.get('trading_history_projection')
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('state') != 'READY':
        return None
    if value.get('scope') != 'all_time' or value.get('complete') is not True:
        return None
    if (not isinstance(value.get('account_id'), str) or not value['account_id']
            or value.get('account_id') != report.get('account', {}).get('account_id')):
        return None
    try:
        stamp = datetime.fromisoformat(value.get('snapshot_at'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
    except (ValueError, TypeError):
        return None
    if any(integer(value.get(key)) is None or value[key] < 0
           for key in ('account_revision', 'report_revision')):
        return None
    for key in ('account_revision', 'report_revision', 'snapshot_at'):
        if key in report and value.get(key) != report[key]:
            return None
    counts = value.get('row_counts', {})
    if not isinstance(counts, dict):
        return None
    if any(not isinstance(value.get(key), list) or counts.get(key) != len(value[key])
           or any(not isinstance(row, dict) for row in value[key]) for key in _ARRAYS):
        return None
    for key in ('fills', 'lots', 'lot_movements'):
        if any(not _stock_code(row.get('code')) for row in value[key]):
            return None
    for row in value['statements'] + value['boundary_statements']:
        if not isinstance(row.get('positions'), list) or any(
                not isinstance(item, dict) or not _stock_code(item.get('code')) for item in row['positions']):
            return None
    ids = [row.get('fill_id', row.get('id')) for row in value['fills']]
    if any(not identity for identity in ids) or len(set(ids)) != len(ids):
        return None
    if any(row.get('account_id', value['account_id']) != value['account_id'] for row in value['fills']):
        return None
    return value


def _stock_code(value):
    return isinstance(value, str) and len(value) == 8 and value[:2] in {'SH', 'SZ', 'BJ'} and value[2:].isdigit()


def _date(value):
    try:
        return value if isinstance(value, str) and date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def holding_lots(report, statement, holdings):
    """Join remaining batches at the saved statement day; never use live balances."""
    source, day = projection(report), statement.get('session')
    by_code, totals, bad = defaultdict(list), defaultdict(lambda: [0, 0]), set()
    if source and day:
        moves = defaultdict(list)
        for row in source['lot_movements']:
            moves[row.get('lot_id')].append(row)
        fills = {row.get('order_id', row.get('id')): row for row in source['fills']}
        for lot in source['lots']:
            code, acquired = lot.get('code'), _date(lot.get('acquired_date'))
            if not acquired or acquired > day:
                if not acquired:
                    bad.add(code)
                continue
            all_moves = moves.get(lot.get('lot_id', lot.get('id')), [])
            if any(not _date(row.get('effective_date')) for row in all_moves):
                bad.add(code)
                continue
            own = [row for row in all_moves if row['effective_date'] <= day]
            qty, cost = _sum(own, 'quantity_delta'), _sum(own, 'cost_delta_fen')
            if not own or qty is None or cost is None or qty < 0 or cost < 0 or (qty == 0 and cost != 0):
                bad.add(code)
                continue
            totals[code][0] += qty
            totals[code][1] += cost
            if not qty:
                continue
            fill = fills.get(lot.get('source_order_id'), {})
            kind = 'BUY' if lot.get('source_order_id') else 'EQUITY' if lot.get('source_case_id') else 'UNKNOWN'
            reason = None
            if kind == 'BUY' and (fill.get('side') != 'BUY' or fill.get('code') != code
                    or fill.get('effective_date', fill.get('target_session')) != acquired
                    or integer(fill.get('quantity')) != integer(lot.get('original_quantity'))
                    or integer(fill.get('net_cash_fen')) is None
                    or -fill['net_cash_fen'] != integer(lot.get('original_cost_fen'))
                    or price(fill.get('price')) == '—'):
                reason = '原始买入证据缺失或与批次不一致，原成交价格与成本待核'
                fill = {}
            elif kind == 'UNKNOWN':
                reason = '批次来源未确认，不能当作买入成交'
            row = {'lot_id': lot.get('lot_id', lot.get('id')), 'code': code, 'acquired_date': acquired,
                'source_kind': kind, 'source_label': {'BUY': '买入', 'EQUITY': '权益到账', 'UNKNOWN': '来源待核'}[kind],
                'source_order_id': lot.get('source_order_id'), 'source_case_id': lot.get('source_case_id'),
                'price': fill.get('price'), 'original_quantity': lot.get('original_quantity'),
                'original_cost_fen': lot.get('original_cost_fen') if reason is None else None,
                'amount_fen': fill.get('amount_fen'), 'commission_fen': fill.get('commission_fen'),
                'stamp_fen': fill.get('stamp_fen'), 'transfer_fen': fill.get('transfer_fen'),
                'fees_fen': _fees(fill), 'quantity': qty, 'cost_fen': cost, 'valuation_session': day,
                'state': 'READY' if reason is None else 'UNAVAILABLE', 'reason': reason}
            by_code[code].append(row)
    for row in holdings:
        shares, cost, code = integer(row.get('shares')), integer(row.get('cost_fen')), row['code']
        row['cost_price'] = (str((Decimal(cost) / shares / 100).quantize(Decimal('0.0001'),
            rounding=ROUND_HALF_UP)) if shares is not None and shares > 0 and cost is not None
            and cost >= 0 and not statement.get('requires_restatement') else None)
        row['buy_lots'] = by_code.get(code, [])
        reason = None
        if not source or not day:
            reason = '缺少同一快照的完整批次投影'
        elif code in bad or totals.get(code, [0, 0]) != [shares, cost]:
            reason = '估值日批次股数或成本与账单不一致，明细待核'
        elif statement.get('requires_restatement'):
            reason = '账单待重述，批次与持仓成本待核'
        elif any(item['state'] != 'READY' for item in row['buy_lots']):
            reason = '部分批次原始买入证据待核'
        row['buy_lots_state'], row['buy_lots_reason'] = ('UNAVAILABLE', reason) if reason else ('READY', None)


def _fees(fill):
    if integer(fill.get('fees_fen')) is not None:
        return fill['fees_fen']
    return _sum([{'fee': fill.get(key)} for key in ('commission_fen', 'stamp_fen', 'transfer_fen')], 'fee')


def _pending_codes(source):
    pending = set()
    case_codes = {row.get('case_id', row.get('id')): row.get('code') for row in source['cases']}
    economic_cases = set()
    for key, terminal in (('entitlements', {'PAID', 'DELIVERED'}),
                          ('tax_items', {'PAID', 'RESOLVED', 'NOT_TRIGGERED'})):
        for row in source[key]:
            economic_cases.add(row.get('case_id'))
            if row.get('status') not in terminal:
                pending.add(row.get('code') or case_codes.get(row.get('case_id')))
    for row in source['cases']:
        identity = row.get('case_id', row.get('id'))
        if row.get('status') in {'MERGED', 'NO_ENTITLEMENT'}:
            continue
        if identity not in economic_cases and not row.get('eligibility') and not row.get('revision_candidate'):
            if event_accounting_scope(row.get('event', row), source.get('as_of_session', '')) in {'OBSERVE', 'INACTIVE'}:
                continue
        actions = row.get('actions') or {}
        accrued = all(actions.get(key, {}).get('state') in {'APPLIED', 'NOT_APPLICABLE'}
                      for key in ('cash_accrual', 'share_accrual'))
        if row.get('status') in {'IDENTITY_PENDING', 'REVISION_PENDING', 'RULE_PENDING', 'WAITING_DATA'} or (
                row.get('status') == 'REGISTERED' and not accrued and identity not in economic_cases):
            pending.add(row.get('code'))
    return pending


def _corrections(source, codes):
    """Only persisted, specifically attributed fee effects enter trading P/L."""
    fills = {row.get('order_id', row.get('id')): row for row in source['fills']}
    cash = {row.get('entry_id', row.get('id')): row for row in source['cash_entries']}
    lots = {row.get('lot_id', row.get('id')): row for row in source['lots']}
    adjustment, reasons = defaultdict(int), {}
    for row in source['corrections']:
        reference = row.get('reference_id')
        entry = cash.get(reference, {})
        fill = fills.get(row.get('reference_fill_id') or reference) or fills.get(entry.get('reference_id'), {})
        code = fill.get('code') or row.get('code') or lots.get(row.get('lot_id'), {}).get('code')
        kind = row.get('kind')
        if kind in {'EVENT_IDENTITY_RESOLUTION', 'TAX_HOLDING_RULE_RESOLUTION'} and all(
                value is None or integer(value) == 0
                for key, value in row.items() if key.endswith('_fen')):
            continue  # These persisted metadata resolutions have no economic adjustment.
        valid = row.get('status') in {None, 'APPLIED'}
        if (kind == 'FIFO_FEE_COST' or (row.get('reason') == 'LATE_SHARE_COST_ALLOCATION'
                and row.get('reference_fill_id') and row.get('case_id')
                and row.get('cash_adjustment_fen') == 0)) and fill.get('side') == 'SELL':
            delta = integer(row.get('realized_cost_adjustment_fen'))
        elif kind == 'FEE_ADJUSTMENT' and fill and row.get('status') == 'APPLIED':
            delta = integer(row.get('realized_pnl_adjustment_fen')) if fill.get('side') == 'SELL' else 0
        elif (kind == 'CASH_ADJUSTMENT' and entry and entry.get('kind') not in
                {'BUY', 'SELL', 'COMMISSION_FEN', 'STAMP_FEN', 'TRANSFER_FEN'}
                and not row.get('realized_cost_adjustment_fen') and not row.get('realized_pnl_adjustment_fen')):
            continue  # Dividends/tax remain outside the explicitly trading-only result.
        else:
            delta = None
        if valid and delta is not None and code in codes:
            adjustment[code] += delta
        else:
            for affected in ({code} if code in codes else codes):
                reasons[affected] = '存在无法明确归属的成本、收益或费用更正，更正后交易盈亏待核'
    return adjustment, reasons


def build_trading_history(report):
    """Return complete scalar history, independently of the recent trading view."""
    source = projection(report)
    output = {'schema_version': 1, 'scope': 'all_time',
        'account_id': report.get('account', {}).get('account_id'), 'snapshot_at': report.get('snapshot_at'),
        'account_revision': report.get('account_revision'), 'report_revision': report.get('report_revision'),
        'state': 'UNAVAILABLE', 'complete': False, 'reason': '完整交易历史投影缺失或与报告快照不一致',
        'row_counts': {'trades': 0, 'summaries': 0}, 'trade_columns': TRADE_COLUMNS,
        'summary_columns': SUMMARY_COLUMNS, 'trade_chunks': [], 'summary_chunks': []}
    if source is None:
        return output
    grouped, live, invalid_lots = defaultdict(list), defaultdict(int), set()
    for fill in source['fills']:
        if not fill.get('code') or fill.get('side') not in {'BUY', 'SELL'}:
            return {**output, 'reason': '完整历史存在缺失股票代码或买卖方向的成交，需核对原记录'}
        grouped[fill['code']].append(fill)
    for lot in source['lots']:
        code, qty = lot.get('code'), integer(lot.get('quantity'))
        if qty is None or qty < 0:
            invalid_lots.add(code)
        else:
            live[code] += qty
    codes = set(grouped) | set(live) | invalid_lots
    pending = _pending_codes(source)
    adjustments, correction_reasons = _corrections(source, codes)
    summaries, states = [], {}
    for code in sorted(codes):
        rows = grouped[code]
        buys, sells = [r for r in rows if r['side'] == 'BUY'], [r for r in rows if r['side'] == 'SELL']
        qty = live.get(code)
        if code in invalid_lots or (buys and qty is None):
            status, qty = 'UNKNOWN', None
        else:
            qty = qty or 0
            status = 'HELD' if qty > 0 else 'CLOSED_PENDING' if code in pending or None in pending else 'CLOSED'
        states[code] = status
        original_pnl = _sum(sells, 'realized_pnl_fen')
        reason = correction_reasons.get(code)
        adjusted = original_pnl + adjustments[code] if original_pnl is not None and not reason else None
        if original_pnl is None:
            reason = '原卖出已实现盈亏记录缺失，不能重复推算FIFO成本'
        if status == 'UNKNOWN':
            reason = (reason + '；' if reason else '') + '当前批次余额缺失或异常'
        summary = {'code': code, 'status': status, 'current_quantity': qty,
            'buy_quantity': _sum(buys, 'quantity'), 'sell_quantity': _sum(sells, 'quantity'),
            'buy_amount_fen': _sum(buys, 'amount_fen'), 'sell_amount_fen': _sum(sells, 'amount_fen'),
            'fees_fen': _sum([{'fees': _fees(row)} for row in rows], 'fees'),
            'net_cash_fen': _sum(rows, 'net_cash_fen'), 'sold_cost_fen': _sum(sells, 'cost_fen'),
            'original_realized_pnl_fen': original_pnl, 'realized_pnl_adjustment_fen': adjustments[code] if not reason else None,
            'realized_pnl_fen': adjusted, 'reason': reason}
        summary['cells'] = [code, CLASS_LABELS[status], _plain(summary['buy_quantity']),
            _plain(summary['sell_quantity']), *[_money(summary[key]) for key in
                ('buy_amount_fen', 'sell_amount_fen', 'fees_fen', 'net_cash_fen', 'sold_cost_fen',
                 'original_realized_pnl_fen', 'realized_pnl_fen')], _plain(qty), reason or '仅交易已实现，不含分红与红利税']
        summaries.append(summary)
    trades = []
    for fill in source['fills']:
        row = {key: fill[key] for key in _FILL_KEYS if key in fill and
               isinstance(fill[key], (str, int, bool, type(None)))}
        row.update(status=states[fill['code']], fees_fen=_fees(fill), reason=None)
        if any(integer(row.get(key)) is None for key in ('quantity', 'amount_fen', 'fees_fen', 'net_cash_fen')):
            row['reason'] = '部分原始成交经济字段缺失，缺项不按零计算'
        sold = fill['side'] == 'SELL'
        row['cells'] = [fill['code'], _plain(fill.get('effective_date', fill.get('target_session'))),
            '卖出' if sold else '买入', _plain(fill.get('quantity')), price(fill.get('price')),
            *[_money(row.get(key)) for key in ('amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen',
                                             'fees_fen', 'net_cash_fen')],
            _money(row.get('cost_fen')) if sold else '—',
            _money(row.get('realized_pnl_fen')) if sold else '—', when(fill.get('processed_at')), row['reason'] or '原成交事实']
        trades.append(row)
    output.update(state='READY', complete=True, reason=None, snapshot_at=source.get('snapshot_at'),
        as_of_session=source.get('as_of_session'), row_counts={'trades': len(trades), 'summaries': len(summaries)},
        trade_chunks=[trades[i:i + CHUNK_SIZE] for i in range(0, len(trades), CHUNK_SIZE)],
        summary_chunks=[summaries[i:i + CHUNK_SIZE] for i in range(0, len(summaries), CHUNK_SIZE)])
    return output
