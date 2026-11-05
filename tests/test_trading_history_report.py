"""Full report/history export consistency without business writes or external collection."""

import csv
import json
import re

import pytest
from test_ledger_trading import Facts, book, buy_and_close

from china_stock_sim.ledger.store import read_snapshot
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.reporting.ledger import public_history, write_history_csv, write_ledger_report

assert book


def test_report_history_and_full_csv_share_revision_and_counts_without_ledger_writes(book, monkeypatch):
    buy_and_close(book)
    monkeypatch.setattr(Facts, 'index_quote', lambda *a, **k: DataResult('MISSING', []), raising=False)
    before = book.settings.database.read_bytes()
    write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    page = book.settings.report.read_text()
    all_reports = json.loads(re.search(r'id="report-data">(.*?)</script>', page, re.S)[1])
    llm = next(report for report in all_reports if report['account']['account_id'] == 'LLM')
    history = json.loads(re.search(r'<script type="application/json" id="history-LLM">(.*?)</script>', page, re.S)[1])
    rows = list(csv.DictReader(next(book.settings.report.parent.glob('LLM-all-trades-*.csv')).open(encoding='utf-8-sig')))
    assert history['complete'] and history['scope'] == 'all_time'
    assert history['row_counts']['trades'] == len(rows) == 1
    assert history['snapshot_at'] == llm['snapshot_at'] == rows[0]['snapshot_at']
    assert history['account_revision'] == llm['account_revision'] == int(rows[0]['account_revision'])
    assert rows[0]['quantity'] == '1000' and rows[0]['amount_fen'] == '1000000'
    assert rows[0]['processed_at'] and int(rows[0]['storage_sequence']) > 0
    assert rows[0]['net_cash_fen'] == '-1000510' and rows[0]['side'] == 'BUY'
    assert 'trading_history_projection' not in page
    assert all('raw' not in row for chunk in history['trade_chunks'] for row in chunk)
    assert 'data-history-id="history-LLM"' in page
    assert book.settings.database.read_bytes() == before


def test_public_history_does_not_duplicate_financial_raw_or_private_evidence():
    row = {'code': 'SH600000', 'status': 'HELD', 'cells': ['<script>plain text</script>'],
           'raw': {'input_bundle': 'SECRET', 'price': '10.00'}, 'original_record_ref': 'SECRET'}
    result = public_history({'trade_chunks': [[row]], 'summary_chunks': [[row]], 'raw': 'SECRET'})
    assert 'SECRET' not in json.dumps(result)
    assert result['trade_chunks'][0][0] == {k: row[k] for k in ('code', 'status', 'cells')}


def test_full_csv_refuses_count_mismatch_and_does_not_overwrite_previous_export(settings):
    dest = settings.report.parent / 'LLM-all-trades.csv'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text('previous complete export')
    history = {'complete': True, 'account_id': 'LLM', 'row_counts': {'trades': 2},
               'trade_chunks': [[{'code': 'SH600000'}]]}
    with pytest.raises(ValueError, match='行数'):
        write_history_csv(settings, history)
    assert dest.read_text() == 'previous complete export'
    assert write_history_csv(settings, {'complete': False}) is None


def test_changed_history_export_cannot_replace_csv_referenced_by_previous_page(settings):
    history = {'complete': True, 'scope': 'all_time', 'account_id': 'LLM', 'snapshot_at': '2026-11-04T10:00:00+08:00',
               'account_revision': 1, 'report_revision': 1, 'row_counts': {'trades': 1},
               'trade_chunks': [[{'id': 'one', 'code': 'SH600000', 'side': 'BUY', 'quantity': 100}]]}
    first = write_history_csv(settings, history)
    original = (settings.report.parent / first).read_bytes()
    second = write_history_csv(settings, {**history, 'snapshot_at': '2026-11-04T11:00:00+08:00'})
    assert first != second
    assert (settings.report.parent / first).read_bytes() == original
    assert write_history_csv(settings, history) == first
