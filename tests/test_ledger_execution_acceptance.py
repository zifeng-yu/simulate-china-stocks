"""Consumer acceptance: immutable input, fresh per-order guards and query entry."""
from copy import deepcopy

import pytest
from test_ledger_trading import DAY, book, call, execute_payload, plan_payload

from china_stock_sim.ledger.cli import execute, register_commands
from china_stock_sim.ledger.kernel import digest

assert book


def frozen(book, orders=None):
    request = plan_payload(book, orders=orders)
    assert call(book, 'freeze_plan', request)['state'] == 'APPLIED'
    return book.get_plan('LLM', request['plan_id'])


def at_end(book):
    from datetime import datetime
    book.test_now[0] = datetime.fromisoformat(DAY + 'T20:00:00+08:00')


@pytest.mark.parametrize('change,expected', [
    ('hash', 'INTEGRITY_ERROR'), ('future_revision', 'STALE_ACCOUNT'),
    ('original_resources', 'INTEGRITY_ERROR'), ('wrong_account', 'INVALID_INPUT'),
    ('before_freeze', 'INVALID_INPUT'), ('missing_result_id', 'INVALID_INPUT'),
    ('missing_policy_for_wait', 'INVALID_INPUT'),
])
def test_execution_view_cannot_be_substituted_to_authorize_booking(book, change, expected):
    plan = frozen(book)
    at_end(book)
    payload = execute_payload(book, plan, plan['orders'][0])
    view = payload['account_view_evidence']
    if change == 'hash':
        payload['account_view_hash'] = 'invented'
    elif change == 'future_revision':
        view['account_revision'] += 2
        payload['account_revision'] = view['account_revision']
    elif change == 'original_resources':
        view['original_resources']['available_cash_fen'] += 10000
    elif change == 'wrong_account':
        view['account_id'] = 'S1'
    elif change == 'before_freeze':
        view['snapshot_at'] = '2026-10-29T19:00:00+08:00'
    elif change == 'missing_result_id':
        payload.pop('result_id')
    else:
        payload = execute_payload(book, plan, plan['orders'][0], state='WAITING_DATA')
        payload.pop('ruleset_hash')
    if change in {'future_revision', 'original_resources', 'wrong_account'}:
        view['view_hash'] = digest({k: v for k, v in view.items() if k not in {'view_hash', 'snapshot_at'}})
        payload['account_view_hash'] = view['view_hash']
    before = book.settings.database.read_bytes()
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == expected, result
    assert book.settings.database.read_bytes() == before
    assert book.fills('LLM')['total'] == 0
    assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 1000510


def test_multi_result_batch_reuses_initial_view_but_rechecks_each_live_guard(book):
    plan = frozen(book, [{'code': code, 'side': 'BUY', 'quantity': 100,
        'limit_price': '10', 'reason': 'acceptance'} for code in ('SH600000', 'SH600001')])
    at_end(book)
    results = [execute_payload(book, plan, order) for order in plan['orders']]
    assert results[0]['account_view_hash'] == results[1]['account_view_hash']
    result = call(book, 'apply_execution_results', {'results': results}, op='batch')
    assert result['state'] == 'APPLIED', result
    fills = book.fills('LLM')['rows']
    assert len(fills) == 2
    assert fills[0]['commit_guard_hash'] != fills[1]['commit_guard_hash']
    after = book.settings.database.read_bytes()
    again = call(book, 'apply_execution_results', {'results': results}, op='batch', expected=0)
    assert again['state'] == 'APPLIED', again
    assert book.settings.database.read_bytes() == after
    assert book.account_state('LLM', DAY)['cash_fen'] == 5000000 - 2*100501


def test_limit_miss_can_use_only_price_without_unknown_st_context(book):
    plan = frozen(book)
    at_end(book)
    payload = execute_payload(book, plan, plan['orders'][0], state='NOT_FILLED')
    payload['execution_context'] = None
    payload['market_snapshot_ids'] = []
    payload['data_evidence'] = [q for q in payload['data_evidence'] if q['method'] == 'prices']
    payload['reason_evidence'] = {'observation_ids': payload['data_evidence'][0]['result']['observation_ids']}
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == 'APPLIED', result
    assert book.account_state('LLM', DAY)['cash_fen'] == 5000000
    assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 0
    assert book.fills('LLM')['total'] == 0


def test_execution_query_cli_is_read_only_and_returns_full_plan(book):
    import argparse
    plan = frozen(book)
    parser = argparse.ArgumentParser()
    register_commands(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(['ledger-query', '--kind', 'execution', '--account', 'LLM',
                              '--plan-id', plan['plan_id']])
    before = book.settings.database.read_bytes()
    result, code = execute(args, book.root)
    assert code in (0, 2)
    assert result['plan_id'] == plan['plan_id']
    assert result['original_resources']['available_cash_fen'] == 5000000
    assert result['orders'][0]['own_reserved_cash_fen'] == 1000510
    assert book.settings.database.read_bytes() == before


def test_refresh_of_future_event_does_not_erase_frozen_known_risk(book, monkeypatch):
    from china_stock_sim.market.contracts import DataResult
    event = {'code': 'SH600000', 'event_id': 'complex', 'kind': 'COMPLEX', 'status': 'IMPLEMENTED',
             'ex_date': '2026-11-05', 'available_at': book._now().isoformat()}
    monkeypatch.setattr(book.market, 'events', lambda *a, **k: DataResult('READY', [deepcopy(event)], (), ('original-event',)))
    plan = frozen(book)
    at_end(book)
    event['available_at'] = book._now().isoformat()
    payload = execute_payload(book, plan, plan['orders'][0], state='NOT_FILLED')
    payload['reason_code'] = 'BUY_INELIGIBLE'
    payload['reason_details'] = {'qualification': 'KNOWN_COMPLEX_ACTION'}
    payload['reason_evidence']['observation_ids'].append('original-event')
    assert plan['event_facts']['SH600000']['result']['data'][0]['available_at'] != event['available_at']
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == 'APPLIED', result
    assert book.fills('LLM')['total'] == 0
    assert book.account_state('LLM', DAY)['cash_fen'] == 5000000


@pytest.mark.parametrize('state,field', [('NOT_FILLED', 'quantity'), ('WAITING_DATA', 'quantity'),
                                      ('WAITING_DATA', 'price'), ('WAITING_DATA', 'fees_fen')])
def test_raw_results_require_explicit_economic_fields_and_integer_zero(book, state, field):
    plan = frozen(book)
    at_end(book)
    payload = execute_payload(book, plan, plan['orders'][0], state=state)
    if state == 'NOT_FILLED':
        payload[field] = False
    else:
        payload.pop(field)
    before = book.settings.database.read_bytes()
    result = call(book, 'apply_execution_results', payload)
    assert result['state'] == 'INVALID_INPUT', result
    assert book.settings.database.read_bytes() == before
    assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 1000510
