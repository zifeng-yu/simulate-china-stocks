"""Explicit maintenance stops bad rules without rewriting frozen decisions."""

import json

import pytest
from execution_fixtures import FIRST, advance, execution_book, proposal
from test_execution_cli import app, tree

from china_stock_sim.common.policies import policy_digest

assert execution_book


def block(book, *, code=None, known='2026-10-30T19:30:00+08:00',
          start=FIRST, end='2026-12-31', rule_ids=None):
    path = book.root / 'configs/execution_rules.toml'
    import tomllib
    registry = tomllib.loads(path.read_text())
    if rule_ids is None:
        rule_ids = [r['rule_id'] for r in registry['rules']]
    record = {'block_id': 'confirmed-legal-correction', 'rule_ids': rule_ids,
              'effective_from': start, 'effective_to': end, 'known_at': known,
              'reason': 'Confirmed fixture rule discrepancy', 'source': 'isolated official fixture'}
    if code:
        record['codes'] = [code]
    text = path.read_text().replace('execution_blocks = []\n', '')
    text += '\n[[execution_blocks]]\n' + '\n'.join(
        f'{key} = {json.dumps(value)}' for key, value in record.items()) + '\n'
    path.write_text(text)


def test_active_rule_hold_blocks_existing_order_even_with_earlier_cutoff(execution_book):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='before-hold')['plan']
    advance(book, FIRST)
    block(book)
    result = app(book).settle('LLM', plan['plan_id'], operation_id='held',
                              knowledge_cutoff=FIRST + 'T18:10:00+08:00')
    assert result['completion_status'] == 'PARTIAL', result
    assert result['results'][0]['reason_code'] == 'RULE_POLICY_CONFLICT'
    assert book.fills('LLM')['total'] == 0
    assert book.account_state('LLM', FIRST)['reserved_cash_fen'] == 1000510
    assert book.get_plan('LLM', plan['plan_id'])['plan_hash'] == plan['plan_hash']


def test_new_plan_waits_for_confirmed_rule_maintenance(execution_book):
    book = execution_book
    block(book, known='2026-10-29T19:00:00+08:00')
    before = tree(book.root)
    result = app(book).freeze(proposal(book), operation_id='blocked-new')
    assert result['state'] == 'WAITING_VALIDATION'
    assert result['validation']['issues'][0]['kind'] == 'RULE_POLICY_CONFLICT'
    assert result['plan'] is None and tree(book.root) == before
    report = result['validation']
    assert report['validation_id'] == 'validation:' + policy_digest({
        k: v for k, v in report.items() if k not in {'validation_id', 'plan_envelope'}})


@pytest.mark.parametrize('kwargs', [
    {'known': '2026-11-01T00:00:00+08:00'}, {'start': '2026-11-01'},
    {'end': '2026-10-29', 'start': '2026-10-01'}, {'rule_ids': ['unrelated-rule']},
])
def test_unrelated_or_not_yet_known_maintenance_does_not_change_original_plan(execution_book, kwargs):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='allowed')['plan']
    advance(book, FIRST)
    block(book, **kwargs)
    result = app(book).settle('LLM', plan['plan_id'], operation_id='unaffected')
    assert result['completion_status'] == 'COMPLETE', result


def test_terminal_orders_are_not_rewritten_by_later_maintenance(execution_book):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='terminal')['plan']
    advance(book, FIRST)
    assert app(book).settle('LLM', plan['plan_id'], operation_id='first')['completion_status'] == 'COMPLETE'
    block(book)
    before = tree(book.root)
    result = app(book).settle('LLM', plan['plan_id'], operation_id='repeat')
    assert result['completion_status'] == 'COMPLETE' and tree(book.root) == before
    assert book.fills('LLM')['total'] == 1


def test_cleared_maintenance_resumes_original_quantity_without_replanning(execution_book):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='resumable')['plan']
    advance(book, FIRST)
    config = book.root / 'configs/execution_rules.toml'
    original = config.read_bytes()
    block(book)
    assert app(book).settle('LLM', plan['plan_id'], operation_id='hold')['completion_status'] == 'PARTIAL'
    config.write_bytes(original)
    result = app(book).settle('LLM', plan['plan_id'], operation_id='release')
    assert result['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['rows'][0]['quantity'] == 1000
    assert book.get_plan('LLM', plan['plan_id'])['plan_hash'] == plan['plan_hash']


def test_post_commit_read_error_keeps_error_and_committed_receipt(execution_book, monkeypatch):
    book = execution_book
    service = app(book)
    plan = service.freeze(proposal(book), operation_id='read-failure')['plan']
    advance(book, FIRST)
    real = service.match
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs) if len(calls) == 1 else {
            'state': 'IO_ERROR', 'completion_status': 'ERROR', 'results': [],
            'submission_status': 'NOT_SUBMITTED'}
    monkeypatch.setattr(service, 'match', fail_second)
    result = service.settle('LLM', plan['plan_id'], operation_id='read-fail')
    assert result['state'] == 'IO_ERROR' and result['completion_status'] == 'ERROR'
    assert result['receipts'] and book.fills('LLM')['total'] == 1
    assert app(book).settle('LLM', plan['plan_id'], operation_id='read-resume')['completion_status'] == 'COMPLETE'
    assert book.fills('LLM')['total'] == 1
