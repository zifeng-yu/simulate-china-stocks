"""LLM application recovery through isolated real ledgers; no network/model service."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from execution_fixtures import FIRST, execution_book
from test_ledger_trading import revision
from test_strategy_acceptance import (
    ControlledModel,
    frozen,
    response,
    strategy_world,
    synchronous_caller,
)

from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.models.base import ModelError, ModelResult
from china_stock_sim.strategies.llm_call import ModelCallError

assert strategy_world and execution_book


def attempts(book):
    return book.report_snapshot('LLM', FIRST)['model_attempts']


@pytest.mark.parametrize('failure', [ModelError('fixture-private-error'), ModelCallError('TIMEOUT')])
def test_model_network_failure_can_retry_same_input_with_distinct_persisted_claim(strategy_world, failure):
    book, make_app, model = strategy_world
    called = []

    def caller(client, messages, **kwargs):
        called.append((messages, kwargs))
        if len(called) == 1:
            book.test_now[0] += timedelta(seconds=7)
            raise failure
        return synchronous_caller(client, messages, **kwargs)

    result = make_app(caller=caller).publish('LLM', FIRST)
    assert result['state'] == 'FROZEN', result
    rows = attempts(book)
    assert len(called) == len(rows) == 2
    assert rows[0]['status'] == ('TIMEOUT' if isinstance(failure, ModelCallError) else 'FAILED')
    assert rows[1]['status'] == 'SUCCEEDED'
    assert rows[0]['attempt_id'] != rows[1]['attempt_id']
    assert rows[0]['input_hash'] == rows[1]['input_hash']
    assert rows[0]['request_hash'] == rows[1]['request_hash']
    assert called[0][0] == called[1][0]
    assert 110 < called[1][1]['timeout_seconds'] <= 113
    assert len(model.calls) == 1


@pytest.mark.parametrize('timeout', [False, True])
def test_two_transport_failures_are_terminal_without_third_call_or_secret_error(strategy_world, timeout):
    book, make_app, _ = strategy_world
    count = []

    def failed(*args, **kwargs):
        count.append(kwargs['timeout_seconds'])
        if timeout:
            raise ModelCallError('TIMEOUT')
        raise ModelError('isolated-fixture-credential https://isolated.invalid/v1')

    app = make_app(caller=failed)
    result = app.publish('LLM', FIRST)
    assert result['state'] == 'MODEL_FAILED', result
    assert len(count) == 2
    assert [row['status'] for row in attempts(book)] == ['TIMEOUT' if timeout else 'FAILED'] * 2
    assert app.recover('LLM', FIRST)['state'] == 'MODEL_FAILED'
    assert len(count) == 2
    assert book.get_plan('LLM', target_session=FIRST) is None
    assert 'isolated-fixture-credential' not in json.dumps(result)
    for row in attempts(book):
        body, _ = read_business_document(book.root, row['response_document'])
        assert body == {} and row['reason'] == ('MODEL_TIMEOUT' if timeout else 'MODEL_CALL_FAILED')


@pytest.mark.parametrize('elapsed', [70, 121])
def test_interrupted_attempt_recovery_keeps_first_claim_deadline_and_fixed_input(strategy_world, elapsed):
    book, make_app, model = strategy_world
    original_clock = book.test_now[0]

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt('isolated process death after durable claim')

    with pytest.raises(KeyboardInterrupt):
        make_app(caller=interrupted).plan('LLM', FIRST)
    original = book.get_strategy_decision('LLM', target_session=FIRST)
    assert attempts(book)[0]['status'] == 'CLAIMED'
    assert datetime.fromisoformat(attempts(book)[0]['claimed_at']) == original_clock
    book.test_now[0] += timedelta(seconds=elapsed)
    budgets = []

    def recovered(client, messages, **kwargs):
        budgets.append(kwargs['timeout_seconds'])
        return synchronous_caller(client, messages, **kwargs)

    result = make_app(caller=recovered).recover('LLM', FIRST)
    rows = attempts(book)
    assert rows[0]['status'] == 'INTERRUPTED'
    current = book.get_strategy_decision('LLM', target_session=FIRST)
    assert current['input_hash'] == original['input_hash']
    assert current['input_document'] == original['input_document']
    if elapsed > 120:
        assert result['state'] == 'MODEL_FAILED', result
        assert len(rows) == 1 and not budgets and not model.calls
    else:
        assert result['state'] == 'FROZEN', result
        assert len(rows) == 2 and len(model.calls) == 1
        assert 47 < budgets[0] <= 50
        assert rows[1]['input_hash'] == rows[0]['input_hash']


def test_complete_execution_validation_failure_is_corrected_with_same_request(strategy_world):
    book, make_app, _ = strategy_world
    invalid = json.loads(response().text)
    # Valid model semantic fields still require the full public DTO/validator;
    # its shared short-reason bound rejects this overlong reason.
    invalid['ordered_intents'][0]['reason'] = '指标' * 129
    model = ControlledModel([ModelResult(json.dumps(invalid), 'isolated-model', 'stop'), response()])
    result = make_app(client=model).publish('LLM', FIRST)
    assert result['state'] == 'FROZEN', result
    rows = attempts(book)
    assert [row['status'] for row in rows] == ['INVALID', 'SUCCEEDED']
    assert rows[0]['input_hash'] == rows[1]['input_hash']
    first, second = [call['messages'] for call in model.calls]
    assert second[:2] == first and len(second) == 4
    assert second[2].content == model.responses[0].text
    errors = json.loads(second[3].content)['errors']
    assert errors and any(error.get('kind') == 'INVALID_INPUT' for error in errors)
    assert '意图理由' in json.dumps(errors, ensure_ascii=False)
    assert frozen(book, 'LLM')['orders'][0]['limit_price'] == '10.10'


@pytest.mark.parametrize('during_call', [False, True])
def test_model_configuration_change_stops_without_new_claim_or_reinterpreting_response(strategy_world, during_call):
    book, make_app, model = strategy_world
    app = make_app()
    prepared = app.prepare('LLM', FIRST)
    original = prepared['decision']
    if during_call:
        def change_configuration(client, messages, **kwargs):
            result = synchronous_caller(client, messages, **kwargs)
            app.model_settings = replace(app.model_settings, temperature=0.1)
            return result
        app.model_caller = change_configuration
    else:
        app.model_settings = replace(app.model_settings, model='different-model')
    result = app.publish('LLM', FIRST)
    assert result['state'] == 'CONFIG_CHANGED', result
    current = book.get_strategy_decision('LLM', target_session=FIRST)
    assert current['input_document'] == original['input_document']
    assert current['input_hash'] == original['input_hash']
    assert book.get_plan('LLM', target_session=FIRST) is None
    assert len(model.calls) == int(during_call)
    if during_call:
        assert attempts(book)[0]['status'] == 'RECEIVED'
        assert attempts(book)[0]['response_document']
    app.recover('LLM', FIRST)
    assert len(model.calls) == int(during_call)


def test_financial_change_during_model_call_saves_response_then_ends_stale(strategy_world):
    book, make_app, model = strategy_world
    old_revision = revision(book)

    def changed(client, messages, **kwargs):
        result = synchronous_caller(client, messages, **kwargs)
        # Exercise the same financial revision guard as any concurrent financial
        # commit, without inventing a fill or bypassing the real transaction layer.
        receipt = book._mutate('LLM', 'recovery-financial-interleave', {}, revision(book), FIRST,
            lambda tx: {'reason': 'isolated concurrent financial revision'}, financial=True)
        assert receipt['state'] == 'APPLIED'
        return result

    result = make_app(caller=changed).publish('LLM', FIRST)
    assert result['state'] == 'STALE_INPUT', result
    row = book.get_strategy_decision('LLM', target_session=FIRST)
    stored = attempts(book)[0]
    assert row['account_revision'] == stored['account_revision_at_claim'] == old_revision
    assert revision(book) == old_revision + 1
    assert stored['status'] == 'RECEIVED'
    body, _ = read_business_document(book.root, stored['response_document'])
    assert body['text'] == model.responses[0].text
    assert book.get_plan('LLM', target_session=FIRST) is None
    make_app().recover('LLM', FIRST)
    assert len(model.calls) == 1


def test_model_success_is_sanitized_in_response_core_and_reports_before_persisting(strategy_world):
    book, make_app, _ = strategy_world
    text = json.loads(response(intents=[]).text)
    text['no_order_reason'] = 'isolated-fixture-credential https://isolated.invalid/v1 暂无操作'
    model = ControlledModel([ModelResult(json.dumps(text), 'isolated-model', 'stop')])
    result = make_app(client=model).publish('LLM', FIRST)
    assert result['state'] == 'FROZEN', result
    row = attempts(book)[0]
    body, _ = read_business_document(book.root, row['response_document'])
    assert '[REDACTED]' in body['text']
    for folder in [book.root / 'data/plans', book.root / 'reports/strategies']:
        for path in folder.rglob('*.json'):
            content = path.read_text()
            assert 'isolated-fixture-credential' not in content
            assert 'isolated.invalid' not in content
    assert b'isolated-fixture-credential' not in book.settings.database.read_bytes()
    assert len(model.calls) == 1


def test_proposed_success_recovery_only_freezes_original_core_without_requestion(strategy_world):
    book, make_app, model = strategy_world
    assert make_app().plan('LLM', FIRST)['state'] == 'PROPOSED'
    original = book.get_strategy_decision('LLM', target_session=FIRST)
    stored = attempts(book)[0]
    assert stored['status'] == 'SUCCEEDED'
    core_bytes = (book.root / original['decision_core']['path']).read_bytes()
    result = make_app().recover('LLM', FIRST)
    assert result['state'] == 'FROZEN', result
    assert len(model.calls) == 1 and len(attempts(book)) == 1
    assert attempts(book)[0]['response_document'] == stored['response_document']
    assert frozen(book, 'LLM')['decision_ref'] == original['decision_ref']
    assert (book.root / original['decision_core']['path']).read_bytes() == core_bytes
