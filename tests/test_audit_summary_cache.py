"""Ordinary audits reuse verified immutable summaries, never current trade rows."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from time import perf_counter

import pytest
from execution_fixtures import FIRST, advance, execution_book, proposal
from test_execution_decision_audit import application, freeze, strategy_request

from china_stock_sim.ledger import decision_evidence
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import digest
from china_stock_sim.ledger.plan_storage import plan_header
from china_stock_sim.reporting import audit

assert execution_book


def clear_memory():
    decision_evidence._VALIDATIONS.clear()
    decision_evidence._DOCUMENTS.clear()
    decision_evidence._DOCUMENT_BYTES = 0


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def manual_report(book, size=80_000):
    frozen = application(book).freeze(proposal(book), operation_id='summary-plan')['plan']
    body = {k: v for k, v in frozen.items() if k not in {'id', 'document'}}
    body['legacy_attachment'] = 'x' * size
    reference = document(book.root, 'frozen', body)
    header = plan_header({**body, 'document': reference})
    report = book.report_snapshot('LLM', FIRST, mode='recent')
    report['plans'] = [header]
    return report, {**body, 'document': reference}


def warm_summary(book, report):
    with decision_evidence.verification_scope(book.root, persist=True):
        result = audit.audit_account(book.root, report, materialize=False)
    assert result['state'] == 'COMPLETE'
    return result


def test_large_original_cold_once_persistent_warm_zero_expansion_and_full_is_explicit(
        execution_book, monkeypatch, record_property):
    book = execution_book
    report, original = manual_report(book, size=20 * 1024 * 1024)
    clear_memory()
    reads = []
    read = decision_evidence.read_document_bytes

    def counted(*args, **kwargs):
        reads.append(1)
        return read(*args, **kwargs)

    monkeypatch.setattr(decision_evidence, 'read_document_bytes', counted)
    started = perf_counter()
    cold = warm_summary(book, report)
    cold_seconds = perf_counter() - started
    assert reads == [1]
    assert len(json.dumps(cold)) < 100_000
    clear_memory()
    before = tree(book.root)
    with monkeypatch.context() as patch:
        patch.setattr(decision_evidence, 'read_document_bytes',
                      lambda *a, **k: pytest.fail('warm summary expanded immutable bytes'))
        patch.setattr(audit, 'resolve_plan', lambda *a: pytest.fail('warm summary expanded plan'))
        started = perf_counter()
        with decision_evidence.verification_scope(book.root, persist=False) as metrics:
            warm = audit.audit_account(book.root, report, materialize=False)
        warm_seconds = perf_counter() - started
    assert warm == cold and tree(book.root) == before
    assert metrics['bytes_read'] == 0 and metrics['receipts_written'] == 0
    with monkeypatch.context() as patch:
        patch.setattr(audit, '_cached_summary', lambda *a: pytest.fail('full audit used summary'))
        full = audit.audit_account(book.root, report, materialize=True)
    assert full['state'] == 'COMPLETE'
    assert full['plans'][0]['plan']['legacy_attachment'] == original['legacy_attachment']
    assert len(reads) > 1 and tree(book.root) == before
    record_property('cold_seconds', cold_seconds)
    record_property('warm_seconds', warm_seconds)
    record_property('logical_attachment_bytes', 20 * 1024 * 1024)


@pytest.mark.parametrize('damage', ['missing_wrapper', 'tampered_wrapper', 'tampered_cas',
                                   'self_hashed_header', 'unknown_header_field'])
def test_summary_never_masks_changed_or_missing_original_or_header(execution_book, damage):
    book = execution_book
    report, original = manual_report(book)
    warm_summary(book, report)
    header = report['plans'][0]
    path = book.root / original['document']['path']
    if damage == 'missing_wrapper':
        path.unlink()
    elif damage == 'tampered_wrapper':
        path.write_text('{}')
    elif damage == 'tampered_cas':
        wrapper = json.loads(path.read_bytes())
        obj = book.root / wrapper['object_ref']['_evidence_ref']['path']
        obj.chmod(0o600)
        raw = bytearray(obj.read_bytes())
        raw[-1] ^= 1
        obj.write_bytes(raw)
    elif damage == 'self_hashed_header':
        header['orders'][0]['quantity'] += 100
        header['header_hash'] = digest({k: v for k, v in header.items() if k != 'header_hash'})
    else:
        header['unrecognized_field'] = 'untrusted'
        header['header_hash'] = digest({k: v for k, v in header.items() if k != 'header_hash'})
    before = tree(book.root)
    result = audit.audit_account(book.root, report, materialize=False)
    assert result['state'] == 'PARTIAL' and result['plans'][0]['audit_state'] == 'AUDIT_UNAVAILABLE'
    assert tree(book.root) == before


def test_legacy_payload_cannot_bypass_full_comparison_by_matching_header(execution_book):
    book = execution_book
    report, original = manual_report(book)
    warm_summary(book, report)
    report['plans'] = [deepcopy(original)]
    report['plans'][0]['legacy_attachment'] = 'changed outside header'
    assert audit.audit_account(book.root, report, materialize=False)['state'] == 'PARTIAL'


def test_current_fills_and_orders_are_not_cached_or_mutated(execution_book, monkeypatch):
    book = execution_book
    request, core = strategy_request(book, nested=True)
    frozen = freeze(book, request)
    report = book.report_snapshot('LLM', FIRST, mode='recent')
    before_report = deepcopy(report)
    first = warm_summary(book, report)
    assert first['fills'] == [] and first['plans'][0]['fills'] == []
    first['plans'][0]['calculation']['ranking'][0]['score'] = '999'
    first['plans'][0]['orders'].append({'order_id': 'poison'})
    advance(book, FIRST)
    assert application(book).settle('LLM', frozen['plan_id'], operation_id='summary-settle')['completion_status'] == 'COMPLETE'
    current = book.report_snapshot('LLM', FIRST, mode='recent')
    before_current = deepcopy(current)
    before = tree(book.root)
    monkeypatch.setattr(audit, 'resolve_plan', lambda *a: pytest.fail('new fill unnecessarily expanded plan'))
    result = audit.audit_account(book.root, current, materialize=False)
    assert result['state'] == 'COMPLETE' and len(result['fills']) == 1
    assert result['plans'][0]['fills'] == result['fills']
    assert len(result['plans'][0]['orders']) == 1 and result['orders'][0]['status'] == 'FILLED'
    assert result['plans'][0]['calculation']['ranking'] == core['calculation']['ranking']
    assert report == before_report and current == before_current and tree(book.root) == before


@pytest.mark.parametrize('dependency', ['core', 'nested'])
def test_summary_rechecks_entire_strategy_closure(execution_book, dependency):
    book = execution_book
    request, core = strategy_request(book, nested=True)
    frozen = freeze(book, request)
    report = book.report_snapshot('LLM', FIRST, mode='recent')
    warm_summary(book, report)
    if dependency == 'core':
        reference = frozen['decision_ref']['decision_core']
    else:
        middle = decision_evidence.read_business_document(book.root, core['calculation']['audit_attachment'])[0]
        reference = middle['model_reply']
    (book.root / reference['path']).unlink()
    assert audit.audit_account(book.root, report, materialize=False)['state'] == 'PARTIAL'


@pytest.mark.parametrize('change', ['privacy', 'source'])
def test_privacy_or_projection_source_change_invalidates_summary(execution_book, monkeypatch, change):
    book = execution_book
    request, _ = strategy_request(book)
    freeze(book, request)
    report = book.report_snapshot('LLM', FIRST, mode='recent')
    first = warm_summary(book, report)
    assert 'ranking' in first['plans'][0]['calculation']
    if change == 'privacy':
        monkeypatch.setattr(audit, 'PRIVATE_FIELDS', audit.PRIVATE_FIELDS | {'ranking'})
    else:
        source = book.root / 'projection.py'
        source.write_bytes(Path(audit.__file__).read_bytes() + b'\n# changed projection identity\n')
        monkeypatch.setattr(audit, '__file__', str(source))
    calls, resolve = [], audit.resolve_plan

    def checked(*args):
        calls.append(1)
        return resolve(*args)

    monkeypatch.setattr(audit, 'resolve_plan', checked)
    before = tree(book.root)
    result = audit.audit_account(book.root, report, materialize=False)
    assert result['state'] == 'COMPLETE' and calls == [1]
    if change == 'privacy':
        assert 'ranking' not in result['plans'][0]['calculation']
    assert tree(book.root) == before


def test_cold_readonly_summary_cannot_create_persistent_receipt(execution_book):
    book = execution_book
    report, _ = manual_report(book)
    clear_memory()
    before = tree(book.root)
    with decision_evidence.verification_scope(book.root, persist=False) as metrics:
        assert audit.audit_account(book.root, report, materialize=False)['state'] == 'COMPLETE'
    assert metrics['receipts_written'] == 0 and tree(book.root) == before
