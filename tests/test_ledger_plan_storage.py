"""SG13: bounded authoritative indexes, explicit originals and isolated strategy compaction."""

import json
import sqlite3
from datetime import date, timedelta

import pytest
from execution_fixtures import execution_book
from test_execution_decision_audit import register_proposed, strategy_request
from test_ledger_trading import DAY, book, call, plan_payload

from china_stock_sim.common import evidence_objects
from china_stock_sim.ledger import plan_storage
from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest
from china_stock_sim.ledger.strategy_decisions import _append_history, compact_decision_index

assert book and execution_book


def stored(book, table, identity):
    with sqlite3.connect(book.settings.database) as db:
        return json.loads(db.execute(f'SELECT data FROM {table} WHERE id=?', (identity,)).fetchone()[0])


def save_row(book, table, identity, row):
    with sqlite3.connect(book.settings.database) as db:
        db.execute(f'UPDATE {table} SET data=? WHERE id=?', (canonical(row), identity))


def financial(book):
    with sqlite3.connect(book.settings.database) as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in ('accounts', 'account_state', 'orders', 'fills', 'cash_entries',
                              'daily_nav', 'position_lots', 'operations')}


def test_new_frozen_plan_is_small_and_public_read_returns_unchanged_original(book):
    payload = plan_payload(book)
    payload['calculation'] = {'proof': 'x' * (17 * 1024 * 1024)}
    result = call(book, 'freeze_plan', payload)
    assert result['state'] == 'APPLIED', result
    row = stored(book, 'plans', payload['plan_id'])
    assert row['storage_format'] == plan_storage.FORMAT
    assert len(canonical(row)) < 30_000 and 'calculation' not in row
    full = book.get_plan('LLM', payload['plan_id'])
    original = read_business_document(book.root, row['document'])[0]
    assert full['calculation'] == payload['calculation']
    assert {k: v for k, v in full.items() if k not in {'id', 'document'}} == original
    assert book.execution_view('LLM', payload['plan_id'])['plan_id'] == payload['plan_id']


@pytest.mark.parametrize('damage', ['header', 'document_missing', 'unknown_format'])
def test_header_and_original_damage_is_not_silently_read_as_legacy(book, damage):
    payload = plan_payload(book)
    assert call(book, 'freeze_plan', payload)['state'] == 'APPLIED'
    row = stored(book, 'plans', payload['plan_id'])
    if damage == 'header':
        assert row['orders'][0]['quantity'] == 1000
        row['orders'][0]['quantity'] = 100
        save_row(book, 'plans', payload['plan_id'], row)
    elif damage == 'document_missing':
        (book.root / row['document']['path']).unlink()
    else:
        row['storage_format'] = 'unknown'
        save_row(book, 'plans', payload['plan_id'], row)
    with pytest.raises((LedgerError, OSError, ValueError)):
        book.get_plan('LLM', payload['plan_id'])


def test_recent_report_reads_only_selected_headers_and_includes_old_unresolved(book, monkeypatch):
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(130)
            if (date(2026, 1, 1) + timedelta(days=i)).weekday() < 5]
    with sqlite3.connect(book.settings.database) as db:
        for n, day in enumerate(days):
            value = {'plan_id': f'old-{n}', 'account_id': 'LLM', 'target_session': day,
                     'status': 'EMPTY', 'orders': [], 'order_ids': [], 'plan_hash': digest(n),
                     'calculation': {'bounded_fixture': n}}
            if n == 1:
                value['task_id'] = 'old-active-task'
            elif n == 2:
                value['task_id'] = 'old-expired-task'
            value['document'] = document(book.root, 'frozen', value)
            db.execute('INSERT INTO plans VALUES(?,?,?)', (value['plan_id'], 'LLM',
                       canonical({**plan_storage.plan_header(value), 'id': value['plan_id']})))
        order = {'id': 'pending-old', 'order_id': 'pending-old', 'plan_id': 'old-0', 'target_session': days[0],
                 'status': 'WAITING_DATA'}
        db.execute('INSERT INTO orders VALUES(?,?,?)', ('pending-old', 'LLM', canonical(order)))
        task = {'task_id': 'old-active-task', 'status': 'ACTIVE', 'end_session': '2026-12-01',
                'targets': [{'code': 'SH600000', 'side': 'BUY', 'quantity': 100}]}
        db.execute('INSERT INTO rebalance_tasks VALUES(?,?,?)', ('old-active-task', 'LLM', canonical(task)))
        db.execute('INSERT INTO rebalance_tasks VALUES(?,?,?)', ('old-expired-task', 'LLM',
                   canonical({**task, 'task_id': 'old-expired-task', 'end_session': '2026-01-30'})))
        for key in ('session', 'effective_date'):
            db.execute('INSERT INTO job_runs VALUES(?,?,?)', (key, 'LLM', canonical({key: days[-1]})))
    def unexpected(*a, **k):
        pytest.fail('recent snapshot must not expand immutable plan bodies')
    with monkeypatch.context() as local:
        local.setattr(plan_storage, 'read_business_document', unexpected)
        report = book.report_snapshot('LLM', DAY, mode='recent')
    assert len(report['plans']) == 62
    assert report['report_scope']['total_plans'] == len(days)
    assert report['report_scope']['has_more'] is True
    assert 'old-0' in {p['plan_id'] for p in report['plans']}
    assert all('calculation' not in p for p in report['plans'])
    assert 'old-2' not in {p['plan_id'] for p in report['plans']}
    assert {r['id'] for r in report['steps']} == {'session', 'effective_date'}
    assert book.get_plan('LLM', 'old-1')['calculation'] == {'bounded_fixture': 1}
    full = book.report_snapshot('LLM', DAY)
    assert len(full['plans']) == len(days) and full['report_scope']['has_more'] is False
    assert all('calculation' in p for p in full['plans'])


def test_large_proposal_history_references_existing_core_instead_of_duplicating_body(book):
    proposal = {'data_evidence': [{'text': 'x' * (17 * 1024 * 1024)}], 'decision_ref': {'identity': 'bound'}}
    core = document(book.root, 'strategy', {'proposal': {k: v for k, v in proposal.items()
                                                      if k != 'decision_ref'}})
    row = {'proposal': proposal, 'decision_core': core, 'decision_ref': proposal['decision_ref'],
           'state': 'PROPOSED'}
    compact = _append_history(book.root, row, book._now().isoformat(), 'SAVE')
    assert len(canonical(compact)) < 10_000
    body = read_business_document(book.root, compact['document'])[0]
    assert len(canonical(body)) < 10_000
    assert compact['proposal']['source'] == core
    from china_stock_sim.ledger.strategy_decisions import resolve_decision_index
    assert resolve_decision_index(book.root, compact)['proposal'] == proposal


def test_public_proposed_roundtrip_and_explicit_submigration_preserve_financial_rows(execution_book):
    book = execution_book
    request, _ = strategy_request(book)
    register_proposed(book, request)
    logical = book.get_strategy_decision('LLM', target_session=request['target_session'])
    compact = stored(book, 'strategy_decisions', logical['decision_id'])
    assert compact['proposal']['storage_format'] == 'decision-core-proposal-v1'
    assert logical['proposal'] == request
    # Reproduce a legacy full proposal index, whose immutable history remains the same.
    save_row(book, 'strategy_decisions', logical['decision_id'], logical)
    before = financial(book)
    raw = book.settings.database.read_bytes()
    plan = plan_storage.compact_strategy_indexes(book.root)
    assert plan['state'] == 'PLANNED' and book.settings.database.read_bytes() == raw
    with pytest.raises(LedgerError):
        plan_storage.compact_strategy_indexes(book.root, apply=True, expected_plan='stale')
    assert book.settings.database.read_bytes() == raw
    result = plan_storage.compact_strategy_indexes(book.root, apply=True, expected_plan=plan['plan_id'])
    assert result['state'] == 'APPLIED' and financial(book) == before
    current = book.get_strategy_decision('LLM', target_session=request['target_session'])
    assert {k: v for k, v in current.items() if k != 'original_record_ref'} == logical
    saved = stored(book, 'strategy_decisions', logical['decision_id'])
    assert evidence_objects.read_json(book.root, saved['original_record_ref']) == logical
    assert compact_decision_index(current)['proposal'] == compact['proposal']
    previous = book.settings.database.read_bytes()
    assert plan_storage.compact_strategy_indexes(book.root, apply=True, expected_plan=plan['plan_id'])['state'] == 'UNCHANGED'
    assert book.settings.database.read_bytes() == previous


def test_isolated_legacy_plan_submigration_preserves_full_contract_and_original_raw(book):
    payload = plan_payload(book)
    assert call(book, 'freeze_plan', payload)['state'] == 'APPLIED'
    full = book.get_plan('LLM', payload['plan_id'])
    save_row(book, 'plans', full['plan_id'], full)
    before = financial(book)
    plan = plan_storage.compact_strategy_indexes(book.root)
    assert len(plan['changes']) == 1
    result = plan_storage.compact_strategy_indexes(book.root, apply=True, expected_plan=plan['plan_id'])
    assert result['state'] == 'APPLIED'
    assert financial(book) == before
    row = stored(book, 'plans', full['plan_id'])
    assert evidence_objects.read_json(book.root, row['original_record_ref']) == full
    assert book.get_plan('LLM', full['plan_id']) == full
    with sqlite3.connect(book.settings.database) as db:
        indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'storage_orders_status_session', 'storage_fills_session', 'storage_cash_session'} <= indexes


def test_compaction_refuses_production_root_without_opening_database(monkeypatch, tmp_path):
    monkeypatch.setattr(plan_storage, '_PRODUCTION', tmp_path)
    with pytest.raises(LedgerError):
        plan_storage.compact_strategy_indexes(tmp_path, apply=True, expected_plan='x')


def test_compact_audit_calculation_keeps_normal_fields_and_references_large_attachment():
    from china_stock_sim.reporting.audit import _compact_calculation
    reference = {'path': 'data/plans/strategy/' + 'a' * 64 + '.json', 'content_hash': 'a' * 64}
    calculation = {'ranking': [{'code': 'SH600000', 'score': '0.5'}],
                   'unknown_attachment': 'full original' * 100_000}
    result = _compact_calculation(calculation, reference)
    assert result['ranking'] == calculation['ranking']
    assert result['unknown_attachment']['content_hash'] == digest(calculation['unknown_attachment'])
    assert result['unknown_attachment']['contained_in'] == reference
    assert result['unknown_attachment']['json_pointer'] == '/calculation/unknown_attachment'
    assert len(canonical(result)) < 2_000
