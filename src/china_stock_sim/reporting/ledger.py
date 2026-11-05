"""Render recorded ledger facts and CSV from the same read snapshot; never trade."""

import csv
import hashlib
import io
import json
import os
import re
from copy import deepcopy
from html import escape

from china_stock_sim.common.accounts import select_account
from china_stock_sim.ledger import LedgerService
from china_stock_sim.market.storage import atomic_text
from china_stock_sim.reporting.audit import (
    account_filename,
    audit_account,
    identity_for_session,
    write_audit_report,
)
from china_stock_sim.reporting.initial import write_initial_report
from china_stock_sim.reporting.presentation import (
    csv_cell,
    report_path,
    strategy_label,
)
from china_stock_sim.reporting.trading_data import build_trading_view
from china_stock_sim.reporting.trading_history import build_trading_history
from china_stock_sim.reporting.trading_interaction import TRADING_SCRIPT
from china_stock_sim.reporting.trading_presentation import when
from china_stock_sim.reporting.trading_ui import render_account

_LEDGER_THEME = '''
*{box-sizing:border-box}:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#17283c;background:#f3f6fa;font-synthesis:none}body{margin:0;font-size:14px;line-height:1.7}main{max-width:1180px;margin:auto;padding:32px 28px 24px}a{color:#214f89;text-underline-offset:3px}a,select,summary{-webkit-tap-highlight-color:transparent}.ledger-header{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:26px}.brand{font-size:11px;font-weight:750;letter-spacing:2px;color:#5d7491;margin-bottom:9px}h1{font-size:29px;letter-spacing:-.5px;margin:0 0 9px}.subtitle{margin:0;color:#64758b;font-size:13px}.actions{display:flex;align-items:center;gap:12px;flex-shrink:0}.button{text-decoration:none;border:1px solid #dbe3ed;background:white;border-radius:10px;padding:11px 16px;font-size:13px;font-weight:650;white-space:nowrap}.button.primary{background:#214f89;color:white;border-color:#214f89;box-shadow:0 5px 14px #214f8914}.button:hover{filter:brightness(.96)}.account-toolbar{display:flex;align-items:center;justify-content:space-between;gap:20px;background:linear-gradient(120deg,#e7f0fd,#f4f8fe 70%);border:1px solid #d5e3f6;border-radius:16px;padding:20px 24px;margin-bottom:24px}.account-toolbar h2{font-size:20px;color:#1d466f;margin:0 0 5px}.account-toolbar p{margin:0;color:#64758b;font-size:12px}.account-toolbar label{display:flex;align-items:center;gap:12px;color:#526b86;font-size:12px;font-weight:650;white-space:nowrap}select{min-width:150px;max-width:100%;font:inherit;font-size:14px;color:#193d69;background:white;border:1px solid #c5d5e9;border-radius:10px;padding:10px 36px 10px 13px;cursor:pointer}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:20px 0}.cards>div{border:1px solid #e1e7ef;background:white;border-radius:12px;padding:16px 18px;min-width:0}.cards b{display:block;font-size:23px;line-height:1.5;color:#1d466f;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.cards span{font-size:12px;color:#77869a}.cards>div:nth-last-child(-n+2){background:#edf3fc;border-color:#d6e2f2}section[data-account]>h2{font-size:18px;margin:32px 0 12px;padding-top:8px;color:#213e60}section[data-account]>h3{font-size:14px;color:#526b89;margin:20px 0 10px}section[data-account]>p{color:#64758b;font-size:12px;overflow-wrap:anywhere}.scroll{overflow:auto;background:white;border:1px solid #e1e7ef;border-radius:12px;margin:12px 0 18px;overscroll-behavior-x:contain}table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:12px 15px;border-bottom:1px solid #edf0f5;text-align:left;white-space:nowrap}th{background:#f7f9fc;color:#60758e;font-size:11px;font-weight:650}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#f4f8fe}td{font-variant-numeric:tabular-nums}.notice{background:#fff5df;border:1px solid #f0d79b;color:#876323;border-radius:10px;padding:13px 17px;font-size:12px;line-height:1.8;margin:16px 0}.muted{color:#8592a3;font-size:12px}article{background:white;border:1px solid #e1e7ef;padding:20px 23px;margin:16px 0;border-radius:14px}article h3{font-size:14px;color:#294a70;margin:0 0 10px;overflow-wrap:anywhere}article p{font-size:12px;color:#64758b;overflow-wrap:anywhere}details{margin-top:12px;border:1px solid #e8edf4;border-radius:8px;background:#fafbfd}summary{cursor:pointer;font-size:12px;font-weight:600;padding:10px 13px;color:#526b89}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.8 ui-monospace,monospace;color:#52657c;padding:0 14px 14px;margin:0}svg{max-height:180px;width:100%;background:white;border:1px solid #e1e7ef;border-radius:12px}footer{margin-top:30px;padding-top:19px;border-top:1px solid #dfe6ef;color:#8a96a7;font-size:11px;line-height:1.8}time{font-variant-numeric:tabular-nums}a:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #8ab4ef;outline-offset:3px}[hidden]{display:none!important}@media(max-width:880px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){main{padding:21px 14px}.ledger-header{align-items:flex-start;gap:12px}h1{font-size:24px}.brand{font-size:9px;letter-spacing:1px}.subtitle{font-size:12px}.actions{flex-direction:column;gap:7px}.button{padding:9px 11px;font-size:11px}.account-toolbar{align-items:stretch;flex-direction:column;padding:18px;gap:14px}.account-toolbar label{justify-content:space-between}.account-toolbar select{flex:1;max-width:70%}.cards{gap:9px}.cards>div{padding:13px 14px}.cards b{font-size:20px}.cards span{font-size:11px}section[data-account]>h2{font-size:16px}article{padding:16px}td,th{padding:10px 12px}}
.valuation-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:24px}.eyebrow{font-size:12px;font-weight:700;letter-spacing:.5px;color:#526b86}.valuation-heading p,.snapshot-note{font-size:12px;color:#65768b}.summary-cards{margin-top:10px}.summary-cards>div:nth-child(-n+3){background:#eaf1fc;border-color:#cadcf3}.summary-cards b.loss{color:#18735d}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:34px 0 16px;padding-top:10px;border-top:1px solid #dde5ee}.section-heading h2{margin:0;color:#213e60;font-size:20px}.section-heading p{margin:3px 0 0;color:#64758b;font-size:12px}.section-heading label{white-space:nowrap;font-size:12px;color:#526b86}.section-heading select{min-width:135px;margin-left:8px}.plan-state{padding:14px 18px;border:1px solid #cddfeb;border-radius:12px;background:#eaf3fa;color:#244e72}.plan-state strong{font-size:14px}.plan-state p{margin:5px 0 0;font-size:12px;color:#58718a}.scroll td:last-child{white-space:normal;min-width:100px;max-width:400px;overflow-wrap:anywhere}.scroll td.empty{padding:22px;white-space:normal;color:#73849a}.detail-body{padding:4px 16px 16px;overflow:hidden}.public-detail,.audit-detail{margin:14px 0;border:1px solid #dce5ef;border-radius:12px;background:#fff}.public-detail>summary,.audit-detail>summary{padding:15px 18px;font-size:13px}.audit-detail{background:#f8fafd}.asset-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:16px 0 24px}.asset-chart{min-width:0;border:1px solid #e1e7ef;border-radius:14px;background:white;padding:18px 12px}.asset-chart h3{margin:0 8px 8px;font-size:14px;color:#294a70}.financial-chart{display:block;max-height:none;width:100%;height:auto;border:0;border-radius:0;background:white}.chart-note{font-size:11px;color:#718198;line-height:1.8;margin:8px}.chart-legend{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:11px;color:#52657c;margin:8px}.chart-swatch{display:inline-block;width:20px;height:10px;max-height:none;vertical-align:middle;margin-right:5px;border:0;background:none}.chart-swatch[data-series=cash]{background:#ad6c18}.chart-swatch[data-series=market_value]{background:#1a8275}.chart-swatch[data-series=pnl]{background:#8064a2}.exports{margin:25px 0 10px;font-size:12px}.detail-body table{font-size:12px}@media(max-width:880px){.asset-charts{grid-template-columns:1fr}}@media(max-width:560px){.valuation-heading{align-items:flex-start;flex-direction:column;gap:0}.valuation-heading p{margin:3px 0 0}.section-heading{align-items:flex-start;flex-direction:column;gap:12px}.section-heading h2{font-size:18px}.asset-chart{padding:14px 3px}.detail-body{padding:4px 10px 12px}.summary-cards>div:nth-child(-n+2){background:#eaf1fc}}

.history-controls,.history-pages{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:14px 0}.history-controls label{font-size:12px;color:#526b86}.history-pages button{border:1px solid #c5d5e9;border-radius:8px;padding:8px 12px;background:#f2f7fe;color:#264d77;cursor:pointer}.history-pages button:disabled{opacity:.45;cursor:default}.history-pages span{font-size:12px;color:#64758b}.daily-stock-pnl{margin:22px 0;padding-top:6px;border-top:1px solid #e1e7ef}
'''


def apply_ledger_theme(html):
    """Upgrade only page chrome/styles; retained account sections and data stay byte-identical."""
    if not isinstance(html, str) or 'id="report-data"' not in html:
        raise ValueError('交易页缺少已保存的展示数据')
    html, count = re.subn(r'<style(?:\s[^>]*)?>.*?</style>',
                         '<style id="ledger-theme">' + _LEDGER_THEME + '</style>',
                         html, count=1, flags=re.S)
    main = re.search(r'<main(?:\s[^>]*)?>', html)
    selector = re.search(r'<label(?:\s[^>]*)?>\s*当前账户\s*(<select\s+id="account">.*?</select>)\s*</label>',
                         html, flags=re.S)
    if count != 1 or main is None or selector is None or main.end() > selector.start():
        raise ValueError('交易页结构无法安全更新样式')
    end = selector.end()
    marker = '</div><!-- ledger-content -->'
    if html.startswith(marker, end):
        end += len(marker)
    chrome = ('<header class="ledger-header" data-report-kind="LEDGER_REPORT"><div>'
        '<div class="brand">SIMULATE CHINA STOCKS</div><h1>交易看板</h1>'
        '<p class="subtitle">每日账本 · 资金、持仓与交易记录</p></div>'
        '<nav class="actions" aria-label="看板导航"><a class="button primary" href="../index.html">运行首页 ↗</a>'
        '<a class="button" href="#account">切换账户</a></nav></header>'
        '<div class="account-toolbar"><div><h2>账户概览</h2>'
        '<p>独立虚拟账户，各自本金以实际账本为准；不合并总资产。</p></div>'
        '<label for="account">当前账户 ' + selector.group(1) + '</label>' + marker)
    return html[:main.end()] + chrome + html[end:]


def homepage_execution(row):
    """An execution's financial result and reason, without its full replay inputs."""
    return deepcopy({key: row[key] for key in (
        'id', 'result_id', 'fill_id', 'order_id', 'plan_id', 'account_id', 'account_revision',
        'code', 'side', 'target_session', 'effective_date', 'state', 'status', 'quantity',
        'price', 'amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen', 'fees_fen',
        'net_cash_fen', 'cost_fen', 'realized_pnl_fen', 'lot_allocations', 'reason',
        'reason_code', 'reason_details', 'computed_at', 'processed_at', 'knowledge_cutoff',
        'execution_model', 'fee_policy_hash', 'rule_policy_hash', 'ruleset_hash',
        'account_view_hash', 'commit_guard_hash', 'strategy_identity', 'audit_state') if key in row})


def homepage_orders(orders):
    result = []
    for order in orders:
        row = {key: order[key] for key in (
            'id', 'account_id', 'plan_id', 'order_id', 'order_index', 'target_session',
            'code', 'side', 'quantity', 'limit_price', 'reserved_cash_fen', 'reserved_shares',
            'status', 'reason', 'result_hash', 'strategy_identity', 'audit_state') if key in order}
        for key in ('result', 'last_attempt'):
            if key in order:
                row[key] = homepage_execution(order[key]) if isinstance(order[key], dict) else order[key]
        result.append(row)
    return deepcopy(result)


def homepage_audit(audit):
    """Keep display facts without embedding immutable document bodies a second time.

    The complete public audit is published separately by write_audit_report.  Use
    an allowlist so future evidence fields cannot silently grow the landing page.
    Copy only selected values; the full audit and its nested originals stay intact.
    """
    result = {key: audit[key] for key in (
        'account_id', 'account_revision', 'report_revision', 'scope', 'scope_note',
        'verification_scope', 'projection_note', 'account', 'state',
        'no_plan_records', 'issues') if key in audit}
    result['orders'] = homepage_orders(audit.get('orders', []))
    result['fills'] = [homepage_execution(row) for row in audit.get('fills', [])]
    result['plans'] = [{key: plan[key] for key in (
        'plan_id', 'target_session', 'status', 'empty_reason', 'identity',
        'audit_state', 'audit_issue', 'evidence_files') if key in plan}
        for plan in audit['plans']]
    result['projection_scope'] = 'HOMEPAGE_SUMMARY'
    return deepcopy(result)


def homepage_account(account):
    """Retain account display facts, replacing the current full plan with its identity."""
    result = {key: value for key, value in account.items() if key != 'active_plan'}
    if 'active_plan' in account:
        plan = account['active_plan']
        result['active_plan'] = None if plan is None else {key: plan[key] for key in (
            'plan_id', 'plan_hash', 'account_id', 'target_session', 'market_as_of',
            'status', 'frozen_at', 'strategy_id', 'strategy_version', 'provenance_mode',
            'empty_reason', 'no_order_reason', 'reason', 'task_id', 'order_ids',
            'buy_budget_fen', 'account_revision_at_freeze') if key in plan}
        if plan is not None and isinstance(plan.get('decision_ref'), dict):
            result['active_plan']['decision_ref'] = {key: plan['decision_ref'][key] for key in (
                'decision_id', 'account_policy_id', 'account_policy_hash',
                'strategy_implementation_hash', 'input_hash') if key in plan['decision_ref']}
    return deepcopy(result)


def homepage_statements(statements):
    """Keep complete financial display rows without the underlying market query bodies."""
    fields = (
        'id', 'account_id', 'session', 'statement_revision', 'account_revision',
        'accounting_status', 'valuation_status', 'plan_status', 'report_status',
        'opening_cash_fen', 'closing_cash_fen', 'cash_changes', 'cash_entries',
        'positions', 'cash_receivable_fen', 'share_receivable_value_fen',
        'market_value_fen', 'tax_payable_fen', 'cumulative_tax_expense_fen',
        'known_assets_fen', 'provisional_assets_fen', 'net_assets_fen',
        'cumulative_pnl_fen', 'daily_pnl_fen', 'net_value', 'realized_pnl_fen',
        'unrealized_pnl_fen', 'cumulative_dividend_income_fen', 'correction_pnl_fen',
        'pnl_decomposition_fen', 'cumulative_transaction_fees_fen', 'orders', 'fills',
        'issues', 'knowledge_cutoff', 'snapshot_ids', 'processed_at', 'supersedes_revision',
        'requires_restatement', 'published_accounting_status', 'published_valuation_status',
        'published_net_assets_fen')
    result = [{key: row[key] for key in fields if key in row} for row in statements]
    for row in result:
        if 'orders' in row:
            row['orders'] = homepage_orders(row['orders'])
        if 'fills' in row:
            row['fills'] = [homepage_execution(fill) for fill in row['fills']]
    return deepcopy(result)


def homepage_report(report):
    """Project one report-data account; safe to reuse on an already projected report."""
    result = {key: report[key] for key in ('account_revision', 'report_revision', 'snapshot_at')}
    result.update(account=homepage_account(report['account']),
        statements=homepage_statements(report['statements']), audit=homepage_audit(report['audit']),
        orders=homepage_orders(report['orders']),
        fills=[homepage_execution(row) for row in report['fills']])
    if 'trading' in report:
        result['trading'] = deepcopy(report['trading'])
    return result


_HISTORY_META = ('schema_version', 'scope', 'account_id', 'snapshot_at', 'account_revision',
                 'report_revision', 'complete', 'state', 'reason', 'row_counts',
                 'trade_columns', 'summary_columns')
_HISTORY_CSV_FIELDS = ('id', 'fill_id', 'order_id', 'plan_id', 'target_session', 'effective_date',
    'code', 'side', 'quantity', 'price', 'amount_fen', 'commission_fen', 'stamp_fen',
    'transfer_fen', 'fees_fen', 'net_cash_fen', 'cost_fen', 'realized_pnl_fen',
    'processed_at', 'storage_sequence')


def public_history(history):
    """Only visible cells and filter keys, without duplicating the raw ledger projection."""
    result = {key: history.get(key) for key in _HISTORY_META}
    for key in ('trade_chunks', 'summary_chunks'):
        result[key] = [[{k: row.get(k) for k in ('code', 'status', 'cells')} for row in chunk]
                       for chunk in history.get(key, [])]
    return result


def write_history_csv(settings, history):
    """Complete original fills, with explicit scope/snapshot; never export a truncated list."""
    if not history.get('complete'):
        return None
    chunks = history.get('trade_chunks', [])
    if sum(len(chunk) for chunk in chunks) != history.get('row_counts', {}).get('trades'):
        raise ValueError('完整成交导出行数不一致，保留上一页面')
    fields = ['account_id', 'snapshot_at', 'account_revision', 'report_revision', 'scope',
              'total_fills', *_HISTORY_CSV_FIELDS]
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    metadata = {key: history.get(key) for key in fields[:5]}
    metadata['total_fills'] = history['row_counts']['trades']
    for chunk in chunks:
        for row in chunk:
            raw = row.get('raw', row)
            writer.writerow({key: csv_cell(value) for key, value in {**raw, **metadata}.items()
                             if key in fields})
    body = '\ufeff' + stream.getvalue()
    suffix = hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]
    name = account_filename(history['account_id']) + '-all-trades-' + suffix + '.csv'
    atomic_text(report_path(settings, settings.report.parent / name), body)
    return name


def write_ledger_report(settings, snapshot, *, ledger=None):
    if snapshot['state'] == 'INITIALIZED_NOT_RUNNING':
        write_initial_report(settings, snapshot)
        return {'state': snapshot['state'], 'published': []}
    selection = select_account(snapshot['accounts'], settings.default_account)
    ledger = ledger or LedgerService(settings)
    reports = [ledger.report_snapshot(a['account_id'], mode='recent', include_trading_history=True) for a in sorted(
        snapshot['accounts'], key=lambda row: row['account_id'])]
    sections, options, embedded_reports, history_scripts = [], [], [], []
    quote_cache = {}
    display_cutoff = max((r['snapshot_at'] for r in reports), default=ledger._now().isoformat())
    for report in reports:
        account = report['account']
        identity = account['account_id']
        audit = audit_account(settings.root, report, materialize=False)
        audit_output = settings.root / 'reports/strategies' / (account_filename(identity) + '-audit.json')
        write_audit_report(settings.root, audit_output, audit)
        audit_href = escape(os.path.relpath(audit_output, settings.report.parent), quote=True)
        audit_view_href = escape(os.path.relpath(audit_output.with_suffix('.html'), settings.report.parent), quote=True)
        trading = build_trading_view(report, market=ledger.market,
            knowledge_cutoff=display_cutoff, quote_cache=quote_cache)
        embedded_reports.append(homepage_report({**{key: report[key] for key in (
            'account', 'account_revision', 'report_revision', 'snapshot_at', 'statements')},
            'audit': audit, 'orders': audit['orders'], 'fills': audit['fills'], 'trading': trading}))
        history = build_trading_history(report)
        history_id = 'history-' + account_filename(identity)
        history_scripts.append('<script type="application/json" id="' + escape(history_id, quote=True) + '">'
            + json.dumps(public_history(history), ensure_ascii=False, allow_nan=False).replace('<', '\\u003c') + '</script>')
        history_csv_name = write_history_csv(settings, history)
        current = report['statements'][-1] if report['statements'] else {}
        options.append(f'<option value="{escape(identity)}"' + (
            ' selected' if identity == selection['account_id'] else '') + f'>{escape(identity)}</option>')
        stream = io.StringIO(newline='')
        identity_fields = ['strategy_id', 'strategy_version', 'provenance_mode', 'decision_id',
            'account_policy_id', 'account_policy_hash', 'strategy_implementation_hash', 'input_hash']
        csv_fields = ['account_id', 'initial_cash_fen', 'session', 'statement_revision',
                     'accounting_status', 'valuation_status', 'closing_cash_fen', 'net_assets_fen',
                     'daily_pnl_fen', 'cumulative_pnl_fen', *identity_fields, 'identity_note']
        writer = csv.DictWriter(stream, fieldnames=csv_fields, extrasaction='ignore')
        writer.writeheader()
        statements = []
        for row in report['statements']:
            historical = identity_for_session(audit, row['session'])
            has_plan = any(p['target_session'] == row['session'] for p in audit['plans'])
            export_row = {**row, 'account_id': identity, 'initial_cash_fen': account['initial_cash_fen'],
                **{key: historical.get(key) for key in identity_fields},
                'identity_note': '当时已冻结计划' if has_plan else '当日无已冻结计划'}
            writer.writerow({key: csv_cell(value) for key, value in export_row.items()})
            statements.append({**row, 'strategy_label': strategy_label(historical)})
        csv_name = f'{account_filename(identity)}-r{report["report_revision"]}.csv'
        atomic_text(report_path(settings, settings.report.parent / csv_name), '\ufeff' + stream.getvalue())
        indices = []
        if current:
            for code in settings.raw['reporting']['benchmark_codes']:
                result = ledger.market.index_quote(code, current['session'])
                quote = result.data if isinstance(result.data, dict) else {}
                indices.append({'code': code, 'date': current['session'], 'state': result.state,
                                'close': quote.get('close'), 'change_percent': quote.get('change_percent')})
        content = render_account(report, audit, trading, statements=statements, csv_name=csv_name,
            audit_href=audit_href, audit_view_href=audit_view_href, indices=indices,
            history=history, history_csv_name=history_csv_name)
        sections.append(f'<section data-account="{escape(identity)}" data-history-id="{escape(history_id, quote=True)}"' + (
            '' if identity == selection['account_id'] else ' hidden') + '>' + content + '</section>')
    generated = ledger._now().isoformat()
    body = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>交易看板 · A股前向模拟</title><style></style></head><body><main><label>当前账户 <select id="account">'''
    body += ''.join(options) + '</select></label>'
    if selection['fallback']:
        body += '<p class="notice">默认账户不可用，已按账户 ID 稳定排序选择 ' + escape(selection['account_id']) + '。</p>'
    body += ''.join(sections)
    body += f'<footer>最后生成（北京时间）：<time id="updated" datetime="{escape(generated)}">{escape(when(generated))}</time><br><span id="age"></span> 页面只展示已保存账本，刷新不会发起采集或交易。</footer>'
    body += '<script type="application/json" id="report-data">' + json.dumps(embedded_reports, ensure_ascii=False).replace('<', '\\u003c') + '</script>'
    body += ''.join(history_scripts)
    body += '<script>' + TRADING_SCRIPT + '</script></main></body></html>'
    atomic_text(report_path(settings, settings.report), apply_ledger_theme(body))
    return {'state': 'LEDGER_REPORT', 'published': [{'account_id': r['account']['account_id'],
        'report_revision': r['report_revision'], 'account_revision': r['account_revision']} for r in reports]}
