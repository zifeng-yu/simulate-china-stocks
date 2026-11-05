"""B21: evidence must prove the right security, date, purpose and execution rules."""

from copy import deepcopy
from datetime import datetime

import pytest
from test_ledger_trading import (
    CODE,
    DAY,
    book,
    call,
    execute_payload,
    plan_payload,
)

from china_stock_sim.market.contracts import DataResult

assert book  # Register shared isolated fixture.


def result_snapshot(book, payload):
    return book.market.snapshots[payload['market_snapshot_ids'][0]]


def frozen_order(book):
    payload = plan_payload(book)
    assert call(book, 'freeze_plan', payload)['state'] == 'APPLIED'
    plan = book.get_plan('LLM', payload['plan_id'])
    book.test_now[0] = datetime.fromisoformat('2026-10-30T20:00:00+08:00')
    return execute_payload(book, plan, plan['orders'][0])


def assert_no_booking(book, payload, expected):
    before = book.settings.database.read_bytes()
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == expected, result
    assert book.settings.database.read_bytes() == before
    assert book.fills('LLM')['total'] == 0
    current = book.account_state('LLM', '2026-11-02')
    assert current['cash_fen'] == 5_000_000
    assert current['reserved_cash_fen'] == 1_000_510


@pytest.mark.parametrize('mutation', ['events', 'empty', 'wrong_code', 'future_row', 'zero', 'no_ids'])
def test_plan_requires_actual_usable_price_rows(book, mutation):
    payload = plan_payload(book)
    content = result_snapshot(book, payload)['body']['content']
    result = content['results'][CODE]['valuation']
    if mutation == 'events':
        content['results'][CODE] = {'events': dict(result, data=[])}
    elif mutation == 'empty':
        result['data'] = []
    elif mutation == 'wrong_code':
        result['data'][0]['code'] = 'SZ000001'
    elif mutation == 'future_row':
        result['data'][0]['date'] = DAY
    elif mutation == 'zero':
        result['data'][0]['close'] = '0'
    else:
        result['observation_ids'] = []
    before = book.settings.database.read_bytes()
    assert call(book, 'freeze_plan', payload)['state'] == 'WAITING_DATA'
    assert book.settings.database.read_bytes() == before


def test_plan_accepts_causal_adjusted_prices(book):
    payload = plan_payload(book)
    content = result_snapshot(book, payload)['body']['content']
    original = content['results'][CODE].pop('valuation')
    original['data'][0]['adjusted_close'] = original['data'][0].pop('close')
    content['results'][CODE]['rules'] = original
    assert call(book, 'freeze_plan', payload)['state'] == 'APPLIED'


@pytest.mark.parametrize('mutation,expected', [
    ('no_execution', 'WAITING_DATA'), ('empty_price', 'WAITING_DATA'),
    ('wrong_row_date', 'WAITING_DATA'), ('wrong_row_code', 'WAITING_DATA'),
    ('missing_context', 'INVALID_INPUT'), ('different_rule', 'INVALID_INPUT'),
    ('different_limit', 'INVALID_INPUT'), ('missing_observation', 'INTEGRITY_ERROR'),
    ('different_snapshot_cutoff', 'WAITING_DATA'),
])
def test_fill_cannot_use_unrelated_or_incomplete_frozen_facts(book, mutation, expected):
    payload = frozen_order(book)
    body = result_snapshot(book, payload)['body']
    result = body['content']['results'][CODE]
    if mutation == 'no_execution':
        result.pop('execution')
    elif mutation == 'empty_price':
        result['valuation']['data'] = []
    elif mutation == 'wrong_row_date':
        result['valuation']['data'][0]['date'] = '2026-10-29'
    elif mutation == 'wrong_row_code':
        result['valuation']['data'][0]['code'] = 'SZ000001'
    elif mutation == 'missing_context':
        payload.pop('execution_context')
    elif mutation == 'different_rule':
        payload['execution_context']['rule_id'] = 'different-rule'
    elif mutation == 'different_limit':
        payload['execution_context']['limit_rate'] = '0.2'
    elif mutation == 'missing_observation':
        body['observation_ids'].remove('security-proof')
    else:
        body['content']['knowledge_cutoff'] = '2026-10-30T19:59:00+08:00'
    if mutation in {'empty_price', 'wrong_row_date', 'wrong_row_code'}:
        # Remove the newly required independent price query so this still tests
        # the absence of any usable same-day price, not a redundant bad snapshot.
        payload['data_evidence'] = [q for q in payload['data_evidence'] if q['method'] != 'prices']
    assert_no_booking(book, payload, expected)


def test_fill_checks_current_public_execution_qualification(book, monkeypatch):
    payload = frozen_order(book)
    monkeypatch.setattr(book.market, 'readiness', lambda *a, **k: DataResult(
        'WAITING_DATA', {'code': CODE, 'date': DAY}, ('unknown-rule',), ('fact',)))
    assert_no_booking(book, payload, 'WAITING_DATA')


def test_same_public_query_evidence_can_replace_snapshot(book):
    payload = frozen_order(book)
    payload['market_snapshot_ids'] = []
    cutoff = payload['knowledge_cutoff']
    prices = {'code': CODE, 'start': DAY, 'end': DAY, 'knowledge_cutoff': cutoff}
    execution = {'code': CODE, 'as_of': DAY, 'purpose': 'execution',
                 'knowledge_cutoff': cutoff, 'rule_context': payload['execution_context']}
    payload['data_evidence'] = payload['data_evidence'] + [
        {'method': 'prices', 'kwargs': prices, 'result': book.market.prices(**prices).as_dict()},
        {'method': 'readiness', 'kwargs': execution, 'result': book.market.readiness(**execution).as_dict()},
    ]
    assert call(book, 'apply_execution_results', payload)['state'] == 'APPLIED'


@pytest.mark.parametrize('state', ['SIMULATED_FILL', 'NOT_FILLED'])
def test_terminal_cannot_be_booked_before_evening(book, state):
    payload = frozen_order(book)
    book.test_now[0] = datetime.fromisoformat('2026-10-30T18:09:59+08:00')
    plan = book.get_plan('LLM', payload['plan_id'])
    payload = execute_payload(book, plan, plan['orders'][0], state=state)
    assert_no_booking(book, payload, 'NOT_DUE')


@pytest.mark.parametrize('computed,cutoff', [
    ('2026-10-30T18:09:59+08:00', '2026-10-30T18:09:59+08:00'),
    ('2026-10-30T19:00:00+08:00', '2026-10-30T20:00:00+08:00'),
    ('2026-10-29T19:00:00+08:00', '2026-10-29T19:00:00+08:00'),
])
def test_terminal_cannot_claim_early_or_noncausal_computation(book, computed, cutoff):
    payload = frozen_order(book)
    payload.update(computed_at=computed, knowledge_cutoff=cutoff)
    assert_no_booking(book, payload, 'INVALID_INPUT')


def test_waiting_may_be_recorded_early_without_economic_effect(book):
    original = frozen_order(book)
    book.test_now[0] = datetime.fromisoformat('2026-10-30T10:00:00+08:00')
    plan = book.get_plan('LLM', original['plan_id'])
    payload = execute_payload(book, plan, plan['orders'][0], state='WAITING_DATA')
    payload['market_snapshot_ids'] = []
    assert call(book, 'apply_execution_results', payload)['state'] == 'WAITING_DATA'
    current = book.account_state('LLM', '2026-11-02')
    assert current['cash_fen'] == 5_000_000 and current['reserved_cash_fen'] == 1_000_510
    assert book.fills('LLM')['total'] == 0


def test_unfilled_can_use_known_suspension_without_execution_ready(book):
    original = frozen_order(book)
    plan = book.get_plan('LLM', original['plan_id'])
    payload = execute_payload(book, plan, plan['orders'][0], state='NOT_FILLED')
    result = result_snapshot(book, payload)['body']['content']['results'][CODE]
    result.pop('valuation')
    result['execution']['state'] = 'WAITING_DATA'
    result['execution']['data']['tradestatus'] = 0
    payload['execution_context'] = None
    payload['reason_code'] = 'SUSPENDED'
    payload['data_evidence'] = []
    payload['reason_evidence'] = {'observation_ids': result['execution']['observation_ids']}
    assert call(book, 'apply_execution_results', payload)['state'] == 'APPLIED'
    assert book.account_state('LLM', '2026-11-02')['reserved_cash_fen'] == 0
    assert book.fills('LLM')['total'] == 0


def test_snapshot_execution_observations_cannot_be_substituted(book):
    payload = frozen_order(book)
    result = result_snapshot(book, payload)['body']['content']['results'][CODE]
    copied = deepcopy(result['execution'])
    copied['observation_ids'] = [f'{CODE}:{DAY}']
    result['execution'] = copied
    assert_no_booking(book, payload, 'WAITING_DATA')


@pytest.mark.parametrize('state', ['SIMULATED_FILL', 'NOT_FILLED'])
def test_evening_submission_cannot_use_intraday_or_missing_cutoff(book, state):
    original = frozen_order(book)
    plan = book.get_plan('LLM', original['plan_id'])
    payload = execute_payload(book, plan, plan['orders'][0], state=state)
    payload['knowledge_cutoff'] = '2026-10-30T10:00:00+08:00'
    assert_no_booking(book, payload, 'INVALID_INPUT')
    payload.pop('knowledge_cutoff')
    assert_no_booking(book, payload, 'INVALID_INPUT')
