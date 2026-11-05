"""Production-sized evidence remains in audit exports, not the landing page JSON."""

import csv
import hashlib
import json
from copy import deepcopy

import pytest
from execution_fixtures import FIRST, advance, execution_book
from test_execution_decision_audit import freeze, strategy_request
from test_execution_integration import settle
from test_interface_reporting import AuditView, Page
from test_ledger_cross_layer import close

from china_stock_sim.ledger.store import read_snapshot
from china_stock_sim.reporting import ledger as reporting

assert execution_book


def test_homepage_summary_size_is_independent_of_original_document_bodies():
    audit = {'account_id': 'S1', 'state': 'COMPLETE', 'orders': [], 'fills': [],
        'issues': [], 'no_plan_records': [{'reason': 'WAITING_DATA'}], 'plans': [{
            'plan_id': 'plan-1', 'target_session': '2026-11-02', 'status': 'FROZEN',
            'empty_reason': 'HOLD', 'identity': {'strategy_id': 'S1', 'parameters': {'size': 5}},
            'audit_state': 'COMPLETE', 'audit_issue': None,
            'evidence_files': [{'path': 'data/plans/strategy/example.json', 'sha256': 'a' * 64}],
            'plan': {'source_proposal': {}}, 'input_bundle': {'prices': []},
            'calculation': {}, 'referenced_documents': {}}]}
    expected = reporting.homepage_audit(audit)
    original = deepcopy(audit)
    large = 'immutable-source-body-' * 100_000
    for key in ('plan', 'input_bundle', 'calculation', 'referenced_documents', 'future_evidence'):
        audit['plans'][0][key] = {'body': large}
    audit['future_evidence'] = {'body': large}
    before = deepcopy(audit)
    actual = reporting.homepage_audit(audit)
    assert actual == expected
    assert len(json.dumps(actual)) < 2000
    assert audit == before
    actual['plans'][0]['identity']['parameters']['size'] = 99
    actual['no_plan_records'][0]['reason'] = 'changed copy'
    assert audit == before
    assert original['plans'][0]['identity'] == expected['plans'][0]['identity']


@pytest.mark.parametrize('on_target_day', [False, True])
def test_large_frozen_input_keeps_homepage_small_and_full_audit_exports_intact(
        execution_book, monkeypatch, on_target_day):
    book = execution_book
    large = 'ORIGINAL_INPUT_PAYLOAD_' * 50_000
    request, core = strategy_request(book, nested=True, input_extra={'large_source': large})
    freeze(book, request)
    if on_target_day:
        advance(book, FIRST, at='08:30:00')
    before_database = book.settings.database.read_bytes()
    originals = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (book.root / 'data/plans').rglob('*.json')}
    captured = []
    captured_reports = []
    original_audit = reporting.audit_account
    original_snapshot = book.report_snapshot

    def capture_report(account, **kwargs):
        report = original_snapshot(account, **kwargs)
        captured_reports.append((report, deepcopy(report)))
        return report

    def capture_audit(root, report, **kwargs):
        audit = original_audit(root, report, **kwargs)
        captured.append((audit, deepcopy(audit)))
        return audit

    monkeypatch.setattr(reporting, 'audit_account', capture_audit)
    monkeypatch.setattr(book, 'report_snapshot', capture_report)
    result = reporting.write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    assert result['state'] == 'LEDGER_REPORT'
    text = book.settings.report.read_text()
    page = Page(text)
    embedded = next(row for row in json.loads(page.scripts['report-data'])
                    if row['account']['account_id'] == 'LLM')
    summary = embedded['audit']['plans'][0]
    assert len(text.encode()) < 250_000
    assert 'ORIGINAL_INPUT_PAYLOAD_' not in text
    assert 'fixture ranking loser' in text  # The visible calculation still works.
    assert summary['identity']['input_hash'] == request['decision_ref']['input_hash']
    assert summary['audit_state'] == 'COMPLETE' and summary['evidence_files']
    assert {'plan', 'input_bundle', 'calculation', 'referenced_documents'}.isdisjoint(summary)
    raw_account = next(report['account'] for report, _ in captured_reports
                       if report['account']['account_id'] == 'LLM')
    if on_target_day:
        assert raw_account['active_plan']['storage_format'] == 'frozen-plan-header-v1'
        assert book.get_plan('LLM', summary['plan_id'])['data_evidence']
        assert embedded['account']['active_plan']['plan_id'] == summary['plan_id']
        assert embedded['account']['active_plan']['decision_ref']['input_hash'] == request['decision_ref']['input_hash']
        assert 'data_evidence' not in embedded['account']['active_plan']
        assert 'account_snapshot' not in embedded['account']['active_plan']
    else:
        assert raw_account['active_plan'] is embedded['account']['active_plan'] is None

    output = book.root / 'reports/strategies/LLM-audit.json'
    exported = json.loads(output.read_text())
    assert output.stat().st_size < 250_000
    assert exported['plans'][0]['input_bundle']['content_hash'] == request['decision_ref']['input_hash']
    assert exported['plans'][0]['input_bundle']['projection'] == 'REFERENCE_ONLY'
    assert 'ORIGINAL_INPUT_PAYLOAD_' not in output.read_text()
    full = original_audit(book.root, captured_reports[0][0], materialize=True)
    assert full['projection_scope'] == 'FULL_AUDIT'
    assert exported['plans'][0]['calculation'] == core['calculation']
    assert exported['plans'][0]['referenced_documents']
    assert reporting.homepage_orders(exported['orders']) == embedded['orders'] == embedded['audit']['orders']
    assert [reporting.homepage_execution(row) for row in exported['fills']] == embedded['fills'] == embedded['audit']['fills']
    view = AuditView(output.with_suffix('.html').read_text())
    assert json.loads(''.join(view.text)) == exported
    assert '../strategies/LLM-audit.json' in page.account_links['LLM']
    assert '../strategies/LLM-audit.html' in page.account_links['LLM']
    assert all(audit == original for audit, original in captured)
    assert all(report == original for report, original in captured_reports)
    assert book.settings.database.read_bytes() == before_database
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
               for path, digest in originals.items())


def test_active_plan_summary_excludes_full_frozen_evidence_without_mutating_account():
    plan = {'plan_id': 'frozen-plan', 'plan_hash': 'a' * 64, 'account_id': 'S1',
        'target_session': FIRST, 'status': 'FROZEN', 'frozen_at': '2026-10-29T20:00:00+08:00',
        'strategy_id': 'S1', 'strategy_version': 'historical', 'provenance_mode': 'STRATEGY',
        'task_id': 'original-task', 'order_ids': ['order-1'], 'buy_budget_fen': 123450,
        'decision_ref': {'decision_id': 'decision-1', 'account_policy_id': 'policy-1',
            'account_policy_hash': 'b' * 64, 'strategy_implementation_hash': 'c' * 64,
            'input_hash': 'd' * 64}}
    account = {'account_id': 'S1', 'cash_fen': 5000000, 'reserved_cash_fen': 123450,
        'positions': [{'code': 'SH600000', 'total_shares': 100, 'cost_fen': 100010}],
        'blocking_reasons': [{'kind': 'PLAN_ALREADY_FROZEN', 'plan_id': 'frozen-plan'}],
        'active_plan': plan}
    expected = deepcopy(account)
    large = 'FROZEN_MARKET_QUERY_BODY_' * 100_000
    for key in ('data_evidence', 'calendar_evidence', 'validation', 'account_snapshot',
                'security_facts', 'event_facts', 'original_resources', 'future_evidence'):
        plan[key] = {'body': large}
    plan['decision_ref']['decision_core'] = {'body': large}
    original = deepcopy(account)
    projected = reporting.homepage_account(account)
    assert projected == expected
    assert len(json.dumps(projected)) < 2000
    assert reporting.homepage_account(projected) == projected
    projected['active_plan']['order_ids'].append('not-original')
    projected['positions'][0]['cost_fen'] = 0
    projected['blocking_reasons'][0]['kind'] = 'changed'
    assert account == original


def test_statement_projection_preserves_restatement_status_and_detaches_evidence():
    financial = {'session': FIRST, 'statement_revision': 2, 'supersedes_revision': 1,
        'accounting_status': 'PARTIAL', 'valuation_status': 'PROVISIONAL',
        'closing_cash_fen': 4000000, 'net_assets_fen': None, 'daily_pnl_fen': None,
        'cumulative_pnl_fen': None, 'net_value': None, 'provisional_assets_fen': 5010000,
        'requires_restatement': True, 'published_accounting_status': 'COMPLETE',
        'published_valuation_status': 'FINAL', 'published_net_assets_fen': 5010000,
        'issues': [{'kind': 'RESTATEMENT_PENDING', 'from': FIRST}],
        'positions': [{'code': 'SH600000', 'market_value_fen': 1010000}],
        'cash_changes': {'BUY': -1000000}, 'orders': [], 'fills': []}
    row = {**deepcopy(financial), 'data_evidence': [{'body': 'DAILY_QUERY_BODY_' * 100_000}],
           'future_evidence': {'body': 'FUTURE_EVIDENCE_' * 100_000}}
    original = deepcopy(row)
    projected = reporting.homepage_statements([row])
    assert projected == [financial]
    assert len(json.dumps(projected)) < 2000
    assert reporting.homepage_statements(projected) == projected
    projected[0]['issues'][0]['kind'] = 'changed'
    projected[0]['positions'][0]['market_value_fen'] = 0
    assert row == original


def test_real_daily_statement_keeps_all_financial_fields_and_csv_without_query_bodies(
        execution_book, monkeypatch):
    book = execution_book
    request, _ = strategy_request(book)
    plan = freeze(book, request)
    advance(book, FIRST)
    assert settle(book, plan)['completion_status'] == 'COMPLETE'
    close(book, FIRST)
    before_database = book.settings.database.read_bytes()
    original_snapshot = book.report_snapshot
    original_audit = reporting.audit_account
    captured = []
    captured_audits = []
    enlarge = [False]
    large = 'LARGE_DAILY_QUERY_' * 100_000

    def capture_report(account, **kwargs):
        report = original_snapshot(account, **kwargs)
        if account == 'LLM' and enlarge[0]:
            assert report['statements'][0]['data_evidence']
            report['statements'][0]['data_evidence'].append({'body': large})
            # These are the real persisted execution shapes, enlarged only in the
            # detached rendering input. The production ledger is never rewritten.
            for fill in report['fills'] + report['statements'][0]['fills']:
                fill.setdefault('account_view_evidence', {})['display_test_body'] = large
                fill.setdefault('data_evidence', []).append({'body': large})
            for order in report['orders'] + report['statements'][0]['orders']:
                order['result'].setdefault('account_view_evidence', {})['display_test_body'] = large
                order['result'].setdefault('data_evidence', []).append({'body': large})
        captured.append((report, deepcopy(report)))
        return report

    def capture_audit(root, report, **kwargs):
        audit = original_audit(root, report, **kwargs)
        captured_audits.append((audit, deepcopy(audit)))
        return audit

    monkeypatch.setattr(book, 'report_snapshot', capture_report)
    monkeypatch.setattr(reporting, 'audit_account', capture_audit)
    reporting.write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    before_text = book.settings.report.read_text()
    enlarge[0] = True
    reporting.write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    text = book.settings.report.read_text()
    page = Page(text)
    embedded = next(row for row in json.loads(page.scripts['report-data'])
                    if row['account']['account_id'] == 'LLM')
    report = next(row for row, _ in reversed(captured) if row['account']['account_id'] == 'LLM')
    statement = report['statements'][0]
    assert embedded['statements'][0] == {
        **{key: value for key, value in statement.items() if key not in {'data_evidence', 'orders', 'fills'}},
        'orders': reporting.homepage_orders(statement['orders']),
        'fills': [reporting.homepage_execution(row) for row in statement['fills']]}
    assert text == before_text  # Adding full replay evidence does not grow the homepage at all.
    assert 'LARGE_DAILY_QUERY_' not in text
    assert len(text.encode()) < 250_000
    with (book.settings.report.parent / f'LLM-r{report["report_revision"]}.csv').open() as stream:
        exported = list(csv.DictReader(stream))
    for field in ('session', 'statement_revision', 'accounting_status', 'valuation_status',
                  'closing_cash_fen', 'net_assets_fen', 'daily_pnl_fen', 'cumulative_pnl_fen'):
        assert exported[0][field] == str(report['statements'][0][field])
    audit_path = book.root / 'reports/strategies/LLM-audit.json'
    full_audit = json.loads(audit_path.read_text())
    expected_audit = next(row for row, _ in reversed(captured_audits) if row['account_id'] == 'LLM')
    assert full_audit == expected_audit
    assert 'account_view_evidence' not in full_audit['fills'][0]
    assert 'account_view_evidence' not in full_audit['orders'][0]['result']
    assert 'LARGE_DAILY_QUERY_' not in audit_path.read_text()
    view = AuditView(audit_path.with_suffix('.html').read_text())
    assert json.loads(''.join(view.text)) == full_audit
    for kind in ('orders', 'fills'):
        assert embedded[kind] == embedded['audit'][kind]
        for shown, source in zip(embedded[kind], full_audit[kind], strict=True):
            for field in ('code', 'side', 'quantity', 'reason', 'target_session', 'strategy_identity', 'audit_state'):
                assert shown[field] == source[field]
            if kind == 'fills':
                for field in ('price', 'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen',
                              'fees_fen', 'net_cash_fen', 'cost_fen', 'realized_pnl_fen', 'lot_allocations'):
                    assert shown[field] == source[field]
            else:
                assert shown['result']['state'] == source['result']['state']
                assert shown['result']['reason_code'] == source['result']['reason_code']
    assert reporting.homepage_report(embedded) == embedded
    assert all(report == original for report, original in captured)
    assert all(audit == original for audit, original in captured_audits)
    assert book.settings.database.read_bytes() == before_database


@pytest.mark.parametrize(('state', 'status', 'amount'), [
    ('SIMULATED_FILL', 'FILLED', 100000), ('NOT_FILLED', 'NOT_FILLED', 0),
    ('WAITING_DATA', 'WAITING_DATA', None),
])
def test_execution_summaries_keep_amounts_states_and_rejection_reasons(state, status, amount):
    financial = {'account_id': 'S1', 'order_id': 'order-1', 'plan_id': 'plan-1',
        'target_session': FIRST, 'code': 'SH600000', 'side': 'BUY', 'state': state,
        'quantity': None if amount is None else 100 if amount else 0,
        'price': '10.00' if amount else None, 'amount_fen': amount,
        'commission_fen': amount, 'stamp_fen': amount, 'transfer_fen': amount,
        'fees_fen': amount, 'net_cash_fen': amount, 'reason': 'known rejection or fill reason',
        'reason_code': 'SOURCE_RESULT_CODE', 'reason_details': {'threshold': '10.00'}}
    execution = {**deepcopy(financial), **{key: {'body': 'REPLAY_BODY_' * 100_000}
        for key in ('account_view_evidence', 'data_evidence', 'execution_context',
                    'reason_evidence', 'future_evidence')}}
    key = 'last_attempt' if state == 'WAITING_DATA' else 'result'
    order = {'order_id': 'order-1', 'status': status, 'reason': financial['reason'], key: execution}
    original = deepcopy(order)
    result = reporting.homepage_orders([order])[0]
    assert result == {'order_id': 'order-1', 'status': status, 'reason': financial['reason'], key: financial}
    assert len(json.dumps(result)) < 1500
    result[key]['reason_details']['threshold'] = 'changed'
    assert order == original
