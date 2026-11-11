"""User-facing date and accounting semantics of the redesigned trading view."""

from html.parser import HTMLParser

from china_stock_sim.reporting.trading_ui import (
    daily_pnl_section,
    historical_no_plan,
    history_section,
    holdings_section,
    planning_notice,
    quote_rows,
    table,
    trade_days,
)


class Text(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.parts = []
        self.feed(html)

    def handle_data(self, value):
        self.parts.append(value)

    @property
    def value(self):
        return ' '.join(self.parts)


def test_frozen_today_plan_is_not_presented_as_yesterday_missing_cutoff():
    account = {'target_session': '2026-11-03', 'blocking_reasons': [{'kind': 'MISSED_CUTOFF'}]}
    audit = {'plans': [{'target_session': '2026-11-04', 'status': 'FROZEN'}],
             'no_plan_records': [{'target_session': '2026-11-03', 'status': 'NO_PLAN',
                                  'reason': 'MISSED_CUTOFF'}]}
    current = planning_notice(account, audit, '2026-11-04', [{'status': 'PENDING'}])
    assert '计划已冻结' in current and '待执行' in current
    assert 'MISSED_CUTOFF' not in current and '未生成计划' not in current
    history = historical_no_plan(account, audit)
    assert '2026-11-03' in history and '09:00' in history
    assert '不表示账务失败' in history and 'MISSED_CUTOFF' not in history
    assert 'data-public-section="history-no-plan"' in history


def test_date_selector_defaults_to_latest_plan_without_hiding_earlier_fills():
    account = {'target_session': '2026-11-03'}
    audit = {'plans': [{'target_session': '2026-11-04'}], 'no_plan_records': []}
    view = {'plan_orders': [{'target_session': '2026-11-04', 'code': 'SH600000',
                            'side': 'BUY', 'quantity': 100, 'limit_price': '10', 'status': 'PENDING'}],
            'executions': [{'target_session': '2026-11-02', 'code': 'SH600000',
                            'side': 'BUY', 'quantity': 100, 'price': '9', 'amount_fen': 90000,
                            'commission_fen': 500, 'net_cash_fen': -90500}]}
    html = trade_days(account, audit, view)
    assert '<option value="2026-11-04" selected>' in html
    assert '<div data-trade-day="2026-11-02" hidden>' in html
    assert '<div data-trade-day="2026-11-04">' in html
    assert '905.00' in html and '实际成交' in html and 'BUY' not in Text(html).value


def test_reference_quote_never_substitutes_target_day_missing_price():
    rows = quote_rows([{'code': 'SH600000', 'market_as_of': '2026-11-03',
                       'target_session': '2026-11-04',
                       'reference_quote': {'session': '2026-11-03', 'state': 'READY', 'close': '10'},
                       'target_quote': {'session': '2026-11-04', 'state': 'WAITING_DATA'}}], [])
    assert rows[0]['role'] == '计划参考日' and rows[0]['close'] != '—'
    assert rows[1]['role'] == '执行交易日' and rows[1]['session'] == '2026-11-04'
    assert rows[1]['close'] == '—' and '尚无' in rows[1]['note']


def test_holdings_and_daily_pnl_explain_different_period_and_unknown_is_not_zero():
    view = {'valuation_session': '2026-11-03', 'holdings': [{'code': 'SH600000', 'shares': 100,
            'sellable_shares': 100, 'price': '9.80', 'price_date': '2026-11-03',
            'cost_fen': 100500, 'market_value_fen': 98000, 'floating_pnl_fen': -2500,
            'status': 'FINAL'}], 'stock_daily_pnl': {'session': '2026-11-03',
            'rows': [{'code': 'SH600000', 'daily_pnl_fen': None, 'state': 'UNAVAILABLE',
                      'reason': '前一交易日估值缺失'}], 'sum_fen': None,
            'account_daily_pnl_fen': -1000, 'unattributed_fen': None}}
    html = holdings_section(view)
    assert '不随上方交易日选择改变' in html
    assert '-25.00' in html and '累计持仓损益' in html
    assert '个股当日盈亏' not in html
    daily = daily_pnl_section('2026-11-03', view['stock_daily_pnl'])
    assert '前一交易日估值缺失' in daily and '<td>待核</td>' in daily


def test_user_table_translates_requested_statuses_without_changing_source_or_markup():
    rows = [{'status': 'NOT_FILLED', 'reason': 'LIMIT_NOT_MET', 'side': 'BUY'},
            {'status': 'COMPLETE', 'reason': '说明<script>alert(1)</script>', 'side': 'SELL'}]
    html = table(rows, [('status', '状态', False), ('reason', '原因', False), ('side', '方向', False)])
    assert all(code not in Text(html).value for code in ('NOT_FILLED', 'LIMIT_NOT_MET', 'BUY', 'COMPLETE'))
    assert '未成交' in html and '买入' in html and '卖出' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert rows[0]['status'] == 'NOT_FILLED'


def test_selected_day_contains_only_its_pnl_and_statement_only_days_are_selectable():
    view = {'daily_pnl_by_session': {
        '2026-11-02': {'rows': [{'code': 'SH600000', 'daily_pnl_fen': -501}],
                       'session': '2026-11-02', 'sum_fen': -501},
        '2026-11-03': {'rows': [{'code': 'SH600000', 'daily_pnl_fen': 2000}],
                       'session': '2026-11-03', 'sum_fen': 2000}}}
    html = trade_days({'target_session': '2026-11-04'},
                      {'plans': [{'target_session': '2026-11-04'}]}, view)
    assert all(f'<option value="{day}"' in html for day in (
        '2026-11-02', '2026-11-03', '2026-11-04'))
    parts = html.split('<div data-trade-day=')
    future, third, second = parts[1:]
    assert '等待该日最终日结' in future and '20.00' not in future and '-5.01' not in future
    assert 'data-pnl-session="2026-11-03"' in third and '20.00' in third and '-5.01' not in third
    assert 'data-pnl-session="2026-11-02"' in second and '-5.01' in second and '20.00' not in second


def test_remaining_buy_lots_show_original_execution_and_fee_inclusive_remaining_cost():
    view = {'valuation_session': '2026-11-03', 'holdings': [{'code': 'SH600000',
        'shares': 100, 'cost_fen': 100501, 'cost_price': '10.0501', 'buy_lots': [{
            'acquired_date': '2026-11-02', 'source_label': '买入成交', 'price': '10.00',
            'original_quantity': 200, 'original_cost_fen': 200502, 'amount_fen': 200000,
            'commission_fen': 500, 'transfer_fen': 2, 'stamp_fen': 0,
            'quantity': 100, 'cost_fen': 100501, 'state': 'READY'}]}]}
    html = holdings_section(view)
    assert '10.0501' in html and '10.00' in html and '2,005.02' in html and '1,005.01' in html
    assert 'data-public-section="buy-lots" data-public-key="SH600000"' in html
    assert '不等同于原始买入成交价' in html
    assert 'FIFO' in html and '截至 2026-11-03' in html


def test_history_controls_are_scoped_and_not_a_prebuilt_unbounded_html_table():
    history = {'snapshot_at': '2026-11-04T04:00:00+00:00', 'complete': True,
               'trade_chunks': [[{'code': 'PRIVATE_UNUSED', 'cells': ['unused']}]]}
    html = history_section(history, 'S1-all-trades.csv')
    assert 'PRIVATE_UNUSED' not in html
    for key in ('stock', 'status', 'rows', 'summary', 'head', 'summary-head',
                'prev', 'next', 'page', 'summary-prev', 'summary-next', 'summary-page'):
        assert 'data-history-' + key in html
    assert 'data-public-section="trade-history"' in html and 'S1-all-trades.csv' in html
    assert '不是包含分红' in html and '清仓后再次买入' in html
    assert '导出全部实际成交' not in history_section({'complete': False}, None)


def test_holdings_summary_pairs_original_buy_dates_prices_and_marks_non_buy_lots():
    holding = {'code': 'SH600000', 'buy_lots': [
        {'source_kind': 'BUY', 'acquired_date': '2026-10-01', 'price': '10.00'},
        {'source_kind': 'BUY', 'acquired_date': '2026-11-02', 'price': '12.30'},
        {'source_kind': 'EQUITY', 'acquired_date': '2026-11-03', 'price': None},
        {'source_kind': 'BUY', 'acquired_date': '2026-11-04', 'price': None},
    ], 'buy_lots_reason': '部分批次原始买入证据待核'}
    summary = holdings_section({'holdings': [holding]}).split('</table>', 1)[0]
    assert '买入日期' in summary and '买入价（元/股）' in summary
    assert '1. 2026-10-01；2. 2026-11-02；3. 权益到账（非买入）；4. 2026-11-04；批次待核' in summary
    assert '1. 10.00；2. 12.30；3. 权益到账（非买入）；4. 待核；批次待核' in summary
    assert '暂无可核对' not in summary
    empty = holdings_section({'holdings': [{'code': 'SH600000'}]}).split('</table>', 1)[0]
    assert '<td>待核</td><td>待核</td>' in empty
