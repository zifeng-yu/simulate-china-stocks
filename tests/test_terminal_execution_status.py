"""Completed executions reuse original-bound headers without financial mutation."""

import hashlib
import json
import sqlite3
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace

import pytest
from execution_fixtures import FIRST, advance, execution_book, proposal
from test_strategy_acceptance import strategy_world

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.ledger import decision_evidence
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import canonical, digest
from china_stock_sim.ledger.plan_storage import plan_header

assert execution_book and strategy_world


def app(book):
    return ExecutionApplication(book.settings, market=book.market, ledger=book,
                                clock=book._now, test_mode=True)


def completed(book):
    saved = app(book).freeze(proposal(book), operation_id='terminal-plan')
    assert saved['state'] == 'APPLIED', saved
    plan = saved['plan']
    advance(book, FIRST)
    result = app(book).settle('LLM', plan['plan_id'], operation_id='terminal-first')
    assert result['completion_status'] == 'COMPLETE', result
    return plan


def tree(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_cold_large_original_once_then_persistent_terminal_summary_without_expansion_or_write(
        execution_book, monkeypatch, record_property):
    book = execution_book
    original = completed(book)
    # A historical-sized original with the same economic contract. This is a
    # temporary resolver fixture, not a new financial freeze or production edit.
    body = {k: v for k, v in original.items() if k not in {'id', 'document'}}
    body['legacy_audit_fixture'] = 'x' * (18 * 1024 * 1024)
    reference = document(book.root, 'frozen', body)
    header = plan_header({**body, 'document': reference})
    with sqlite3.connect(book.settings.database) as db:
        db.execute('UPDATE plans SET data=? WHERE account_id=? AND id=?',
                   (canonical(header), 'LLM', original['plan_id']))
    assert book.terminal_execution_status('LLM', original['plan_id'])['reason'] == 'PLAN_HEADER_VERIFICATION_REQUIRED'
    before = book.settings.database.read_bytes()
    calls = []
    real_view = ExecutionApplication._view

    def full(self, *args):
        calls.append(1)
        return real_view(self, *args)

    monkeypatch.setattr(ExecutionApplication, '_view', full)
    started = perf_counter()
    with decision_evidence.verification_scope(book.root, persist=True):
        cold = app(book).settle('LLM', original['plan_id'], operation_id='cold-terminal')
    cold_seconds = perf_counter() - started
    assert cold['completion_status'] == 'COMPLETE' and calls == [1]
    assert cold['audit_scope'] == 'TERMINAL_STATUS'
    assert book.settings.database.read_bytes() == before
    decision_evidence._VALIDATIONS.clear()
    decision_evidence._DOCUMENTS.clear()
    decision_evidence._DOCUMENT_BYTES = 0
    monkeypatch.setattr(ExecutionApplication, '_view', lambda *a: pytest.fail('warm terminal expanded plan'))
    monkeypatch.setattr(book.market, 'calendar_operation', lambda *a, **k: pytest.fail('warm terminal read market'))
    before_tree = tree(book.root)
    started = perf_counter()
    with decision_evidence.verification_scope(book.root, persist=True) as metrics:
        warm = app(book).settle('LLM', original['plan_id'], operation_id='warm-terminal')
    warm_seconds = perf_counter() - started
    assert warm['completion_status'] == 'COMPLETE' and warm['submission_status'] == 'UNCHANGED'
    assert warm['receipts'] == warm['calculations'] == warm['results'] == []
    assert 'account_view' not in warm and warm['step_receipt']['state'] == 'ALREADY_APPLIED'
    assert metrics['bytes_read'] < 10_000 and metrics['receipts_written'] == 0
    assert tree(book.root) == before_tree
    record_property('cold_seconds', cold_seconds)
    record_property('warm_seconds', warm_seconds)
    record_property('logical_original_fixture_bytes', 18 * 1024 * 1024)
    assert warm_seconds < 1


@pytest.mark.parametrize('damage', ['missing_original', 'tampered_object', 'header_self_hash',
    'missing_order', 'order_quantity', 'missing_fill', 'reserved_cash'])
def test_terminal_summary_refuses_original_header_or_financial_index_damage(execution_book, damage):
    book = execution_book
    plan = completed(book)
    assert book.terminal_execution_status('LLM', plan['plan_id'])['state'] == 'READY'
    if damage in {'missing_original', 'tampered_object'}:
        path = book.root / plan['document']['path']
        if damage == 'missing_original':
            path.unlink()
        else:
            raw = json.loads(path.read_bytes())
            if raw.get('storage_format') == 'cas-json-v1':
                path = book.root / raw['object_ref']['_evidence_ref']['path']
            content = bytearray(path.read_bytes())
            content[len(content) // 2] ^= 1
            path.write_bytes(content)
    else:
        with sqlite3.connect(book.settings.database) as db:
            order_id = plan['order_ids'][0]
            if damage == 'header_self_hash':
                raw = json.loads(db.execute('SELECT data FROM plans WHERE id=?', (plan['plan_id'],)).fetchone()[0])
                raw['orders'][0]['quantity'] += 100
                raw['header_hash'] = digest({k: v for k, v in raw.items()
                    if k not in {'id', 'original_record_ref', 'header_hash'}})
                db.execute('UPDATE plans SET data=? WHERE id=?', (canonical(raw), plan['plan_id']))
            elif damage in {'missing_order', 'missing_fill'}:
                table = 'orders' if damage == 'missing_order' else 'fills'
                db.execute(f'DELETE FROM {table} WHERE id=?', (order_id,))
            elif damage == 'order_quantity':
                db.execute("UPDATE orders SET data=json_set(data,'$.quantity',999) WHERE id=?", (order_id,))
            else:
                db.execute("UPDATE reservations SET data=json_set(data,'$.status','ACTIVE') WHERE id=?", (order_id,))
    before = book.settings.database.read_bytes()
    result = app(book).settle('LLM', plan['plan_id'], operation_id='damage-terminal')
    assert result['state'] == 'INTEGRITY_ERROR', result
    assert book.settings.database.read_bytes() == before


def test_pending_order_still_uses_original_match_and_commit_path(execution_book, monkeypatch):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='pending-plan')['plan']
    assert book.terminal_execution_status('LLM', plan['plan_id'])['reason'] == 'ORDERS_NOT_TERMINAL'
    advance(book, FIRST)
    calls, real = [], ExecutionApplication._view

    def full(self, *args):
        calls.append(1)
        return real(self, *args)

    monkeypatch.setattr(ExecutionApplication, '_view', full)
    result = app(book).settle('LLM', plan['plan_id'], operation_id='pending-settle')
    assert result['completion_status'] == 'COMPLETE' and calls
    assert len(result['receipts']) == 1 and book.fills('LLM')['total'] == 1


@pytest.mark.parametrize('damage', ['index', 'original'])
def test_terminal_strategy_status_rejects_missing_authoritative_frozen_decision(strategy_world, damage):
    book, make_app, _ = strategy_world
    assert make_app().publish('S1', FIRST)['state'] == 'FROZEN'
    plan = book.get_plan('S1', target_session=FIRST)
    advance(book, FIRST)
    assert app(book).settle('S1', plan['plan_id'], operation_id='strategy-complete')['completion_status'] == 'COMPLETE'
    assert book.terminal_execution_status('S1', plan['plan_id'])['state'] == 'READY'
    if damage == 'index':
        with sqlite3.connect(book.settings.database) as db:
            db.execute('DELETE FROM strategy_decisions WHERE account_id=?', ('S1',))
    else:
        decision = book._read('S1', lambda tx: tx.get('strategy_decisions', plan['decision_ref']['decision_id']))
        (book.root / decision['document']['path']).unlink()
    before = book.settings.database.read_bytes()
    assert app(book).settle('S1', plan['plan_id'], operation_id='strategy-missing')['state'] == 'INTEGRITY_ERROR'
    assert book.settings.database.read_bytes() == before


def test_workflow_execution_uses_locator_without_full_plan_read(execution_book, monkeypatch):
    book = execution_book
    plan = completed(book)
    workflow = WorkflowApplication(book.settings, ledger=book, market=book.market,
        clock=book._now, test_mode=True)
    workflow._remaining = lambda: 60
    workflow._observe = lambda name, function, **kwargs: function()
    workflow.days = SimpleNamespace(before_execution=lambda *a: {}, after_execution=lambda *a, **k: {},
        close=lambda *a: {'statement': {'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL'}})
    monkeypatch.setattr(book, 'get_plan', lambda *a, **k: pytest.fail('workflow expanded full plan for ID'))
    monkeypatch.setattr(workflow.execution, '_view', lambda *a: pytest.fail('terminal plan expanded'))
    result = workflow._settle_day('LLM', FIRST)
    assert result['state'] == 'COMPLETE' and result['execution']['plan_id'] == plan['plan_id']


def test_header_locator_does_not_decode_legacy_full_plan(execution_book, monkeypatch):
    from china_stock_sim.ledger.kernel import Transaction
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='locator-plan')['plan']
    with sqlite3.connect(book.settings.database) as db:
        db.execute('UPDATE plans SET data=? WHERE id=?', (canonical(deepcopy(plan)), plan['plan_id']))
    monkeypatch.setattr(Transaction, '_decode', lambda *a: pytest.fail('locator decoded legacy plan'))
    assert book.get_plan_header('LLM', target_session=FIRST)['plan_id'] == plan['plan_id']
    assert book.terminal_execution_status('LLM', plan['plan_id'])['reason'] == 'LEGACY_FULL_PLAN'
