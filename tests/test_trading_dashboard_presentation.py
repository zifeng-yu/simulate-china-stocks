"""TU03/TU04: human labels, exact values and honest static chart gaps."""

from copy import deepcopy
from html.parser import HTMLParser
from math import isfinite

import pytest

from china_stock_sim.reporting.trading_presentation import (
    asset_charts,
    display_cell,
    price,
    status_label,
    when,
)


class Chart(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def statement(day, *, assets=5_000_000, cash=3_000_000, market=2_000_000,
              pnl=0, valuation='FINAL', revision=1, **extra):
    return {'session': day, 'accounting_status': 'COMPLETE', 'valuation_status': valuation,
            'statement_revision': revision, 'net_assets_fen': assets,
            'closing_cash_fen': cash, 'market_value_fen': market,
            'cumulative_pnl_fen': pnl, **extra}


@pytest.mark.parametrize(('code', 'label'), [
    ('COMPLETE', '已完成'), ('FINAL', '最终估值'),
    ('MISSED_CUTOFF', '已过该交易日09:00计划截止时间'),
    ('BUY', '买入'), ('SELL', '卖出'), ('NOT_FILLED', '未成交'),
    ('LIMIT_NOT_MET', '未满足限价成交条件'), ('INITIAL_CAPITAL', '初始注资'),
    ('DIVIDEND_TAX', '红利税扣缴'), ('WAITING_ALLOCATION', '等待股份分配'),
    ('RELEASED', '预留已释放'), ('ALREADY_APPLIED', '已处理，复用结果'),
])
def test_required_and_related_codes_are_human_labels(code, label):
    assert status_label(code) == label
    assert display_cell('status', code) == label


def test_only_enum_fields_translate_and_free_prose_remains_content():
    assert display_cell('code', 'SH600519') == 'SH600519'
    assert display_cell('strategy_id', 'BUY') == 'BUY'
    assert display_cell('reason', '持仓不足，等待下一次检查') == '持仓不足，等待下一次检查'
    assert status_label('NEW_UNDOCUMENTED_STATE') == '状态待解释（详见审计记录）'
    assert 'NEW_UNDOCUMENTED_STATE' not in display_cell('reason_code', 'NEW_UNDOCUMENTED_STATE')
    assert status_label({'kind': 'MISSED_CUTOFF'}) == status_label('MISSED_CUTOFF')
    assert status_label(['BUY', 'SELL']) == '买入；卖出'
    assert display_cell('limit_price', None) == status_label(None) == '—'


@pytest.mark.parametrize(('value', 'expected'), [
    (None, '—'), ('NaN', '—'), ('Infinity', '—'), (True, '—'),
    ('8.7000', '8.70'), ('10.123456', '10.123456'), ('4', '4.00'), (0, '0.00'),
])
def test_price_keeps_recorded_precision_without_float_rounding(value, expected):
    assert price(value) == expected


def test_two_charts_have_explicit_axes_legends_exact_point_values_and_are_pure():
    rows = [statement('2026-11-02'), statement('2026-11-03', assets=4_973_581, pnl=-26_419)]
    original = deepcopy(rows)
    html = asset_charts(rows, 5_000_000)
    assert rows == original
    assert '资产构成走势' in html and '累计盈亏走势' in html
    assert html.count('金额（元）') == html.count('>交易日期</text>') == 2
    for text in ('总资产', '现金', '持仓市值', '累计盈亏', '2026-11-02', '2026-11-03',
                 '总资产：49,735.81 元', '累计盈亏：-264.19 元', '初始本金 50,000.00 元'):
        assert text in html
    parsed = Chart(html)
    assert len([1 for tag, attrs in parsed.elements if tag == 'svg'
                and attrs.get('class') == 'financial-chart']) == 2
    assert len([1 for tag, _ in parsed.elements if tag == 'polyline']) == 4
    assert any(attrs.get('class') == 'chart-zero' for _, attrs in parsed.elements)
    assert all(attrs.get('tabindex') == '0' and attrs.get('aria-label')
               for tag, attrs in parsed.elements if tag == 'circle')
    assert '<script' not in html and 'https://' not in html


def test_null_provisional_restatement_and_missing_dates_break_lines_without_zero_points():
    rows = [statement('2026-11-02'), statement('2026-11-03', valuation='PROVISIONAL'),
            statement('2026-11-04', assets=None, pnl=None),
            statement('2026-11-05', requires_restatement=True),
            statement('2026-11-06'), statement('2026-11-09')]
    html = asset_charts(rows, 5_000_000)
    parsed = Chart(html)
    assert not any(tag == 'polyline' for tag, _ in parsed.elements)
    labels = [attrs['aria-label'] for tag, attrs in parsed.elements if tag == 'circle']
    assert not any('2026-11-03' in label or '2026-11-05' in label for label in labels)
    assert not any('2026-11-04' in label and ('总资产' in label or '累计盈亏' in label)
                   for label in labels)
    assert '未知金额不记为零' not in html  # Other valid points remain visible.
    assert '非相邻日期也不推定连续' in html


def test_newer_provisional_revision_does_not_resurrect_older_final_row():
    html = asset_charts([statement('2026-11-02', revision=1),
                         statement('2026-11-02', revision=2, valuation='PROVISIONAL')], 5_000_000)
    assert html.count('尚无可用的最终账单') == 2
    assert '<circle' not in html


def test_empty_single_flat_and_negative_values_are_explained_and_have_finite_geometry():
    assert asset_charts([], None).count('尚无可用的最终账单') == 2
    single = asset_charts([statement('2026-11-02')], 5_000_000)
    assert single.count('目前只有一个有效估值日') == 2
    assert '<circle' in single and '<polyline' not in single
    flat = asset_charts([statement('2026-11-02'), statement('2026-11-03')], 5_000_000)
    assert all(isfinite(float(value)) for _, attrs in Chart(flat).elements
               for key, value in attrs.items() if key in {'cx', 'cy', 'x1', 'x2', 'y1', 'y2'})
    negative = asset_charts([statement('2026-11-02', assets=-100, cash=-100, market=0,
                                       pnl=-5_000_100)], 5_000_000)
    assert '总资产：-1.00 元' in negative and '累计盈亏：-50,001.00 元' in negative
    assert all(isfinite(float(value)) for _, attrs in Chart(negative).elements
               for key, value in attrs.items() if key in {'cx', 'cy', 'x1', 'x2', 'y1', 'y2'})


def test_invalid_dates_and_nonfinite_amounts_are_not_embedded_as_svg_or_numbers():
    html = asset_charts([statement('<script>alert(1)</script>'),
                         statement('2026-11-02', assets='NaN', cash='Infinity',
                                   market=None, pnl=None)], 5_000_000)
    assert '<script>' not in html and '<circle' not in html
    assert html.count('尚无可用的最终账单') == 2


def test_browser_followup_actual_execution_and_input_labels():
    assert display_cell('reason', 'OPEN_PRICE_MATCH') == '开盘价满足限价，已成交'
    assert display_cell('state', 'WAITING_INPUTS') == '等待必要输入'


def test_browser_followup_aware_timestamps_are_beijing_time_without_rewriting_other_fields():
    utc = '2026-11-03T13:47:07.123456+00:00'
    expected = '2026-11-03 21:47:07'
    assert when(utc) == when('2026-11-03T21:47:07+08:00') == expected
    assert when('2026-11-03T23:30:00Z') == '2026-11-04 07:30:00'
    for key in ('processed_at', 'recorded_at', 'snapshot_at', 'frozen_at', 'knowledge_cutoff'):
        assert display_cell(key, utc) == expected
    assert display_cell('reason', '原始记录时间：' + utc) == '原始记录时间：' + utc
    assert display_cell('target_session', '2026-11-03') == '2026-11-03'


def test_browser_followup_absent_invalid_and_naive_timestamps_are_not_guessed():
    assert when(None) == when('') == '—'
    assert when('2026-11-03T13:47:07') == '时间待核（未注明时区）'
    assert when('2026-11-03') == '时间待核（未注明时区）'
    assert when('<script>alert(1)</script>') == when(True) == '时间待核'


def test_chart_viewport_followup_endpoint_dates_anchor_inward_and_fit_narrow_canvas():
    rows = [statement(f'2026-11-{day:02}') for day in range(2, 10)]
    parsed = Chart(asset_charts(rows, 5_000_000))
    charts = [attrs for tag, attrs in parsed.elements if tag == 'svg'
              and attrs.get('class') == 'financial-chart']
    assert len(charts) == 2
    assert all(attrs['viewbox'] == '0 0 520 280' for attrs in charts)
    ticks = [attrs for tag, attrs in parsed.elements if tag == 'text'
             and attrs.get('class') == 'chart-date-tick']
    assert len(ticks) == 6  # Three readable full-date ticks per chart, not five crowded ones.
    for group in (ticks[:3], ticks[3:]):
        assert [tick['text-anchor'] for tick in group] == ['start', 'middle', 'end']
        assert float(group[0]['x']) == 100 and float(group[-1]['x']) == 496
        assert all(float(tick['font-size']) == 16 for tick in group)
        # Reserve a conservative 10 glyphs × 0.65em for each numeric ISO label.
        bounds = []
        for tick in group:
            width = 10 * float(tick['font-size']) * .65
            x = float(tick['x'])
            left = x if tick['text-anchor'] == 'start' else (
                x - width if tick['text-anchor'] == 'end' else x - width / 2)
            bounds.append((left, left + width))
        assert all(0 <= start < end <= 520 for start, end in bounds)
        assert all(a[1] < b[0] for a, b in zip(bounds, bounds[1:]))


def test_chart_viewport_followup_single_date_stays_centered_and_integer_axis_is_compact():
    html = asset_charts([statement('2026-11-02')], 5_000_000)
    parsed = Chart(html)
    ticks = [attrs for tag, attrs in parsed.elements if tag == 'text'
             and attrs.get('class') == 'chart-date-tick']
    assert len(ticks) == 2
    assert all(tick['text-anchor'] == 'middle' and float(tick['x']) == 298 for tick in ticks)
    assert '>60,000</text>' in html and '>60,000.00</text>' not in html
    assert '总资产：50,000.00 元' in html  # Detailed point value retains the cents precision.
