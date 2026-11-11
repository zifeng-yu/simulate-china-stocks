"""Read-only trading dashboard sections; amounts retain recorded accounting dates."""

from html import escape

from china_stock_sim.reporting.presentation import identity_html, json_details, money, overview_html
from china_stock_sim.reporting.trading_presentation import (
    asset_charts,
    display_cell,
    price,
    status_label,
    when,
)


def text(value):
    return escape('—' if value is None else str(value), quote=True)


def table(rows, fields, *, empty='暂无记录'):
    head = ''.join(f'<th scope="col">{text(label)}</th>' for _, label, _ in fields)
    body = []
    for row in rows:
        cells = []
        for key, _, is_money in fields:
            value = row.get(key)
            rendered = money(value) if is_money else display_cell(key, value)
            cells.append('<td>' + text(rendered) + '</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    return ('<div class="scroll"><table><thead><tr>' + head + '</tr></thead><tbody>'
            + (''.join(body) or f'<tr><td colspan="{len(fields)}" class="empty">{text(empty)}</td></tr>')
            + '</tbody></table></div>')


def stock(row):
    name, code = row.get('name'), row.get('code')
    return f'{name} · {code}' if name and name != code else str(code or '未记录股票')


def public_details(key, title, body, *, identity=None):
    stable = f' data-public-key="{text(identity)}"' if identity is not None else ''
    return f'<details class="public-detail" data-public-section="{text(key)}"{stable}><summary>{text(title)}</summary><div class="detail-body">{body}</div></details>'


def reason_of(order):
    if order.get('execution_reason'):
        return order['execution_reason']
    for result in (order.get('result'), order.get('last_attempt')):
        if isinstance(result, dict):
            reason = result.get('reason_code') or result.get('reason')
            if reason:
                return reason
    return order.get('reason_code') or order.get('reason')


def planning_notice(account, audit, session, orders):
    plans = [p for p in audit.get('plans', []) if p.get('target_session') == session]
    missing = [p for p in audit.get('no_plan_records', []) if p.get('target_session') == session]
    if plans:
        pending = sum(o.get('status') in {'PENDING', 'WAITING_DATA'} for o in orders)
        if pending:
            label = f'计划已冻结 · {pending}笔订单待执行或待数据'
            detail = '已冻结计划保持原样；模拟成交结果在对应交易日收盘数据就绪后确认。'
        elif orders:
            label, detail = '计划已冻结 · 执行结果已记录', '未成交原因见对应订单；没有成交也可以正常日结。'
        else:
            label = '计划已冻结 · 本日无买卖订单'
            detail = status_label(plans[-1].get('empty_reason') or 'NO_ORDERS')
        return f'<div class="plan-state"><strong>{text(session)} · {text(label)}</strong><p>{text(detail)}</p></div>'
    if missing:
        reason = missing[-1].get('reason')
        message = ('该交易日09:00前未生成冻结计划，已过截止时间；记录保留，不补造当日交易。'
                   if reason == 'MISSED_CUTOFF' else status_label(reason))
        return f'<div class="notice"><strong>{text(session)} · 当日未生成计划</strong><br>{text(message)} 正常持仓估值与日结仍可继续。</div>'
    return f'<div class="plan-state"><strong>{text(session)} · 尚无已冻结计划</strong><p>这里仅展示已保存记录，未生成不等于程序执行失败。</p></div>'


def financial_reasons(account):
    reasons = [r for r in account.get('blocking_reasons', [])
               if r.get('kind') not in {'MISSED_CUTOFF', 'PLAN_ALREADY_FROZEN'}]
    if not reasons:
        return ''
    return '<div class="notice">账户核对事项（' + text(account.get('target_session')) + '）：' + text(
        '；'.join(status_label(r.get('kind')) for r in reasons)) + '</div>'


def historical_no_plan(account, audit):
    rows = audit.get('no_plan_records', [])
    body = '<p>以下记录按各自目标交易日解释，不代表当前资金或最新计划异常。</p>'
    body += table(rows, [('target_session', '目标交易日', False), ('status', '记录状态', False),
                         ('reason', '未生成原因', False), ('processed_at', '记录时间', False)])
    if any(r.get('kind') == 'MISSED_CUTOFF' for r in account.get('blocking_reasons', [])):
        day = account.get('target_session')
        body += ('<p>账户核对目标日 ' + text(day) + '：已过该日09:00新规划截止。'
                 '这是规划时限提示；已有冻结计划继续有效，不表示账务失败。</p>')
    return public_details('history-no-plan', '未生成计划的运行记录与截止时间说明', body)


def plan_audit(audit):
    result = '<details class="audit-detail"><summary>每日冻结明细与决策审计</summary><div class="detail-body">'
    if not audit.get('plans'):
        result += '<p>尚无已冻结计划。</p>'
    for plan in sorted(audit.get('plans', []), key=lambda p: (p.get('target_session') or ''), reverse=True):
        result += '<article><h3>' + text(plan.get('target_session')) + ' · ' + text(status_label(plan.get('status'))) + '</h3>'
        result += '<p>计划标识：' + text(plan.get('plan_id')) + '；审计：' + text(status_label(plan.get('audit_state'))) + '</p>'
        result += identity_html(plan.get('identity'))
        if plan.get('empty_reason'):
            result += '<p>无订单原因：' + text(status_label(plan['empty_reason'])) + '</p>'
        if plan.get('audit_issue'):
            result += '<p class="notice">审计待核：' + text(plan['audit_issue']) + '</p>'
        result += json_details('决策依据、指标、排名与预算', plan.get('calculation')) + '</article>'
    return result + '</div></details>'


def quote_rows(orders, fills):
    rows, seen = [], set()
    for order in orders:
        for field, role in [('reference_quote', '计划参考日'), ('target_quote', '执行交易日')]:
            quote = order.get(field) or {}
            day = quote.get('session') or order.get('market_as_of' if field == 'reference_quote' else 'target_session')
            key = (order.get('code'), day, role)
            if key in seen:
                continue
            seen.add(key)
            rows.append({'stock': stock(order), 'role': role, 'session': day,
                **{k: price(quote.get(k)) for k in ('open', 'high', 'low', 'close', 'previous_close')},
                'note': quote.get('reason') or ('已保存行情' if quote.get('state') == 'READY' else '尚无该日可用收盘数据')})
    for fill in fills:
        quote = fill.get('quote') or {}
        day = quote.get('session') or fill.get('effective_date') or fill.get('target_session')
        key = (fill.get('code'), day, '执行交易日')
        if key in seen:
            continue
        seen.add(key)
        rows.append({'stock': stock(fill), 'role': '执行交易日', 'session': day,
            **{k: price(quote.get(k)) for k in ('open', 'high', 'low', 'close', 'previous_close')},
            'note': quote.get('reason') or ('已保存行情' if quote.get('state') == 'READY' else '尚无该日可用收盘数据')})
    return rows


def trade_days(account, audit, view):
    orders, fills = view.get('plan_orders', []), view.get('executions', [])
    days = {r.get('target_session') for r in [*audit.get('plans', []), *audit.get('no_plan_records', []), *orders]}
    days.update(r.get('effective_date') or r.get('target_session') for r in fills)
    days.update(view.get('daily_pnl_by_session', {}))
    days = sorted((day for day in days if day), reverse=True)
    if not days:
        days = [account.get('target_session') or view.get('valuation_session') or '尚无交易日']
    default = days[0]
    content = '<div class="section-heading"><div><h2>交易计划与实际执行</h2><p>按交易日查看计划、成交、行情及个股当日盈亏。</p></div><label>交易日 <select data-trade-selector aria-label="选择交易日">'
    content += ''.join(f'<option value="{text(day)}"' + (' selected' if day == default else '') + f'>{text(day)}</option>' for day in days) + '</select></label></div>'
    for day in days:
        selected = [r for r in orders if r.get('target_session') == day]
        executed = [r for r in fills if (r.get('effective_date') or r.get('target_session')) == day]
        content += f'<div data-trade-day="{text(day)}"' + ('' if day == default else ' hidden') + '>'
        content += planning_notice(account, audit, day, selected)
        content += '<h3>交易计划</h3>' + table(
            [{**r, 'stock': stock(r), 'limit_price': price(r.get('limit_price')), 'reason': reason_of(r)} for r in selected],
            [('stock', '股票', False), ('side', '方向', False), ('quantity', '计划股数', False),
             ('limit_price', '限价（元）', False), ('status', '执行状态', False), ('reason', '计划理由／执行说明', False)],
            empty='该交易日没有买卖订单，详见上方计划状态。')
        stamps = sorted({r.get('frozen_at') for r in selected if r.get('frozen_at')})
        if stamps:
            content += '<p class="muted">计划冻结时间（北京时间）：' + text('；'.join(when(stamp) for stamp in stamps)) + '</p>'
        content += '<h3>实际成交</h3>' + table(
            [{**r, 'stock': stock(r), 'price': price(r.get('price'))} for r in executed],
            [('stock', '股票', False), ('side', '方向', False), ('quantity', '成交股数', False),
             ('price', '成交价（元）', False), ('amount_fen', '成交额（元）', True),
             ('commission_fen', '佣金', True), ('stamp_fen', '印花税', True), ('transfer_fen', '过户费', True),
             ('net_cash_fen', '净现金变动（元）', True), ('realized_pnl_fen', '卖出已实现盈亏（元）', True)],
            empty='该交易日尚无实际成交记录；待执行、未成交与已成交分别见上表。')
        quotes = quote_rows(selected, executed)
        content += '<h3>相关股票行情</h3><p class="muted">只显示已保存的日行情，不是实时价格。计划参考日与执行日分列；前收为前一交易日已保存收盘价。</p>'
        content += table(quotes, [('stock', '股票', False), ('role', '用途', False), ('session', '行情日期', False),
            ('previous_close', '前收（元）', False), ('open', '开盘', False), ('high', '最高', False),
            ('low', '最低', False), ('close', '收盘', False), ('note', '数据说明', False)])
        content += daily_pnl_section(day, view.get('daily_pnl_by_session', {}).get(day)) + '</div>'
    return content


def holding_buy_cells(holding):
    """Keep original dates/prices paired across the remaining valuation-day lots."""
    dates, prices = [], []
    batches = holding.get('buy_lots', [])
    for index, batch in enumerate(batches, 1):
        prefix = f'{index}. ' if len(batches) > 1 else ''
        kind = batch.get('source_kind')
        if kind == 'BUY':
            dates.append(prefix + str(batch.get('acquired_date') or '待核'))
            value = price(batch.get('price'))
            prices.append(prefix + (value if value != '—' else '待核'))
        else:
            label = '权益到账（非买入）' if kind == 'EQUITY' else '来源待核'
            dates.append(prefix + label)
            prices.append(prefix + label)
    suffix = '；批次待核' if batches and holding.get('buy_lots_reason') else ''
    return {'buy_dates': ('；'.join(dates) or '待核') + suffix,
            'buy_prices': ('；'.join(prices) or '待核') + suffix}


def holdings_section(view):
    day = view.get('valuation_session') or '尚无日结'
    content = '<div class="section-heading"><div><h2>当前持仓与买入成本</h2><p>估值所属日：' + text(day) + '。此区域不随上方交易日选择改变；可卖数量按本次账户快照。</p></div></div>'
    content += table([{**r, **holding_buy_cells(r), 'stock': stock(r), 'price': price(r.get('price'))} for r in view.get('holdings', [])], [
        ('stock', '股票', False), ('buy_dates', '买入日期', False), ('buy_prices', '买入价（元/股）', False),
        ('shares', '持有股数', False), ('sellable_shares', '当前可卖', False),
        ('cost_fen', '剩余含费成本（元）', True), ('cost_price', '含费成本均价（元/股）', False),
        ('price', '估值收盘价（元）', False), ('price_date', '价格日期', False),
        ('market_value_fen', '市值（元）', True), ('floating_pnl_fen', '持仓浮动盈亏（元）', True),
        ('status', '估值状态', False), ('reason', '说明', False)], empty='当前没有已记录持仓。')
    content += '<p class="muted">持仓浮动盈亏＝持仓市值－剩余含费成本，属于累计持仓损益。含费成本均价＝剩余成本÷剩余股数，不等同于原始买入成交价；部分卖出后沿用账本FIFO剩余成本。尚未到账股份另列。</p>'
    content += '<p class="muted">买入日期与买入价取下方剩余批次的原始成交记录；多批次按相同编号对应列出，不合并为均价。权益到账不视为买入，缺失或不一致的批次显示待核。</p>'
    receivables = [r for r in view.get('holdings', []) if str(r.get('receivable_quantity') or '0') not in {'0', '0.0', '0.00'} or r.get('share_receivable_value_fen')]
    if receivables:
        content += table([{**r, 'stock': stock(r)} for r in receivables], [('stock', '股票', False),
            ('receivable_quantity', '股份应收数量', False), ('share_receivable_value_fen', '股份应收价值（元）', True),
            ('receivable_cost_fen', '应收股份成本（元）', True), ('receivable_floating_pnl_fen', '应收股份浮盈（元）', True)])
    for holding in view.get('holdings', []):
        batches = holding.get('buy_lots', [])
        body = '<p>截至 ' + text(day) + ' 仍有剩余股份的批次；已耗尽批次的买卖保留在完整交易历史中。原成交与后续剩余成本分别列示。</p>'
        if holding.get('buy_lots_reason'):
            body += '<p class="notice">' + text(holding['buy_lots_reason']) + '</p>'
        body += table(batches, [
            ('acquired_date', '原始取得日期', False), ('source_label', '来源', False),
            ('price', '原买入成交价（元）', False), ('original_quantity', '原始股数', False),
            ('amount_fen', '原成交额（元）', True), ('commission_fen', '佣金', True),
            ('stamp_fen', '印花税', True), ('transfer_fen', '过户费', True),
            ('original_cost_fen', '原始含费成本（元）', True), ('quantity', '估值日剩余股数', False),
            ('cost_fen', '估值日剩余成本（元）', True), ('state', '核验状态', False),
            ('reason', '说明', False)], empty='暂无可核对的剩余批次明细。')
        content += public_details('buy-lots', stock(holding) + ' · 逐笔买入与剩余批次', body,
                                  identity=holding.get('code'))
    return content


def daily_pnl_section(day, daily):
    content = '<div class="daily-stock-pnl" data-pnl-session="' + text(day) + '"><h3>' + text(day) + ' · 个股当日盈亏</h3>'
    if not daily:
        return content + '<p class="notice">等待该日最终日结；尚无该日已保存账单，不使用其他日期的盈亏代替。</p></div>'
    content += table([{**r, 'stock': stock(r)} for r in daily.get('rows', [])], [('stock', '股票', False),
        ('daily_pnl_fen', '当日盈亏（元）', True), ('state', '核算状态', False), ('reason', '说明', False)],
        empty='该日没有持仓或成交涉及的股票。')
    content += '<p class="muted">普通交易日口径：本日市值－前一交易日市值＋当日成交净现金流（已含手续费）。缺连续估值或权益税务影响未能完整归属时显示待核。</p>'
    content += '<p>已核算个股合计：' + money(daily.get('sum_fen')) + '元；账户当日盈亏：' + money(daily.get('account_daily_pnl_fen')) + '元；其他／待核差额：' + money(daily.get('unattributed_fen')) + '元。差额不分摊到股票。</p>'
    if daily.get('reason'):
        content += '<p class="muted">' + text(daily['reason']) + '</p>'
    return content + '</div>'


def history_section(history, csv_name):
    body = '<div data-trade-history><p>全部已记录成交，截至快照 ' + text(when(history.get('snapshot_at'))) + '。当前持仓分类不随上方交易日选择改变；清仓后再次买入归为当前持有，前次买卖仍完整保留。</p>'
    body += '<p class="muted">累计交易已实现盈亏按已保存卖出及可归属更正汇总，不是包含分红、红利税及未实现盈亏的总收益。原成交明细不随更正改写。</p>'
    body += '<div class="history-controls"><label>股票 <select data-history-stock aria-label="历史股票"><option value="">全部股票</option></select></label><label>当前股份状态 <select data-history-status aria-label="历史持仓状态"><option value="">全部状态</option></select></label></div>'
    body += '<p data-history-info role="status">正在读取完整交易历史…</p><h3>股票累计买卖汇总 · 含已清仓股票</h3>'
    body += '<div class="scroll"><table><thead><tr data-history-summary-head></tr></thead><tbody data-history-summary></tbody></table></div>'
    body += '<div class="history-pages"><button type="button" data-history-summary-prev>上一页汇总</button><span data-history-summary-page></span><button type="button" data-history-summary-next>下一页汇总</button></div>'
    body += '<h3>完整逐笔买卖历史</h3><div class="scroll"><table><thead><tr data-history-head></tr></thead><tbody data-history-rows></tbody></table></div>'
    body += '<div class="history-pages"><button type="button" data-history-prev>上一页成交</button><span data-history-page></span><button type="button" data-history-next>下一页成交</button></div>'
    if csv_name and history.get('complete'):
        body += '<p><a href="' + text(csv_name) + '">导出全部实际成交 CSV（完整范围；金额单位：分）</a></p>'
    if history.get('complete') and history.get('row_counts', {}).get('trades') == 0:
        body += '<p class="muted">当前完整范围内为0笔成交；CSV仅表头，账户范围与快照以上方说明为准。</p>'
    body += '<noscript><p>分页展示需要启用浏览器JavaScript；完整成交可通过上方CSV下载查看。</p></noscript></div>'
    return public_details('trade-history', '已清仓股票与完整买卖历史 · 展开查看', body)


def render_account(report, audit, view, *, statements, csv_name, audit_href, audit_view_href, indices,
                   history=None, history_csv_name=None):
    account = report['account']
    current = report['statements'][-1] if report.get('statements') else {}
    day = current.get('session') or '尚无日结'
    content = '<div class="valuation-heading"><span class="eyebrow">账户资产 · 人民币元</span><p>账务日期 ' + text(day) + ' · ' + text(status_label(current.get('accounting_status', 'NOT_STARTED'))) + ' / ' + text(status_label(current.get('valuation_status', 'NOT_STARTED'))) + '</p></div>'
    cards = [('最终总资产', current.get('net_assets_fen')), ('当日盈亏', current.get('daily_pnl_fen')),
             ('累计盈亏', current.get('cumulative_pnl_fen')), ('持仓市值', current.get('market_value_fen')),
             ('当前现金', account.get('cash_fen')), ('可用现金', account.get('available_cash_fen')),
             ('买单预留', account.get('reserved_cash_fen')), ('累计交易费用', current.get('cumulative_transaction_fees_fen'))]
    content += '<div class="cards summary-cards">' + ''.join('<div><span>' + label + '（元）</span><b' + (' class="loss"' if value is not None and value < 0 else '') + '>' + money(value) + '</b></div>' for label, value in cards) + '</div>'
    content += '<p class="snapshot-note">总资产与盈亏截至上述账务日；现金、预留和可用资金为页面快照。不会将今日尚未收盘的行情作为最终估值。</p>'
    content += financial_reasons(account) + trade_days(account, audit, view) + holdings_section(view)
    if history is not None:
        content += history_section(history, history_csv_name)
    content += '<h2>账户资产与盈亏走势</h2>' + asset_charts(report['statements'], account['initial_cash_fen'])
    funding = '<p>总资产＝现金＋持仓市值＋现金应收＋股份应收价值－已确认应付税；买单预留不重复扣资产。金额未知时显示待核。</p>'
    funding += '<p>本金 ' + money(account['initial_cash_fen']) + '元；现金应收 ' + money(account.get('cash_receivable_fen')) + '元；已确认应付税 ' + money(account.get('known_tax_payable_fen')) + '元。</p>'
    funding += table(statements, [('session', '交易日', False), ('statement_revision', '修订', False),
        ('accounting_status', '账务', False), ('valuation_status', '估值', False), ('closing_cash_fen', '现金（元）', True),
        ('net_assets_fen', '总资产（元）', True), ('daily_pnl_fen', '当日盈亏（元）', True),
        ('cumulative_pnl_fen', '累计盈亏（元）', True), ('strategy_label', '当日计划策略', False)])
    content += public_details('financial-history', '每日资产与资金计算 · 查看账单明细', funding)
    content += historical_no_plan(account, audit)
    content += public_details('cash', '现金流水', table(report.get('cash_entries', []), [
        ('effective_date', '有效日', False), ('kind', '类型', False), ('amount_fen', '变动（元）', True),
        ('reference_id', '关联记录', False), ('recorded_at', '记入时间', False)]))
    content += public_details('corporate-actions', '权益与税款', table(report.get('cases', []), [
        ('code', '股票', False), ('record_date', '登记日', False), ('status', '状态', False), ('case_id', '事项', False)])
        + table(report.get('tax_items', []), [('code', '股票', False), ('status', '税款状态', False),
            ('assessed_fen', '累计确认（元）', True), ('paid_fen', '已支付（元）', True), ('due_date', '待扣日期', False)]))
    content += public_details('runtime', '运行与恢复记录', table(report.get('steps', []), [
        ('effective_date', '有效日', False), ('step', '步骤', False), ('status', '状态', False), ('reason', '说明', False)]))
    content += public_details('benchmarks', '大盘行情', table(indices, [('code', '指数', False),
        ('date', '日期', False), ('state', '状态', False), ('close', '收盘', False), ('change_percent', '涨跌幅%', False)]))
    content += '<details class="audit-detail"><summary>账户登记与最近冻结身份</summary><div class="detail-body">' + overview_html(account, audit) + '</div></details>'
    content += plan_audit(audit)
    scope = report.get('report_scope', {})
    if scope.get('mode') == 'recent':
        content += f'<p class="muted">范围：最近{text(scope.get("recent_sessions"))}个交易日及未决事项；展示{text(scope.get("returned_plans"))}份计划，共{text(scope.get("total_plans"))}份。完整实际成交另见上方全部买卖历史；更早冻结审计可在本机按日期查询。</p>'
    content += f'<p class="exports"><a href="{text(csv_name)}">导出每日账单 CSV（金额单位：分）</a> · <a href="{audit_view_href}">查看本账户审计（中文）</a> · <a href="{audit_href}">导出冻结计划审计 JSON</a></p>'
    content += '<p class="muted">账务完成至 ' + text(account.get('settled_through') or '尚无') + '；最终估值至 ' + text(account.get('valued_through') or '尚无') + '。审计 ' + text(status_label(audit.get('state'))) + ' · 报表修订 ' + text(report.get('report_revision')) + ' · 快照时间（北京时间）' + text(when(report.get('snapshot_at'))) + '</p>'
    return content
