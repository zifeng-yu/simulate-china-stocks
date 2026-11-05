"""Chinese display labels and offline charts for recorded trading facts.

These helpers neither query nor mutate the ledger. Machine exports retain their
original codes; callers HTML-escape text returned by the text helpers.
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

STATUS_LABELS = {
    'COMPLETE': '已完成', 'COMPLETED': '已完成', 'FINAL': '最终估值',
    'PROVISIONAL': '暂估', 'PARTIAL': '部分完成', 'NOT_STARTED': '尚未开始',
    'INITIALIZED_NOT_RUNNING': '已初始化，尚未启用', 'ACTIVE': '生效中',
    'READY': '已就绪', 'OK': '正常', 'PASS': '核验通过', 'KNOWN': '已确认', 'UNKNOWN': '待确认',
    'UNKNOWN_LIABILITY': '负债待确认', 'UNAVAILABLE': '数据不可用',
    'STALE': '数据已过期', 'INVALID': '未通过校验', 'INTEGRITY_ERROR': '完整性待核',
    'AUDIT_UNAVAILABLE': '审计资料待核', 'VALID': '校验通过',
    'MISSED_CUTOFF': '已过该交易日09:00计划截止时间',
    'MISSED': '已过截止时间', 'PLAN_ALREADY_FROZEN': '该日计划已冻结',
    'FROZEN_PLAN_EXISTS': '已有冻结计划，沿用原计划',
    'FROZEN': '已冻结', 'PROPOSED': '提案已生成', 'EMPTY': '无交易订单',
    'NO_PLAN': '当日无交易计划', 'NO_ORDERS': '无交易订单',
    'BUY': '买入', 'SELL': '卖出', 'HOLD': '继续持有',
    'PENDING': '待执行', 'WAITING': '等待处理', 'WAITING_DATA': '等待数据',
    'WAITING_INPUT': '等待可用输入', 'WAITING_INPUTS': '等待必要输入',
    'WAITING_ALLOCATION': '等待股份分配',
    'FILLED': '已成交', 'SIMULATED_FILL': '模拟撮合成功', 'NOT_FILLED': '未成交',
    'LIMIT_NOT_MET': '未满足限价成交条件',
    'OPEN_PRICE_MATCH': '开盘价满足限价，已成交', 'CANCELLED': '已取消',
    'REJECTED': '已拒绝', 'EXPIRED': '已到期', 'RELEASED': '预留已释放',
    'LIMIT_UP': '涨停限制', 'LIMIT_DOWN': '跌停限制', 'SUSPENDED': '停牌',
    'ZERO_VOLUME': '当日无成交量', 'INSUFFICIENT_CASH': '可用资金不足',
    'INSUFFICIENT_SHARES': '可卖股数不足', 'T_PLUS_ONE': '买入股份尚未可卖',
    'NOT_TRADING_DAY': '非交易日', 'NOT_REBALANCE_DAY': '非调仓日',
    'NO_REBALANCE_REQUIRED': '本日无需调仓', 'NO_USABLE_CANDIDATES': '没有可用候选股票',
    'NO_ELIGIBLE_CANDIDATES': '没有满足条件的候选股票',
    'NO_SELLABLE_HOLDINGS': '没有可卖持仓', 'MODEL_FAILED': '模型调用失败',
    'MODEL_ERROR': '模型调用异常', 'CONFIG_CHANGED': '配置已变化，待核对',
    'STALE_INPUT': '输入已过期', 'RECEIVED': '已收到模型响应',
    'SUCCEEDED': '已成功', 'FAILED': '失败', 'ERROR': '异常',
    'RUNNING': '处理中', 'INTERRUPTED': '运行中断', 'PAUSED': '已暂停',
    'DISABLED': '未启用', 'SCHEDULED': '待定时运行', 'IDLE': '等待下次检查',
    'COOLDOWN': '等待重试', 'SOURCE_COOLDOWN': '数据源冷却，等待重试',
    'NETWORK': '网络请求失败', 'SKIPPED': '已跳过', 'ALREADY_APPLIED': '已处理，复用结果',
    'APPLIED': '已记入账本', 'NOT_APPLICABLE': '不适用', 'NOT_REQUIRED': '无需处理',
    'NOT_DUE': '尚未到处理日期', 'CURRENT_REPORT_ALREADY_PUBLISHED': '当前报告已发布',
    'COMPLETE_BACKUP_ALREADY_EXISTS': '已有完整备份', 'NO_CHANGE': '没有变化',
    'REGISTERED': '已登记', 'ACCRUED': '已确认应收', 'PAID': '已支付',
    'DELIVERED': '股份已到账', 'RESOLVED': '已解决', 'NOT_TRIGGERED': '尚未触发',
    'ASSESSED': '税款已确认', 'DUE': '已到期', 'OVERDUE': '已逾期',
    'IDENTITY_PENDING': '权益身份待确认', 'REVISION_PENDING': '权益修订待核对',
    'RULE_PENDING': '适用规则待确认', 'MERGED': '已归并', 'SUPERSEDED': '已由新记录替代',
    'INITIAL_CAPITAL': '初始注资', 'BUY_SETTLEMENT': '买入结算',
    'SELL_SETTLEMENT': '卖出结算', 'CASH_DIVIDEND': '现金分红',
    'DIVIDEND': '分红', 'DIVIDEND_PAYMENT': '分红到账', 'DIVIDEND_TAX': '红利税扣缴',
    'TAX_PAYMENT': '税款支付', 'CASH': '现金权益', 'SHARES': '股份权益',
    'CASH_CORRECTION': '现金更正', 'CORRECTION': '账务更正',
    'FEE': '交易费用', 'COMMISSION': '佣金', 'STAMP': '印花税', 'TRANSFER': '过户费',
    'UNKNOWN_TAX_LIABILITY': '税款金额待确认', 'UNRESOLVED_ORDERS': '历史订单待处理',
    'UNRESOLVED_ENTITLEMENTS': '权益事项待处理',
    'UNKNOWN_SHARE_ALLOCATION': '股份分配待确认', 'RESTATEMENT_PENDING': '历史账务待更正',
    'CASH_SHORTFALL': '可用资金不足', 'VALUATION_NOT_FINAL': '估值尚未最终确认',
    'PRIOR_ACCOUNTING_INCOMPLETE': '前一交易日账务未完成',
    'MISSING_PREVIOUS_SESSION': '缺少前一交易日信息', 'EVENT_EVIDENCE_UNAVAILABLE': '权益证据待补齐',
    'UNDATED_EVENT': '权益日期待确认',
    'UNREGISTERED_OR_AMBIGUOUS_EVENT': '权益事项待登记或身份待核',
    'DUE_ACTION_UNPROCESSED': '到期事项尚未处理',
    'DUE_ENTITLEMENT_UNSETTLED': '到期权益尚未结算', 'DUE_TAX_UNPAID': '到期税款尚未支付',
    'NO_TRUSTED_PRICE': '没有可信估值价格', 'CONFLICT': '来源存在冲突',
    'FROZEN_PLANS_ONLY': '已冻结计划及关联记录', 'MANUAL': '人工来源', 'STRATEGY': '策略来源',
    'calendar': '核对交易日历', 'data': '采集与补齐数据', 'month_end': '月末换池',
    'input_review': '复查输入', 'recovery': '恢复历史待办', 'settlement': '账户结算',
    'entitlements_before': '处理到期权益', 'execution': '执行冻结计划',
    'entitlements_after': '处理权益与税款', 'close': '日结与估值',
    'daily_close': '日结与估值', 'report_daily': '更新当日报告',
    'plan': '生成或恢复计划', 'report_plan': '更新计划报告', 'backup': '核验与保存备份',
    'freeze_plan': '冻结交易计划', 'settle_order': '结算订单',
    'process_entitlements': '处理权益事项', 'pay_dividends': '支付分红',
    'deliver_shares': '交付股份', 'settle_taxes': '结算税款',
}

_ENUM_FIELDS = {
    'status', 'state', 'accounting_status', 'valuation_status', 'plan_status',
    'report_status', 'audit_state', 'side', 'reason', 'reason_code', 'kind', 'step',
    'empty_reason', 'no_order_reason', 'tax_start_status', 'provenance_mode',
}
_TIME_FIELDS = {
    'processed_at', 'recorded_at', 'snapshot_at', 'frozen_at', 'computed_at',
    'knowledge_cutoff', 'published_at', 'started_at', 'finished_at', 'updated_at',
    'created_at', 'fetched_at',
}


def status_label(value):
    """Return a human label without exposing an unexplained machine enum."""
    if value is None or value == '':
        return '—'
    if isinstance(value, dict):
        return status_label(value.get('kind', value.get('reason', value.get('state'))))
    if isinstance(value, (list, tuple)):
        return '；'.join(status_label(item) for item in value) or '—'
    text = str(value)
    label = STATUS_LABELS.get(text, STATUS_LABELS.get(text.upper(), STATUS_LABELS.get(text.lower())))
    if label is not None:
        return label
    if re.search(r'[\u3400-\u9fff]', text):
        return text  # User/model prose is content, not an enum; caller still escapes it.
    return '状态待解释（详见审计记录）'


def display_cell(key, value):
    """Format a non-money cell; only enum-bearing fields are translated."""
    if key in _TIME_FIELDS:
        return when(value)
    if key in _ENUM_FIELDS:
        return status_label(value)
    return '—' if value is None else str(value)


def when(value):
    """Show an aware instant in Beijing time; never infer a naive timestamp's zone."""
    if value is None or value == '':
        return '—'
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return '时间待核（未注明时区）'
        return stamp.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OverflowError):
        return '时间待核'


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def price(value):
    """Keep recorded decimal price precision, with at least two fractional digits."""
    value = _decimal(value)
    if value is None:
        return '—'
    text = format(value, 'f')
    integer, _, fractional = text.partition('.')
    return integer + '.' + fractional.rstrip('0').ljust(2, '0')


def _money(value):
    return f'{value:,.2f}'


def _statement_rows(statements):
    """Use the latest supplied revision per date; never resurrect an old FINAL row."""
    selected = {}
    for row in statements:
        if not isinstance(row, dict):
            continue
        day = row.get('session')
        try:
            date.fromisoformat(day)
        except (ValueError, TypeError):
            continue
        if len(day) != 10:
            continue
        revision = _decimal(row.get('statement_revision', 0)) or Decimal(0)
        if day not in selected or revision >= selected[day][0]:
            selected[day] = (revision, row)
    return [selected[day][1] for day in sorted(selected)]


def _final(row):
    return (row.get('valuation_status') == 'FINAL'
            and row.get('accounting_status') == 'COMPLETE'
            and not row.get('requires_restatement'))


def _axis(values):
    """A small deterministic axis with a real zero and readable decimal steps."""
    low, high = min([Decimal(0), *values]), max([Decimal(0), *values])
    if low == high:
        return Decimal(-1), Decimal(1), [Decimal(-1), Decimal(0), Decimal(1)]
    target = (high - low) / 4
    base = Decimal(10) ** target.adjusted()
    ratio = target / base
    step = next(Decimal(n) * base for n in (1, 2, 5, 10) if ratio <= n)
    lower = (low // step) * step
    if lower > low:
        lower -= step
    upper = (high // step) * step
    if upper < high:
        upper += step
    ticks = []
    tick = lower
    while tick <= upper:
        ticks.append(tick)
        tick += step
    return lower, upper, ticks


def _chart(rows, title, series, *, zero_line=False):
    values = [[(_decimal(row.get(field)) / 100
                if _final(row) and _decimal(row.get(field)) is not None else None)
               for row in rows] for field, _, _, _ in series]
    known = [value for item in values for value in item if value is not None]
    parts = [f'<article class="asset-chart"><h3>{escape(title)}</h3>']
    parts.append('<div class="chart-legend">' + ''.join(
        f'<span class="chart-key" data-series="{key}"><svg class="chart-swatch" '
        f'width="20" height="10" viewBox="0 0 20 10" aria-hidden="true">'
        f'<line x1="0" y1="5" x2="20" y2="5" stroke="{color}" stroke-width="3"/>'
        f'</svg>{escape(label)}</span>' for _, label, color, key in series) + '</div>')
    if not known:
        parts.append('<p class="chart-note">尚无可用的最终账单，暂无曲线。未知金额不记为零。</p></article>')
        return ''.join(parts)
    low, high, ticks = _axis(known)
    left, right, top, bottom = Decimal(100), Decimal(496), Decimal(30), Decimal(220)

    def x(index):
        return ((left + right) / 2 if len(rows) == 1
                else left + Decimal(index) * (right - left) / (len(rows) - 1))

    def y(value):
        return bottom - (value - low) * (bottom - top) / (high - low)

    label = title + '；横轴为交易日期，纵轴为人民币元；仅显示有效最终账单'
    parts.append(f'<svg class="financial-chart" width="520" height="280" viewBox="0 0 520 280" '
                 f'role="img" aria-label="{escape(label)}"><title>{escape(label)}</title>')
    parts.append('<text x="16" y="18" fill="#60758e" font-size="16">金额（元）</text>')
    for tick in ticks:
        py = y(tick)
        is_zero = zero_line and tick == 0
        parts.append(f'<line class="{"chart-zero" if is_zero else "chart-grid"}" '
                     f'x1="{left}" y1="{py:.1f}" x2="{right}" y2="{py:.1f}" '
                     f'stroke="{"#8191a5" if is_zero else "#e3e9f1"}" '
                     f'stroke-width="{"1.5" if is_zero else "1"}"/>')
        tick_label = f'{tick:,.0f}' if tick == tick.to_integral_value() else _money(tick)
        tick_size = min(16, 132 / max(1, len(tick_label)))
        parts.append(f'<text class="chart-value-tick" x="88" y="{py + 5:.1f}" '
                     f'text-anchor="end" fill="#60758e" font-size="{tick_size:g}">'
                     f'{escape(tick_label)}</text>')
    parts.append(f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" '
                 'stroke="#a8b6c8"/>')
    # Three complete ISO dates fit the narrow canvas without overlapping.
    indices = sorted({round(i * (len(rows) - 1) / min(2, len(rows) - 1))
                      for i in range(min(2, len(rows) - 1) + 1)}) if len(rows) > 1 else [0]
    for index in indices:
        day = rows[index]['session']
        anchor = ('middle' if len(rows) == 1 else 'start' if index == 0
                  else 'end' if index == len(rows) - 1 else 'middle')
        parts.append(f'<text class="chart-date-tick" x="{x(index):.1f}" y="242" '
                     f'text-anchor="{anchor}" fill="#60758e" font-size="16">'
                     f'{escape(day)}</text>')
    parts.append('<text x="298" y="269" text-anchor="middle" fill="#60758e" '
                 'font-size="16">交易日期</text>')
    for (_, name, color, key), data in zip(series, values, strict=True):
        segment = []

        def flush():
            if len(segment) > 1:
                coords = ' '.join(f'{x(index):.1f},{y(value):.1f}' for index, value in segment)
                parts.append(f'<polyline class="chart-series" data-series="{key}" '
                             f'points="{coords}" fill="none" stroke="{color}" stroke-width="2.5"/>')
            segment.clear()

        for index, value in enumerate(data):
            # Without a calendar, do not assume an absent date was a market holiday.
            gap = index and (date.fromisoformat(rows[index]['session'])
                            - date.fromisoformat(rows[index - 1]['session'])).days != 1
            if value is None or gap:
                flush()
            if value is None:
                continue
            segment.append((index, value))
        flush()
        for index, value in enumerate(data):
            if value is not None:
                tip = f'{rows[index]["session"]} · {name}：{_money(value)} 元'
                parts.append(f'<circle class="chart-point" data-series="{key}" '
                             f'cx="{x(index):.1f}" cy="{y(value):.1f}" r="3.8" '
                             f'fill="{color}" tabindex="0" aria-label="{escape(tip)}">'
                             f'<title>{escape(tip)}</title></circle>')
    parts.append('</svg>')
    if len(rows) == 1 or sum(any(data[i] is not None for data in values)
                             for i in range(len(rows))) == 1:
        parts.append('<p class="chart-note">目前只有一个有效估值日，显示数值点，尚不能形成走势。</p>')
    overlap_note = ('数值相同的系列会重合，可在每日账单查看准确金额。'
                    if len(series) > 1 else '')
    parts.append('<p class="chart-note">仅使用已保存的最终账单，非实时行情。' + overlap_note
                 + '暂估、待更正或缺失金额不连线；非相邻日期也不推定连续。'
                 '悬停或聚焦数值点可查看日期和金额，精确数值见每日账单。</p></article>')
    return ''.join(parts)


def asset_charts(statements, initial_cash_fen):
    """Render asset composition and P&L separately from immutable supplied rows."""
    rows = _statement_rows(statements)
    principal = _decimal(initial_cash_fen)
    principal_note = ('初始本金 ' + _money(principal / 100) + ' 元。'
                      if principal is not None else '初始本金待核。')
    return ('<div class="asset-charts">' + _chart(rows, '资产构成走势', (
        ('net_assets_fen', '总资产', '#214f89', 'net_assets'),
        ('closing_cash_fen', '现金', '#b67d1d', 'cash'),
        ('market_value_fen', '持仓市值', '#218278', 'market_value'),
    )) + _chart(rows, '累计盈亏走势', (
        ('cumulative_pnl_fen', '累计盈亏', '#7c4f9c', 'pnl'),
    ), zero_line=True) + '</div><p class="chart-note">' + principal_note
        + '累计盈亏取账本已记录结果，不把总资产变化直接当作当日盈亏。</p>')
