"""Pure presentation upgrades retain recorded trading facts and export boundaries."""

import json
import re
from importlib.util import find_spec

import pytest

from china_stock_sim.reporting.ledger import apply_ledger_theme

if find_spec('china_stock_sim.publishing') is not None:
    from test_static_publishing import NOW, SAMPLE, bundle, snapshot, world

    assert world
else:
    SAMPLE = '2026-11-02T08:59:00+08:00'

    @pytest.fixture
    def world():
        pytest.skip('Local publishing component is not installed')


def legacy_page():
    selection = ('<select id="account"><option value="S1">S1</option>'
                 '<option value="LLM" selected>LLM &amp; 独立账户</option></select>')
    facts = [{'account': {'account_id': 'S1', 'cash_fen': 5000000},
              'account_revision': 7, 'report_revision': 8, 'snapshot_at': SAMPLE,
              'statements': [{'session': '2026-10-30', 'net_assets_fen': 5001200}],
              'orders': [{'code': 'SH600000', 'quantity': 100, 'status': 'FILLED'}],
              'fills': [{'price': '10.01', 'quantity': 100, 'commission_fen': 500}],
              'audit': {'input_bundle': {'private': 'PRIVATE_INPUT'}}}]
    contents = ('<p class="notice">保留默认账户提示</p>'
                '<section data-account="S1" hidden><h2>账户与策略</h2><p>政策：'
                + 'a' * 128 + '</p><div class="cards"><div><b>50,012.00</b>'
                '<span>净资产</span></div></div><div class="scroll"><table>'
                '<tr><th>现金</th><td>50,000.00</td></tr></table></div>'
                '<a href="S1-r2.csv">每日 CSV</a>'
                '<a href="../strategies/S1-audit.html">审计 HTML</a>'
                '<a href="../strategies/S1-audit.json">审计 JSON</a>'
                '<details><summary>计算</summary><pre>PRIVATE_CALC &lt;原件&gt;</pre></details>'
                '</section><section data-account="LLM"><p>独立账本</p></section>'
                '<footer>最后生成：<time id="updated" datetime="' + SAMPLE + '">'
                + SAMPLE + '</time><span id="age"></span></footer>'
                '<script type="application/json" id="report-data">'
                + json.dumps(facts, ensure_ascii=False) + '</script>'
                '<script>document.getElementById("account").onchange=e=>'
                '{document.querySelectorAll("[data-account]").forEach(s=>'
                's.hidden=s.dataset.account!==e.target.value)};</script></main></body></html>')
    source = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>每日账本</title><style>body{background:#f5f3ed}</style></head><body><main>'
              '<h1>每日账本</h1><p>旧页眉</p><label>当前账户 ' + selection + '</label>' + contents)
    return source, selection, contents, facts


def test_theme_upgrade_preserves_recorded_account_sections_data_exports_and_generation_time():
    source, selection, contents, facts = legacy_page()
    result = apply_ledger_theme(source)
    assert result.endswith(contents)  # All financial content, audit links, data and JS are exact bytes.
    assert selection in result
    assert result.count('id="account"') == 1
    assert result.count('class="ledger-header"') == 1
    assert result.count('href="../index.html"') == 1
    assert '<label for="account">' in result
    assert 'aria-label="看板导航"' in result
    assert '<section data-account="S1" hidden>' in result
    assert '<section data-account="LLM">' in result
    assert '<option value="LLM" selected>' in result
    assert apply_ledger_theme(result) == result
    assert legacy_page()[0] == source
    embedded = re.search(r'id="report-data">(.*?)</script>', result).group(1)
    assert json.loads(embedded) == facts


def test_theme_has_home_palette_responsive_container_and_long_identity_wrapping():
    result = apply_ledger_theme(legacy_page()[0])
    css = re.search(r'<style id="ledger-theme">(.*?)</style>', result, re.S).group(1)
    for contract in ('max-width:1180px', '#f3f6fa', '#17283c', '#214f89',
                     '@media(max-width:560px)', 'repeat(2,minmax(0,1fr))',
                     'overflow-wrap:anywhere', 'min-width:0', 'overflow:auto',
                     '[hidden]{display:none!important}', ':focus-visible'):
        assert contract in css
    assert not re.search(r'url\s*\(|@import|https?://|file:', css, re.I)


@pytest.mark.parametrize('bad_part', ['<style>body{background:#f5f3ed}</style>',
                                     'id="account"', 'id="report-data"', '<main>'])
def test_unsafe_or_incomplete_page_is_not_silently_restyled(bad_part):
    page = legacy_page()[0].replace(bad_part, '')
    with pytest.raises(ValueError):
        apply_ledger_theme(page)


def test_cloud_themed_page_has_one_home_navigation_and_readable_exact_times(world):
    source, _, _, facts = legacy_page()
    page = apply_ledger_theme(source)
    (world.root / 'reports/forward_current/index.html').write_text(page)
    before = snapshot(world.root)
    result = bundle(world)
    html = result.files['trading.html'].decode()
    assert snapshot(world.root) == before
    assert html.count('href="index.html"') == 1
    assert html.count('class="cloud-publication"') == 1
    assert html.index('<main>') < html.index('class="ledger-header"')
    assert html.index('</header>') < html.index('class="cloud-publication"')
    assert html.index('class="cloud-publication"') < html.index('class="account-toolbar"')
    assert '<style id="cloud-publication-theme">' in html
    for exact, short in ((SAMPLE, '11-02 08:59'), (NOW.isoformat(), '11-02 09:00'),
                         ('2026-11-02T21:00:00+08:00', '11-02 21:00')):
        assert f'<time datetime="{exact}" title="{exact}">{short}</time>' in html
    assert '每日 09:00、21:00 · 北京时间 · 完整审计请在本机查看' in html
    assert 'PRIVATE_CALC' not in html and 'PRIVATE_INPUT' not in html
    assert 'data-next-due="2026-11-02T21:00:00+08:00"' in html
    assert '等待本机更新' in html and '同步期限未知' in html
    embedded = json.loads(re.search(r'id="report-data"[^>]*>(.*?)</script>', html, re.S).group(1))
    assert embedded == [{key: value for key, value in facts[0].items() if key != 'audit'}]
    assert b'account,cash\nS1,5000000\n' in result.files.values()
    assert 'S1-audit.html' in page and 'S1-audit.json' in page
    assert bundle(world).files == result.files


def test_legacy_cloud_page_without_header_gets_one_fallback_home_navigation(world):
    html = bundle(world).files['trading.html'].decode()
    assert html.count('href="index.html"') == 1
    assert html.count('class="cloud-publication"') == 1
    assert html.index('<body>') < html.index('class="cloud-publication"') < html.index('id="account"')
    assert '50,000.00' in html and 'SH600000' in html
