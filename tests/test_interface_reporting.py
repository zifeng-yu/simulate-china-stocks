"""SI01: persistent accounts and frozen identities in offline HTML/CSV/JSON."""

import csv
import json
from dataclasses import replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pytest
from execution_fixtures import FIRST, advance, execution_book
from test_execution_decision_audit import freeze, publish, strategy_request, tree
from test_execution_integration import settle
from test_ledger_cross_layer import close
from test_ledger_trading import Facts, book, buy_and_close, call

from china_stock_sim.config import load_settings
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.store import initialize, read_snapshot
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.reporting import audit as audit_module
from china_stock_sim.reporting.audit import account_filename
from china_stock_sim.reporting.initial import write_initial_report
from china_stock_sim.reporting.ledger import write_ledger_report
from china_stock_sim.reporting.presentation import csv_cell, identity_html

assert execution_book and book


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.selected, self.visible, self.links, self.external, self.scripts = [], [], [], [], {}
        self.current = None
        self.current_select = None
        self.account_links, self.current_account = {}, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.current_select = attrs.get('id')
        if tag == 'option' and 'selected' in attrs and self.current_select == 'account':
            self.selected.append(attrs['value'])
        if tag == 'section' and 'data-account' in attrs and 'hidden' not in attrs:
            self.visible.append(attrs['data-account'])
        if tag == 'section' and 'data-account' in attrs:
            self.current_account = attrs['data-account']
            self.account_links[self.current_account] = []
        if tag == 'a':
            self.links.append(attrs['href'])
            if self.current_account is not None:
                self.account_links[self.current_account].append(attrs['href'])
        if 'src' in attrs:
            self.external.append(attrs['src'])
        if tag == 'script' and attrs.get('type') == 'application/json':
            self.current = attrs['id']
            self.scripts[self.current] = ''

    def handle_endtag(self, tag):
        if tag == 'select':
            self.current_select = None
        if tag == 'script':
            self.current = None
        if tag == 'section':
            self.current_account = None

    def handle_data(self, data):
        if self.current:
            self.scripts[self.current] += data


def exports(settings):
    return [json.loads(path.read_text()) for path in (settings.root / 'reports/strategies').glob('*.json')]


class AuditView(HTMLParser):
    """Extract rendered text without treating escaped markup as executable HTML."""

    def __init__(self, text):
        super().__init__()
        self.elements, self.text, self.in_data = [], [], False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag == 'pre' and attrs.get('id') == 'audit-data':
            self.in_data = True

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.in_data = False

    def handle_data(self, data):
        if self.in_data:
            self.text.append(data)


def assert_audit_views(settings, page):
    for output in (settings.root / 'reports/strategies').glob('*.json'):
        expected = json.loads(output.read_text())
        view = output.with_suffix('.html')
        parsed = AuditView(view.read_text())
        assert json.loads(''.join(parsed.text)) == expected
        links = page.account_links[expected['account_id']]
        assert '../strategies/' + view.name in links
        assert '../strategies/' + output.name in links


def test_initial_page_four_persistent_accounts_principal_formula_fallback(project):
    config = project / 'configs/default.toml'
    config.write_text(config.read_text().replace('initial_cash_fen = 5000000',
        'initial_cash_fen = 1234567', 1) + '\n[[accounts]]\nid = "ALT"\nstrategy = "S1"\ninitial_cash_fen = 7654321\n')
    settings = replace(load_settings(project), default_account='removed')
    initialize(settings)
    snapshot = read_snapshot(settings)
    before = settings.database.read_bytes()
    write_initial_report(settings, snapshot)
    text = settings.report.read_text()
    page = Page(text)
    assert page.selected == page.visible == ['ALT']
    assert '4 个独立虚拟账户' in text and '默认账户不可用' in text
    assert '初始注资 76,543.21' in text and '12,345.67' in text
    assert '当前资产 76,543.21' in text
    assert "document.getElementById('formula').textContent" in text
    assert '三个' not in text and '各自本金 50,000' not in text
    assert json.loads(page.scripts['accounts']) == snapshot['accounts']
    assert len(exports(settings)) == 4
    assert_audit_views(settings, page)
    assert all(item['plans'] == [] and item['scope'] == 'FROZEN_PLANS_ONLY' for item in exports(settings))
    assert page.external == [] and settings.database.read_bytes() == before


def test_active_page_lists_all_persistent_accounts_with_different_principal(project):
    config = project / 'configs/default.toml'
    config.write_text(config.read_text() + '\n[[accounts]]\nid = "ALT"\nstrategy = "S1"\ninitial_cash_fen = 7654321\n')
    settings = load_settings(project)
    ledger = LedgerService(settings, market=Facts(),
        clock=lambda: datetime.fromisoformat('2026-10-29T20:00:00+08:00'), test_mode=True)
    ledger.initialize()
    assert call(ledger, 'activate_simulation', {'target_session': FIRST}, account='ALT')['state'] == 'APPLIED'
    before = settings.database.read_bytes()
    write_ledger_report(settings, read_snapshot(settings), ledger=ledger)
    page = Page(settings.report.read_text())
    reports = json.loads(page.scripts['report-data'])
    assert len(reports) == len(exports(settings)) == 4
    assert_audit_views(settings, page)
    assert next(r for r in reports if r['account']['account_id'] == 'ALT')['account']['initial_cash_fen'] == 7654321
    assert '76,543.21' in settings.report.read_text()
    assert settings.database.read_bytes() == before


@pytest.mark.parametrize('state', ['INITIALIZED_NOT_RUNNING', 'ACTIVE'])
def test_no_accounts_gives_controlled_error(settings, state):
    with pytest.raises(ValueError, match='账户'):
        write_ledger_report(settings, {'state': state, 'accounts': []})
    assert not settings.report.exists()


def test_initial_account_text_and_filename_are_safe(settings):
    malicious = '../../x"</script><script>alert(1)</script>'
    row = {'account_id': malicious, 'strategy_id': '<script>bad()</script>',
           'initial_cash_fen': 100000, 'cash_fen': 100000}
    snapshot = {'accounts': [row]}
    write_initial_report(settings, snapshot)
    text = settings.report.read_text()
    page = Page(text)
    assert page.selected == [malicious]
    assert '</script><script>alert(1)</script>' not in text
    assert '<script>bad()</script>' not in text
    assert json.loads(page.scripts['accounts']) == [row]
    assert page.external == []
    assert len(exports(settings)) == 1
    assert (settings.root / 'reports/strategies' / (account_filename(malicious) + '-audit.json')).is_file()


def test_template_markers_remain_literal_account_ids_and_large_money_is_exact(project):
    config = project / 'configs/default.toml'
    config.write_text(config.read_text().replace('id = "S1"', 'id = "__CASH__"', 1)
        .replace('id = "S1-LT"', 'id = "__INITIAL__"', 1)
        .replace('initial_cash_fen = 5000000', 'initial_cash_fen = 9223372036854775807', 1)
        + '\n[[accounts]]\nid = "__OPTIONS__"\nstrategy = "S1"\ninitial_cash_fen = 7654321\n')
    settings = replace(load_settings(project), default_account='__CASH__')
    initialize(settings)
    snapshot = read_snapshot(settings)
    write_initial_report(settings, snapshot)
    text = settings.report.read_text()
    page = Page(text)
    assert page.selected == page.visible == ['__CASH__']
    assert json.loads(page.scripts['accounts']) == snapshot['accounts']
    display = json.loads(page.scripts['account-display'])
    assert {row['account_id'] for row in display} == {'LLM', '__CASH__', '__INITIAL__', '__OPTIONS__'}
    large = next(row for row in display if row['account_id'] == '__CASH__')
    assert large['initial'] == large['cash'] == '92,233,720,368,547,758.07'
    assert '初始注资 92,233,720,368,547,758.07 + 后续现金流水 0.00' in large['formula']
    assert "textContent=a.cash" in text and "textContent=a.formula" in text
    assert 'toLocaleString' not in text and 'a.initial_cash_fen' not in text


def test_active_fallback_manual_identity_and_no_model_attempt_text(book, monkeypatch):
    buy_and_close(book)
    monkeypatch.setattr(book.market, 'index_quote', lambda *a, **k: DataResult('MISSING', []), raising=False)
    settings = replace(book.settings, default_account='removed')
    before = settings.database.read_bytes()
    write_ledger_report(settings, read_snapshot(settings), ledger=book)
    text = settings.report.read_text()
    page = Page(text)
    assert page.selected == page.visible == ['LLM']
    assert '默认账户不可用' in text and '人工来源' in text
    assert '当时模型：' not in text
    reports = json.loads(page.scripts['report-data'])
    assert all('model_attempts' not in r for r in reports)
    assert '三个独立' not in text and '各自本金50,000' not in text
    assert settings.database.read_bytes() == before


def fixture_strategy(book, *, empty=False, nested=False):
    request, core = strategy_request(book, empty=empty, nested=nested)
    core['calculation']['identity'] = {'strategy_name': '当时的规则与模型',
        'program_revision': 'program-frozen', 'prompt_revision': 'prompt-frozen',
        'uses_model': True, 'model': {'model': 'frozen-model', 'provider': 'fixture',
                                    'generation_parameters': {'temperature': '0.2'}}}
    core['calculation']['reason'] = '<script>sample reason</script>'
    request = publish(book, request, core)
    return freeze(book, request), core, request


def test_frozen_history_csv_json_same_identity_despite_configuration_drift(execution_book):
    book = execution_book
    plan, core, _ = fixture_strategy(book)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    close(book, FIRST)
    before = book.settings.database.read_bytes()
    book.settings.raw['strategies'] = {'replacement': {'version': 'today-replacement'}}
    write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    text = book.settings.report.read_text()
    page = Page(text)
    assert all(value in text for value in ('frozen-model', 'program-frozen', 'prompt-frozen'))
    assert 'today-replacement' not in text
    assert '<script>sample reason</script>' not in text
    report = next(r for r in json.loads(page.scripts['report-data']) if r['account']['account_id'] == 'LLM')
    audit = next(a for a in exports(book.settings) if a['account_id'] == 'LLM')
    summary = report['audit']
    assert summary['projection_scope'] == 'HOMEPAGE_SUMMARY'
    for key in ('account_id', 'state', 'issues'):
        assert summary[key] == audit[key]
    assert audit == audit_module.audit_account(book.root, book.report_snapshot('LLM', mode='recent'), materialize=False)
    for kind in ('orders', 'fills'):
        assert summary[kind] == report[kind]
        for shown, source in zip(summary[kind], audit[kind], strict=True):
            for field in ('code', 'side', 'quantity', 'reason', 'target_session', 'strategy_identity', 'audit_state'):
                assert shown[field] == source[field]
            if kind == 'orders':
                for field in ('limit_price', 'status', 'reserved_cash_fen', 'reserved_shares'):
                    assert shown[field] == source[field]
                for field in ('state', 'reason', 'reason_code', 'reason_details', 'quantity', 'price',
                              'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen', 'net_cash_fen'):
                    assert shown['result'][field] == source['result'][field]
                assert 'account_view_evidence' not in source['result']
                assert 'account_view_evidence' not in shown['result']
            else:
                for field in ('price', 'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen',
                              'fees_fen', 'net_cash_fen', 'cost_fen', 'realized_pnl_fen', 'lot_allocations'):
                    assert shown[field] == source[field]
                assert 'account_view_evidence' not in source
                assert 'account_view_evidence' not in shown
    assert summary['plans'][0]['identity'] == audit['plans'][0]['identity']
    assert summary['plans'][0]['audit_state'] == audit['plans'][0]['audit_state']
    assert 'input_bundle' not in summary['plans'][0]
    assert audit['plans'][0]['input_bundle']['projection'] == 'REFERENCE_ONLY'
    full = audit_module.audit_account(book.root, book.report_snapshot('LLM'), materialize=True)
    assert full['plans'][0]['input_bundle'] == core['input_bundle']
    assert_audit_views(book.settings, page)
    historical = audit['plans'][0]['identity']
    assert historical['parameters'] == core['account_policy']['parameters']
    assert historical['program_revision'] == 'program-frozen'
    assert audit['orders'][0]['strategy_identity'] == historical
    assert audit['fills'][0]['strategy_identity'] == historical
    csv_path = book.settings.report.parent / f'LLM-r{report["report_revision"]}.csv'
    with csv_path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]['account_id'] == 'LLM' and rows[0]['initial_cash_fen'] == '5000000'
    assert rows[0]['strategy_version'] == historical['strategy_version']
    assert rows[0]['account_policy_hash'] == historical['account_policy_hash']
    assert rows[0]['decision_id'] == core['decision_id']
    assert book.settings.database.read_bytes() == before


def test_empty_plan_remains_visible_and_missing_original_only_degrades_audit(execution_book):
    book = execution_book
    _, _, request = fixture_strategy(book, empty=True)
    before = book.settings.database.read_bytes()
    write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    text = book.settings.report.read_text()
    assert 'explicit fixture HOLD' in text and '无订单原因' in text
    assert_audit_views(book.settings, Page(text))
    (book.root / request['decision_ref']['decision_core']['path']).unlink()
    write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    text = book.settings.report.read_text()
    assert 'AUDIT_UNAVAILABLE' in text and '每日资产与资金计算' in text
    assert 'frozen-model' not in text
    assert_audit_views(book.settings, Page(text))
    audit = next(a for a in exports(book.settings) if a['account_id'] == 'LLM')
    assert audit['state'] == 'PARTIAL'
    assert book.settings.database.read_bytes() == before


def test_no_plan_run_record_visible_with_blank_csv_identity(execution_book):
    book = execution_book
    assert call(book, 'record_no_plan', {'target_session': FIRST, 'reason': 'MODEL_FAILED'})['state'] == 'APPLIED'
    advance(book, FIRST)
    close(book, FIRST)
    write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    text = book.settings.report.read_text()
    assert '未生成计划的运行记录' in text and 'MODEL_FAILED' in text
    audit = next(a for a in exports(book.settings) if a['account_id'] == 'LLM')
    assert audit['plans'] == [] and len(audit['no_plan_records']) == 1
    report = book.report_snapshot('LLM')
    with (book.settings.report.parent / f'LLM-r{report["report_revision"]}.csv').open(
            encoding='utf-8-sig', newline='') as stream:
        row = next(csv.DictReader(stream))
    assert row['strategy_id'] == '' and row['identity_note'] == '当日无已冻结计划'


@pytest.mark.parametrize('uses_model', [False, None])
def test_models_not_displayed_when_capability_not_explicit(uses_model):
    html = identity_html({'strategy_id': 'future', 'strategy_version': 'frozen',
        'uses_model': uses_model, 'model': {'model': 'must-not-show'}})
    assert 'must-not-show' not in html


@pytest.mark.parametrize('text', ['=1+1', '+cmd', '-1+2', '@SUM(A1)', '\tformula', '\rformula', '  =1'])
def test_csv_dangerous_text_does_not_become_formula(text):
    assert csv_cell(text) == "'" + text


def test_report_publication_failure_preserves_previous_page_and_ledger(settings, monkeypatch):
    initialize(settings)
    settings.report.parent.mkdir(parents=True)
    settings.report.write_text('previous page')
    before = settings.database.read_bytes()
    monkeypatch.setattr('china_stock_sim.reporting.initial.atomic_text',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('full')))
    with pytest.raises(OSError, match='full'):
        write_initial_report(settings, read_snapshot(settings))
    assert settings.report.read_text() == 'previous page'
    assert settings.database.read_bytes() == before


def test_symlink_page_destination_cannot_write_outside_reports(settings, tmp_path):
    initialize(settings)
    settings.report.parent.mkdir(parents=True)
    outside = tmp_path / 'outside.html'
    outside.write_text('unchanged')
    settings.report.symlink_to(outside)
    with pytest.raises(ValueError):
        write_initial_report(settings, read_snapshot(settings))
    assert outside.read_text() == 'unchanged'


def test_audit_view_preserves_unicode_numbers_and_safely_displays_markup(execution_book):
    book = execution_book
    fixture_strategy(book)
    audit = audit_module.audit_account(book.root, book.report_snapshot('LLM', FIRST))
    audit['account']['display_name'] = '中文账户🧾 "引号" \\路径'
    audit['plans'][0]['calculation']['中文依据'] = [
        '持有𠀀', '</pre><script>alert(1)</script><img src="x" onerror="alert(2)">',
        '换行\n制表\t & 引号"', 2**63 - 1,
    ]
    audit['public_notes'] = {'client_secret': 'synthetic-private-secret', 'reason': '中文保留'}
    original = json.loads(json.dumps(audit))
    before = tree(book.root)
    destination = Path('reports/strategies/中文 #audit.html')
    json_path = destination.with_suffix('.json')
    audit_module.write_audit_output(book.root, json_path, audit)
    result = audit_module.write_audit_view(book.root, destination, audit)
    assert result == str(destination)
    text = (book.root / result).read_text(encoding='utf-8')
    parsed = AuditView(text)
    expected = json.loads((book.root / json_path).read_text())
    assert json.loads(''.join(parsed.text)) == expected
    assert '中文账户🧾' in text and '持有𠀀' in text and '\\u4e2d' not in text
    assert 'synthetic-private-secret' not in text
    assert audit == original
    assert not any(tag in {'script', 'img', 'iframe', 'object', 'link', 'base'}
                   for tag, _ in parsed.elements)
    assert any(tag == 'meta' and attrs.get('charset', '').lower() == 'utf-8'
               for tag, attrs in parsed.elements)
    assert any(tag == 'meta' and attrs.get('name') == 'viewport'
               for tag, attrs in parsed.elements)
    assert [attrs['href'] for tag, attrs in parsed.elements if tag == 'a'] == [
        quote(json_path.name, safe='')]
    after = tree(book.root)
    for output in (str(destination), str(json_path)):
        after.pop(output)
    assert after == before


@pytest.mark.parametrize('destination', [
    'data/state/overwrite.html', 'reports/strategies/../../data/overwrite.html',
    'reports/strategies/result.json',
])
def test_audit_view_rejects_unsafe_destination_without_writing(execution_book, destination):
    book = execution_book
    audit = audit_module.audit_account(book.root, book.report_snapshot('LLM', FIRST))
    before = tree(book.root)
    with pytest.raises(ValueError):
        audit_module.write_audit_view(book.root, destination, audit)
    assert tree(book.root) == before


@pytest.mark.parametrize('symlink_directory', [False, True])
def test_audit_view_rejects_symlink_escape(execution_book, tmp_path, symlink_directory):
    book = execution_book
    audit = audit_module.audit_account(book.root, book.report_snapshot('LLM', FIRST))
    outside = tmp_path / 'outside'
    outside.mkdir()
    existing = outside / 'audit.html'
    existing.write_text('unchanged')
    destination = book.root / 'reports/strategies/audit.html'
    destination.parent.parent.mkdir(exist_ok=True)
    if symlink_directory:
        destination.parent.symlink_to(outside, target_is_directory=True)
    else:
        destination.parent.mkdir()
        destination.symlink_to(existing)
    before = tree(book.root)
    with pytest.raises(ValueError):
        audit_module.write_audit_view(book.root, destination, audit)
    assert existing.read_text() == 'unchanged'
    assert tree(book.root) == before


def test_audit_view_atomic_failure_preserves_previous_view_and_business_data(
        execution_book, monkeypatch):
    book = execution_book
    fixture_strategy(book)
    audit = audit_module.audit_account(book.root, book.report_snapshot('LLM', FIRST))
    destination = book.root / 'reports/strategies/LLM-audit.html'
    destination.parent.mkdir(parents=True)
    destination.write_text('previous view')
    before = tree(book.root)

    def fail_replace(source, target):
        assert target == destination
        raise OSError('publication failure')

    monkeypatch.setattr('china_stock_sim.market.storage.os.replace', fail_replace)
    with pytest.raises(OSError, match='publication failure'):
        audit_module.write_audit_view(book.root, destination, audit)
    assert tree(book.root) == before
    assert not list(destination.parent.glob('.writing-*'))


@pytest.mark.parametrize('suffix', ['.json', '.html'])
def test_audit_report_rejects_internal_alias_before_any_publication(settings, suffix):
    initialize(settings)
    snapshot = read_snapshot(settings)
    directory = settings.root / 'reports/strategies'
    directory.mkdir(parents=True)
    alias = directory / ('alias' + suffix)
    alias.write_text('previous alias')
    linked = directory / ('LLM-audit' + suffix)
    linked.symlink_to(alias.name)
    counterpart = directory / ('LLM-audit' + ('.html' if suffix == '.json' else '.json'))
    counterpart.write_text('previous counterpart')
    settings.report.parent.mkdir(parents=True)
    settings.report.write_text('previous dashboard')
    before = tree(settings.root)

    with pytest.raises(ValueError, match='符号链接'):
        write_initial_report(settings, snapshot)

    assert linked.is_symlink() and linked.readlink() == Path(alias.name)
    assert tree(settings.root) == before
    assert settings.report.read_text() == 'previous dashboard'
