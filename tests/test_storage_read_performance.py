"""SG: projected reads and reusable, tamper-sensitive immutable verification."""

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from time import perf_counter

import pytest
from test_market_service import bar, populate, source

from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger import decision_evidence as evidence
from china_stock_sim.ledger.document_objects import archive_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, Transaction, canonical
from china_stock_sim.ledger.queries import latest_statements
from china_stock_sim.ledger.schema import check_schema, create_schema, tables_for_version
from china_stock_sim.ledger.store import initialize
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

NOW = '2026-11-03T12:00:00+08:00'


def _book(settings):
    initialize(settings, recorded_at=NOW)
    return LedgerService(settings, clock=lambda: datetime.fromisoformat(NOW), test_mode=True)


def _tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_schema_shape_check_does_not_run_full_integrity_scan():
    db = sqlite3.connect(':memory:')
    create_schema(db)
    statements = []
    db.set_trace_callback(statements.append)
    assert check_schema(db, full=False) == 4
    assert not any('quick_check' in s or 'foreign_key_check' in s for s in statements)
    check_schema(db)
    assert any('quick_check' in s for s in statements)
    assert any('foreign_key_check' in s for s in statements)
    db.execute('PRAGMA user_version=3')
    assert check_schema(db, legacy=True, full=False) == 3
    assert tables_for_version(3) == tables_for_version(4)
    with pytest.raises(ValueError, match='显式迁移'):
        check_schema(db, full=False)


@pytest.mark.parametrize('years', [1, 3, 5])
def test_year_scale_metadata_reads_do_not_decode_audit_rows(settings, monkeypatch, record_property, years):
    book = _book(settings)
    start = date(2020, 1, 1)
    rows = []
    for i in range(252 * years):
        day = (start + timedelta(days=i)).isoformat()
        rows.append((f'nav:{day}', 'S1', canonical({'session': day, 'statement_revision': 1,
            'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL',
            'net_assets_fen': 5_000_000, 'account_revision': i,
            'orders': [{'evidence': 'x' * 512}], 'data_evidence': [{'large': 'y' * 512}]})))
    with sqlite3.connect(settings.database) as db:
        db.executemany('INSERT INTO daily_nav VALUES (?,?,?)', rows)
        db.execute('INSERT INTO orders VALUES (?,?,?)', ('pending', 'S1', canonical({
            'plan_id': 'p', 'code': 'SH600000', 'target_session': '2026-11-03', 'status': 'PENDING',
            'last_attempt': {'evidence': 'z' * 1_000_000}})))
    monkeypatch.setattr(Transaction, '_decode', lambda *a: pytest.fail('metadata read decoded full row'))
    started = perf_counter()
    status = book.workflow_status('S1', through='2026-11-03')
    revision = book.revision_state('S1')
    summary = book._read('S1', lambda tx: latest_statements(tx, summary=True))
    elapsed = perf_counter() - started
    record_property('years', years)
    record_property('statement_count', len(rows))
    record_property('elapsed_seconds', elapsed)
    record_property('projected_bytes', len(json.dumps(status).encode()))
    assert len(status['statements']) == len(summary) == 252 * years
    assert status['orders'] == [{'id': 'pending', 'plan_id': 'p', 'code': 'SH600000',
                                'target_session': '2026-11-03', 'status': 'PENDING'}]
    assert revision['account_revision'] == 0
    assert 'evidence' not in json.dumps(status)
    assert len(json.dumps(status)) < 300_000
    assert elapsed < 15


def test_document_verification_is_readonly_then_persisted_and_reused(tmp_path):
    ref = document(tmp_path, 'test', {'important': 'original'})
    before = _tree(tmp_path)
    with evidence.verification_scope(tmp_path) as first:
        evidence.read_business_document(tmp_path, ref)
    assert first['full_checks'] == 1 and _tree(tmp_path) == before
    with evidence.verification_scope(tmp_path, persist=True) as second:
        result, _ = evidence.read_business_document(tmp_path, ref)
    assert second['cache_hits'] == 1 and second['receipts_written'] == 1
    result['important'] = 'caller mutation'
    evidence._DOCUMENTS.clear()
    evidence._DOCUMENT_BYTES = 0
    with evidence.verification_scope(tmp_path) as third:
        actual, _ = evidence.read_business_document(tmp_path, ref)
    assert actual['important'] == 'original'
    assert third['full_checks'] == 0 and third['cache_hits'] == 1


def test_document_cache_detects_same_size_tamper_even_with_restored_mtime(tmp_path):
    ref = document(tmp_path, 'test', {'value': 'AAAA'})
    with evidence.verification_scope(tmp_path, persist=True):
        evidence.read_business_document(tmp_path, ref)
    path = tmp_path / ref['path']
    old = path.stat()
    path.write_bytes(path.read_bytes().replace(b'AAAA', b'BBBB'))
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(LedgerError, match='损坏'):
        evidence.read_business_document(tmp_path, ref)


def test_validation_cache_invalidates_changed_dependency_and_rule(tmp_path, monkeypatch):
    child = document(tmp_path, 'test', {'value': 1})
    parent = document(tmp_path, 'test', {'child': child})
    closure = evidence.document_closure(tmp_path, {parent['path']: {parent['content_hash']}})
    with evidence.verification_scope(tmp_path, persist=True):
        evidence.remember_validation(tmp_path, 'fixture', 'abc', closure['files'])
    assert evidence.cached_validation(tmp_path, 'fixture', 'abc')
    original_rule = evidence.verification_identity()
    monkeypatch.setattr(evidence, 'verification_identity', lambda: 'changed-rule')
    assert not evidence.cached_validation(tmp_path, 'fixture', 'abc')
    monkeypatch.setattr(evidence, 'verification_identity', lambda: original_rule)
    (tmp_path / child['path']).unlink()
    assert not evidence.cached_validation(tmp_path, 'fixture', 'abc')
    with pytest.raises(LedgerError):
        evidence.document_closure(tmp_path, {parent['path']: {parent['content_hash']}})


def test_nested_readonly_validation_scope_cannot_write_receipts(tmp_path):
    ref = document(tmp_path, 'test', {'value': 1})
    before = _tree(tmp_path)
    with evidence.verification_scope(tmp_path, persist=True):
        with evidence.verification_scope(tmp_path, persist=False):
            evidence.read_business_document(tmp_path, ref)
    assert _tree(tmp_path) == before


def test_cas_document_preserves_exact_original_and_closure_and_rejects_tamper(tmp_path):
    ref = document(tmp_path, 'test', {'important': ['word' * 1000], 'amount_fen': 12345})
    original = (tmp_path / ref['path']).read_bytes()
    old_body, _ = evidence.read_business_document(tmp_path, ref)
    result = archive_document(tmp_path, ref)
    assert result['state'] == 'APPLIED'
    body, info = evidence.read_business_document(tmp_path, ref)
    assert body == old_body and info['original_sha256'] == hashlib.sha256(original).hexdigest()
    closure = evidence.document_closure(tmp_path, {ref['path']: {ref['content_hash']}})
    stored_path = result['object_ref']['_evidence_ref']['path']
    assert {f['path'] for f in closure['files']} == {ref['path'], stored_path}
    assert archive_document(tmp_path, ref)['state'] == 'UNCHANGED'
    path = tmp_path / stored_path
    stat = path.stat()
    path.chmod(0o600)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 1
    path.write_bytes(data)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(LedgerError):
        evidence.read_business_document(tmp_path, ref)


def test_failed_wrapper_publication_keeps_original_bytes(tmp_path, monkeypatch):
    ref = document(tmp_path, 'test', {'value': 'original'})
    original = (tmp_path / ref['path']).read_bytes()
    monkeypatch.setattr('china_stock_sim.ledger.document_objects.os.replace',
                        lambda *a: (_ for _ in ()).throw(OSError('interruption')))
    with pytest.raises(OSError):
        archive_document(tmp_path, ref)
    assert (tmp_path / ref['path']).read_bytes() == original


def test_market_dependency_fingerprint_is_scoped_and_cutoff_sensitive(tmp_path):
    repository = MarketRepository(tmp_path)
    populate(repository)
    market = MarketDataService(tmp_path)
    query = {'code': 'SH600000', 'start': '2024-02-08', 'end': '2024-02-08',
             'knowledge_cutoff': '2024-02-08T19:00:00+08:00'}
    original = market.statement_input_fingerprint('prices', query)
    assert original['state'] == 'READY'
    repository.save_state('jobs', 'job', {'state': 'COMPLETE'})
    other = {**bar(), 'code': 'SH600001'}
    repository.put('bar', [other], source([other]))
    assert market.statement_input_fingerprint('prices', query) == original
    revised = bar(close='12.00')
    repository.put('bar', [revised], source([revised], fetched='2024-02-08T20:00:00+08:00'))
    assert market.statement_input_fingerprint('prices', query) == original
    later = market.statement_input_fingerprint('prices', {
        **query, 'knowledge_cutoff': '2024-02-08T21:00:00+08:00'})
    assert later['fingerprint'] != original['fingerprint']
