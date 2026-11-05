"""Regression for repeated waiting, compact witnesses and bounded statements."""

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from test_execution_matching import bundle, inputs, matched
from test_ledger_trading import book, call

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.common.evidence_objects import is_reference, read_json
from china_stock_sim.common.execution_views import (
    compact_view_evidence,
    statement_record,
    waiting_identity,
)
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.ledger.kernel import LedgerError, canonical
from china_stock_sim.ledger.statements import continuous_watermarks

assert book


def test_repeated_waits_do_not_nest_old_results_then_one_fill(book, monkeypatch):
    plan, _ = inputs(book)
    original = book.market.prices

    def missing_volume(*args, **kwargs):
        value = original(*args, **kwargs)
        return replace(value, data=[{k: v for k, v in row.items() if k != 'volume'}
                                    for row in value.data])

    monkeypatch.setattr(book.market, 'prices', missing_volume)
    sizes, identities = [], []
    for index in range(6):
        result = asdict(matched(book, plan, data=bundle(book, plan))[0])
        assert result['state'] == 'WAITING_DATA'
        witness = result['account_view_evidence']
        assert witness['evidence_schema'] == 2 and 'frozen_plan' not in witness
        assert all('last_attempt' not in r['order'] for r in witness['orders'])
        identities.append(waiting_identity(result))
        receipt = call(book, 'apply_execution_results', result, op=f'wait-{index}')
        assert receipt['state'] == 'WAITING_DATA', receipt
        saved = book._read('LLM', lambda tx: tx.list('orders')[0])
        reference = saved['last_attempt']['account_view_evidence']
        assert is_reference(reference)
        assert read_json(book.root, reference) == witness
        sizes.append(len(canonical(saved)))
    assert len(set(identities)) == 1
    assert max(sizes) < 100_000 and max(sizes) - min(sizes) < 2_000
    monkeypatch.setattr(book.market, 'prices', original)
    result = asdict(matched(book, plan)[0])
    assert call(book, 'apply_execution_results', result, op='fill')['state'] == 'APPLIED'
    fills = book._read('LLM', lambda tx: tx.list('fills'))
    assert len(fills) == 1 and is_reference(fills[0]['account_view_evidence'])
    assert len(canonical(fills[0])) < 100_000
    assert matched(book, plan) == ()


def test_rehashed_compact_witness_cannot_change_frozen_identity(book):
    plan, _ = inputs(book)
    result = asdict(matched(book, plan)[0])
    witness = result['account_view_evidence']
    witness['frozen_plan_binding']['plan_hash'] = 'f' * 64
    witness['view_hash'] = policy_digest({k: v for k, v in witness.items()
                                         if k not in {'view_hash', 'snapshot_at'}})
    result['account_view_hash'] = witness['view_hash']
    assert call(book, 'apply_execution_results', result)['state'] == 'INTEGRITY_ERROR'
    assert book._read('LLM', lambda tx: tx.list('fills')) == []


def test_budget_stops_before_match_or_any_write(book, monkeypatch):
    app = ExecutionApplication(book.settings, market=book.market, ledger=book,
                               clock=book._now, test_mode=True)
    monkeypatch.setattr(app, 'match', lambda *a, **k: pytest.fail('expired budget did work'))
    before = book.settings.database.read_bytes()
    result = app.settle('LLM', 'plan', operation_id='budget', should_continue=lambda: False)
    assert result['reason'] == 'RUN_BUDGET_EXHAUSTED' and result['receipts'] == []
    assert book.settings.database.read_bytes() == before


def test_size_guard_and_witness_ignore_unrelated_historical_payload():
    with pytest.raises(LedgerError, match='上限|大小|记录|超出'):
        canonical({'data': 'a' * 2000}, max_bytes=1000)
    view = {'frozen_plan': {'plan_id': 'p', 'plan_hash': 'h', 'account_id': 'a',
             'target_session': '2026-11-02', 'data_evidence': 'x' * 1_000_000},
            'snapshot_at': '2026-11-03T10:00:00+08:00', 'orders': [{
                'order': {'id': 'o', 'result': {'account_view_evidence': 'y' * 1_000_000}},
                'result': {'account_view_evidence': 'y' * 1_000_000}}]}
    assert len(json.dumps(compact_view_evidence(view))) < 2000


def test_wait_identity_changes_on_new_facts_not_only_later_read_cutoff():
    original = {'state': 'WAITING_DATA', 'reason_code': 'MISSING_VOLUME',
        'knowledge_cutoff': '2026-11-02T19:00:00+08:00', 'data_evidence': [{
            'kwargs': {'knowledge_cutoff': '2026-11-02T19:00:00+08:00'},
            'result': {'observation_ids': ['original'], 'data': {'volume': None}}}]}
    later = deepcopy(original)
    later['knowledge_cutoff'] = '2026-11-02T19:15:00+08:00'
    later['data_evidence'][0]['kwargs']['knowledge_cutoff'] = later['knowledge_cutoff']
    assert waiting_identity(original) == waiting_identity(later)
    later['data_evidence'][0]['result']['observation_ids'] = ['revised']
    assert waiting_identity(original) != waiting_identity(later)
    for field in ('knowledge_cutoff', 'snapshot_at'):
        changed_fact = deepcopy(original)
        changed_fact['data_evidence'][0]['result']['data'][field] = 'new source fact'
        assert waiting_identity(original) != waiting_identity(changed_fact)


def test_watermarks_cannot_cross_missing_calendar_day_or_provisional_gap():
    complete = {'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL'}
    statements = {'2026-10-30': complete, '2026-11-02': complete}
    rows = [{'date': '2026-10-30', 'is_trading_day': 1},
            {'date': '2026-11-01', 'is_trading_day': 0},
            {'date': '2026-11-02', 'is_trading_day': 1}]
    assert continuous_watermarks(statements, '2026-10-30', rows) == ('2026-10-30', '2026-10-30')
    rows.append({'date': '2026-10-31', 'is_trading_day': 0})
    assert continuous_watermarks(statements, '2026-10-30', rows) == ('2026-11-02', '2026-11-02')
    statements['2026-10-30'] = {**complete, 'valuation_status': 'PROVISIONAL'}
    assert continuous_watermarks(statements, '2026-10-30', rows) == ('2026-11-02', None)


def test_statement_reference_preserves_waiting_order_after_current_order_changes(tmp_path):
    order = {'id': 'order-1', 'account_id': 'S1', 'status': 'WAITING_DATA',
             'last_attempt': {'reason_code': 'MISSING_VOLUME', 'data_evidence': ['original']}}
    saved = statement_record(tmp_path, order, 'orders')
    same = statement_record(tmp_path, order, 'orders')
    assert saved['record_reference']['snapshot'] == same['record_reference']['snapshot']
    original = deepcopy(order)
    order.update(status='FILLED', last_attempt=None)
    historical = read_json(tmp_path, saved['record_reference']['snapshot'])
    assert historical == original
    assert policy_digest(historical) == saved['record_reference']['content_hash']


def test_record_guard_rejects_before_copying_tree(monkeypatch):
    from china_stock_sim.ledger import kernel
    monkeypatch.setattr(kernel, '_json_value', lambda value: pytest.fail('copied oversized input'))
    with pytest.raises(LedgerError) as failure:
        kernel.canonical({'data': ['x' * 1000] * 100}, max_bytes=1000)
    assert failure.value.state == 'RECORD_TOO_LARGE'
