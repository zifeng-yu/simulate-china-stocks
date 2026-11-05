"""Independent strategy acceptance through real isolated market/ledger services.

All source records, accounts, plans, fills and reports live in pytest temporary
projects. Only the model response/caller is controlled; no production data or
credentials are loaded and no network is permitted.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event

import pytest
from execution_fixtures import (
    CODES,
    FIRST,
    PREVIOUS,
    advance,
    execution_book,
    set_pool,
    source,
)
from test_ledger_trading import call, revision

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.config import load_settings
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.store import read_snapshot
from china_stock_sim.market.events import normalize_eastmoney_events
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.models.base import ModelResult
from china_stock_sim.models.settings import ModelSettings
from china_stock_sim.reporting.ledger import write_ledger_report

assert execution_book


def tree_state(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() else 'directory' for path in root.rglob('*')}


def response(target=FIRST, *, intents=None):
    if intents is None:
        intents = [{'code': CODES[0], 'side': 'BUY', 'reason': '比较已提供的20日变动',
                    'basis_fields': ['ret_20d'], 'order_index': 0,
                    'limit_price': '10.10', 'budget_fen': 1_000_000}]
    return ModelResult(json.dumps({'target_session': target, 'ordered_intents': intents,
                                  'no_order_reason': '没有新增交易依据' if not intents else None},
                                 ensure_ascii=False), 'isolated-model', 'stop', {'total_tokens': 30})


class ControlledModel:
    def __init__(self, responses=None):
        self.responses = list(responses or [response()])
        self.calls = []

    def complete(self, messages, **options):
        self.calls.append({'messages': messages, 'options': options})
        assert options['json_mode'] is True
        assert options['max_output_tokens'] == 2048
        assert len(self.calls) <= len(self.responses), 'unexpected model re-question'
        return self.responses[len(self.calls) - 1]


def synchronous_caller(client, messages, *, timeout_seconds, max_output_tokens=2048,
                       json_mode=True):
    assert 0 < timeout_seconds <= 120
    return client.complete(messages, max_output_tokens=max_output_tokens, json_mode=json_mode)


@pytest.fixture
def strategy_world(execution_book):
    from china_stock_sim.application.strategy import StrategyApplication

    book = execution_book
    assert call(book, 'activate_simulation', {'target_session': FIRST}, account='S1-LT')['state'] == 'APPLIED'
    model = ControlledModel()
    model_settings = ModelSettings('openai_compatible', 'isolated-model',
        'https://isolated.invalid/v1', 'isolated-fixture-credential', 120, 2048, 0.0, {})

    def app(*, client=model, caller=synchronous_caller):
        return StrategyApplication(book.settings, market=book.market, ledger=book,
            clock=lambda: book.test_now[0], test_mode=True, model_client=client,
            model_settings=model_settings, model_caller=caller)

    return book, app, model


def frozen(book, account, target=FIRST):
    plan = book.get_plan(account, target_session=target)
    assert plan is not None and plan['provenance_mode'] == 'STRATEGY'
    decision = book.get_strategy_decision(account, target_session=target)
    assert decision['state'] == 'FROZEN' and decision['plan_id'] == plan['plan_id']
    assert plan['decision_ref'] == decision['decision_ref']
    return plan


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_three_strategies_public_prepare_plan_publish_settle_close_and_html(strategy_world, account):
    book, make_app, model = strategy_world
    app = make_app()
    before = tree_state(book.root)
    inspect = app.inspect(account, FIRST)
    assert inspect['state'] == 'READY', inspect
    assert tree_state(book.root) == before
    prepared = app.prepare(account, FIRST)
    decision = book.get_strategy_decision(account, target_session=FIRST)
    assert decision['state'] == 'PREPARED', prepared
    original_revision = revision(book, account)
    planned = app.plan(account, FIRST)
    proposed = book.get_strategy_decision(account, target_session=FIRST)
    assert proposed['state'] == 'PROPOSED', planned
    assert book.get_plan(account, target_session=FIRST) is None
    assert revision(book, account) == original_revision
    assert book.positions(account, FIRST) == []
    assert len(model.calls) == (1 if account == 'LLM' else 0)
    original_core = (book.root / proposed['decision_core']['path']).read_bytes()
    app.plan(account, FIRST)
    assert len(model.calls) == (1 if account == 'LLM' else 0)
    published = app.publish(account, FIRST)
    plan = frozen(book, account)
    assert plan['orders'], published
    assert plan['strategy_data_evidence'] and plan['data_evidence']
    assert (book.root / proposed['decision_core']['path']).read_bytes() == original_core
    before_recover = book.settings.database.read_bytes()
    app.recover(account, FIRST)
    assert book.settings.database.read_bytes() == before_recover
    assert len(model.calls) == (1 if account == 'LLM' else 0)

    advance(book, FIRST)
    execution = ExecutionApplication(book.settings, market=book.market, ledger=book,
                                    clock=lambda: book.test_now[0], test_mode=True)
    settled = execution.settle(account, plan['plan_id'], operation_id='isolated-strategy-settle')
    assert settled['completion_status'] == 'COMPLETE', settled
    fills = book.fills(account)['rows']
    assert len(fills) == len(plan['orders'])
    close = call(book, 'close_session', {'session': FIRST,
        'knowledge_cutoff': book._now().isoformat()}, account=account)
    assert close['state'] == 'APPLIED', close
    statement = book.daily_statement(account, FIRST)
    assert statement['accounting_status'] == 'COMPLETE' and statement['valuation_status'] == 'FINAL'
    assert statement['net_assets_fen'] == 5_000_000 - sum(fill['fees_fen'] for fill in fills)
    output = write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    assert output['published'] and book.settings.report.is_file()
    assert account in book.settings.report.read_text()
    export = json.loads((book.root / f'reports/strategies/{account}-audit.json').read_text())
    assert export['account_id'] == account and export['plans']
    for other in {'S1', 'S1-LT', 'LLM'} - {account}:
        assert book.get_account_profile(other)['cash_fen'] == 5_000_000
        assert book.fills(other)['total'] == 0


def test_month_end_uses_next_pool_and_never_requests_future_prices(strategy_world):
    book, make_app, _ = strategy_world
    advance(book, FIRST)
    set_pool(book, FIRST, '2026-11-02', [CODES[1]])
    opened = book.create_account({'account_id': 'NEXT', 'strategy_id': 'S1',
        'initial_cash_fen': 123_456, 'strategy_parameters': {'target_count': 1}}, operation_id='next-account')
    assert opened['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': '2026-11-02'}, account='NEXT')['state'] == 'APPLIED'
    inspected = make_app().inspect('NEXT', '2026-11-02', include_input=True)
    assert inspected['state'] == 'READY', inspected
    bundle = inspected['input_bundle']
    assert bundle['context']['market_as_of'] == FIRST
    assert {code for code, item in bundle['context']['securities'].items() if item['in_pool']} == {CODES[1]}
    assert {item['code'] for item in book.market.pool(FIRST).data['selected']} == set(CODES)
    for query in bundle['data_evidence']:
        if query['method'] == 'prices':
            assert query['kwargs']['end'] == FIRST
    assert book.market.prices(CODES[1], '2026-11-02', '2026-11-02',
        knowledge_cutoff=book._now().isoformat()).state == 'WAITING_DATA'


def test_custom_history_requirement_is_retained_on_each_public_input_assembly(strategy_world):
    from china_stock_sim.application.strategy_inputs import collection_requirements

    book, make_app, _ = strategy_world
    assert book.create_account({'account_id': 'LONG', 'strategy_id': 'S1',
        'initial_cash_fen': 1_234_567, 'strategy_parameters': {'lookback_sessions': 130}},
        operation_id='long-account')['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': FIRST}, account='LONG')['state'] == 'APPLIED'
    inspected = make_app().inspect('LONG', FIRST, include_input=True)
    assert inspected['state'] == 'READY', inspected
    bundle = inspected['input_bundle']
    assert bundle['required_history_sessions'] == 130
    assert any(q['method'] == 'signal_prices' and q['kwargs']['size'] == 130
               for q in bundle['data_evidence'])
    for target in [FIRST, '2026-11-02']:
        assert collection_requirements(book, ['LLM', 'S1', 'LONG'], target)['history_sessions'] == 130


def test_two_invalid_model_responses_are_terminal_and_other_account_can_publish(strategy_world):
    book, make_app, _ = strategy_world
    model = ControlledModel([ModelResult('{}', 'isolated-model', 'stop')] * 2)
    app = make_app(client=model)
    app.publish('LLM', FIRST)
    decision = book.get_strategy_decision('LLM', target_session=FIRST)
    assert decision['state'] == 'MODEL_FAILED'
    assert len(model.calls) == 2 and book.get_plan('LLM', target_session=FIRST) is None
    assert [x['status'] for x in book.report_snapshot('LLM', FIRST)['model_attempts']] == ['INVALID', 'INVALID']
    app.recover('LLM', FIRST)
    assert len(model.calls) == 2
    app.publish('S1', FIRST)
    assert frozen(book, 'S1')['orders']


def test_received_response_survives_admission_failure_without_requestion(strategy_world, monkeypatch):
    book, make_app, model = strategy_world
    original = book.save_strategy_decision
    failed = []

    def interrupted(account_id, payload, **kwargs):
        if payload['state'] == 'PROPOSED' and not failed:
            failed.append(True)
            return {'state': 'IO_ERROR', 'reason': 'isolated admission interruption'}
        return original(account_id, payload, **kwargs)

    monkeypatch.setattr(book, 'save_strategy_decision', interrupted)
    app = make_app()
    app.plan('LLM', FIRST)
    assert len(model.calls) == 1
    assert book.report_snapshot('LLM', FIRST)['model_attempts'][0]['status'] == 'RECEIVED'
    monkeypatch.setattr(book, 'save_strategy_decision', original)
    app.recover('LLM', FIRST)
    assert len(model.calls) == 1
    decision = book.get_strategy_decision('LLM', target_session=FIRST)
    assert decision['state'] in {'PROPOSED', 'FROZEN'}
    app.publish('LLM', FIRST)
    frozen(book, 'LLM')
    assert len(model.calls) == 1


def test_same_account_concurrent_planners_claim_only_once(strategy_world):
    book, make_app, model = strategy_world
    entered, release = Event(), Event()

    def controlled(client, messages, **kwargs):
        entered.set()
        assert release.wait(5), 'test failed to release controlled model'
        return synchronous_caller(client, messages, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(make_app(caller=controlled).plan, 'LLM', FIRST)
        try:
            assert entered.wait(5), 'first planner failed before model call'
            second = make_app().plan('LLM', FIRST)
            assert second['state'] == 'ALREADY_RUNNING', second
        finally:
            release.set()
        first.result(timeout=5)
    assert len(model.calls) == 1
    assert len(book.report_snapshot('LLM', FIRST)['model_attempts']) == 1


def test_late_model_response_is_audited_but_never_frozen(strategy_world):
    book, make_app, model = strategy_world

    def late(client, messages, **kwargs):
        result = synchronous_caller(client, messages, **kwargs)
        book.test_now[0] = datetime.fromisoformat(FIRST + 'T09:00:00+08:00')
        return result

    make_app(caller=late).publish('LLM', FIRST)
    assert book.get_plan('LLM', target_session=FIRST) is None
    attempts = book.report_snapshot('LLM', FIRST)['model_attempts']
    assert len(attempts) == 1 and attempts[0]['response_document']
    assert book.get_strategy_decision('LLM', target_session=FIRST)['state'] == 'MISSED_CUTOFF'
    assert len(model.calls) == 1


@pytest.mark.parametrize('target,clock', [
    ('2026-10-31', '2026-10-29T20:00:00+08:00'),
    (FIRST, FIRST + 'T09:00:00+08:00'),
])
def test_nontrading_or_cutoff_targets_do_not_claim_or_write(strategy_world, target, clock):
    book, make_app, model = strategy_world
    book.test_now[0] = datetime.fromisoformat(clock)
    before = tree_state(book.root)
    inspected = make_app().inspect('LLM', target)
    assert inspected['state'] in {'INVALID_INPUT', 'MISSED_CUTOFF'}, inspected
    assert tree_state(book.root) == before
    assert not model.calls


@pytest.mark.parametrize('ex_date,progress,expected', [
    ('2026-11-10', '实施分配', 'EXCLUDED'),
    (None, '董事会预案', 'EXCLUDED'),
    ('2026-01-05', '实施分配', 'READY'),
    ('2026-11-10', '取消分配', 'READY'),
])
def test_public_inputs_distinguish_future_historical_and_cancelled_complex_events(
        strategy_world, ex_date, progress, expected):
    book, make_app, model = strategy_world
    raw = {'SECURITY_CODE': '600000', 'SECUCODE': '600000.SH',
           'REPORT_DATE': '2025-12-31', 'PLAN_NOTICE_DATE': '2025-12-31',
           'NOTICE_DATE': '2026-01-01', 'EX_DIVIDEND_DATE': ex_date,
           'EQUITY_RECORD_DATE': None, 'PRETAX_BONUS_RMB': None,
           'BONUS_RATIO': None, 'IT_RATIO': None,
           'IMPL_PLAN_PROFILE': '吸收合并方案', 'ASSIGN_PROGRESS': progress}
    at = PREVIOUS + 'T18:20:00+08:00'
    event = normalize_eastmoney_events([raw], CODES[0], at)[0]
    assert event['kind'] == 'COMPLEX'
    if progress == '取消分配':
        assert event['status'] == 'CANCELLED'
    MarketRepository(book.root).put('event', [event], source([raw], at, 'eastmoney'))
    if ex_date is None:
        signal = book.market.signal_prices(CODES[0], PREVIOUS, size=121,
                                          knowledge_cutoff=book._now().isoformat())
        assert signal.state == 'WAITING_DATA' and signal.data == []
        assert signal.issues == ({'kind': 'UNDATED_ACTION'},)
        assert signal.observation_ids
    before = tree_state(book.root)
    inspected = make_app().inspect('LLM', FIRST, include_input=True)
    assert inspected['state'] == 'READY', inspected
    bundle = inspected['input_bundle']
    assert bundle['context']['securities'][CODES[0]]['eligibility'] == expected
    assert (CODES[0] in bundle['planning_request']['buy_candidates']) == (expected == 'READY')
    assert tree_state(book.root) == before and not model.calls
    published = make_app().publish('S1', FIRST)
    assert published['state'] == 'FROZEN', json.dumps({
        'state': published['state'], 'reasons': published.get('reasons'),
        'validation': published.get('validation', {}).get('reasons')}, ensure_ascii=False)
    plan = frozen(book, 'S1')
    assert (CODES[0] in {o['code'] for o in plan['orders']}) == (expected == 'READY')
    assert CODES[1] in {o['code'] for o in plan['orders']}


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_real_holdings_leaving_pool_are_preserved_in_input_and_can_exit(strategy_world, account):
    book, make_app, model = strategy_world
    make_app().publish(account, FIRST)
    plan = frozen(book, account)
    advance(book, FIRST)
    executor = ExecutionApplication(book.settings, market=book.market, ledger=book,
                                    clock=lambda: book.test_now[0], test_mode=True)
    assert executor.settle(account, plan['plan_id'], operation_id='held-source-fill')[
        'completion_status'] == 'COMPLETE'
    assert call(book, 'close_session', {'session': FIRST,
        'knowledge_cutoff': book._now().isoformat()}, account=account)['state'] == 'APPLIED'
    target = '2026-11-02'
    set_pool(book, FIRST, target, [CODES[1]])
    inspected = make_app().inspect(account, target, include_input=True)
    assert inspected['state'] == 'READY', inspected
    bundle = inspected['input_bundle']
    held = bundle['context']['securities'][CODES[0]]
    assert held['in_pool'] is False and held['eligibility'] == 'EXCLUDED'
    assert len(held['adjusted_close']) in {21, 121}
    if account == 'LLM':
        request = bundle['planning_request']
        entry = next(row for row in request['securities'] if row['code'] == CODES[0])
        assert entry['is_holding'] and entry['allowed_actions'] == ['SELL', 'HOLD']
        assert CODES[0] not in request['buy_candidates']
        model.responses.append(response(target, intents=[{
            'code': CODES[0], 'side': 'SELL', 'reason': '已出池持仓退出',
            'basis_fields': ['quantity'], 'order_index': 0,
            'limit_price': '9.90', 'sell_fraction': '1'}]))
    result = make_app().publish(account, target)
    assert result['state'] == 'FROZEN', result
    second = frozen(book, account, target)
    assert any(o['code'] == CODES[0] and o['side'] == 'SELL' for o in second['orders']), result
    assert not any(o['code'] == CODES[0] and o['side'] == 'BUY' for o in second['orders'])
    assert second['strategy_data_evidence']


def test_dynamic_account_and_real_model_decision_backup_preserve_full_audit_chain(
        strategy_world, tmp_path):
    book, make_app, model = strategy_world
    opened = book.create_account({'account_id': 'SAVINGS', 'display_name': '独立小本金',
        'strategy_id': 'S1', 'initial_cash_fen': 1_234_567,
        'strategy_parameters': {'target_count': 1}}, operation_id='savings-open')
    assert opened['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': FIRST},
                account='SAVINGS')['state'] == 'APPLIED'
    for account in ['SAVINGS', 'LLM']:
        make_app().publish(account, FIRST)
        frozen(book, account)
    before = {account: book.get_strategy_decision(account, target_session=FIRST)
              for account in ['SAVINGS', 'LLM']}
    attempts = book.report_snapshot('LLM', FIRST)['model_attempts']
    assert len(attempts) == 1 and attempts[0]['status'] == 'SUCCEEDED'
    paths = {ref['path'] for row in before.values()
             for ref in [row['document'], row['input_document'], row['decision_core']]}
    paths |= {attempts[0]['request_document']['path'], attempts[0]['response_document']['path']}
    policy = book.get_account_policy('SAVINGS', FIRST)
    paths.add(policy['document']['path'])
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE', backup['evidence']['issues']
    assert paths <= {item['path'] for item in backup['evidence']['files']}
    assert backup['balances_fen']['SAVINGS'] == 1_234_567
    expected = {path: (book.root / path).read_bytes() for path in paths}
    for path in paths:
        (book.root / path).unlink()
    target = tmp_path / 'independent-recovery'
    restored = book.restore_backup(backup['backup_id'], target)
    # Ledger backup carries every referenced original, not an independent market query index.
    assert restored['state'] == 'RESTORED_AUDIT_PARTIAL' and restored['market_archive_required']
    assert all((target / path).read_bytes() == value for path, value in expected.items())
    recovered = LedgerService(load_settings(target))
    assert recovered.get_account_profile('SAVINGS')['cash_fen'] == 1_234_567
    assert recovered.get_account_policy('SAVINGS', FIRST)['policy'] == policy['policy']
    for account, row in before.items():
        assert recovered.get_strategy_decision(account, target_session=FIRST) == row
        assert recovered.get_plan(account, target_session=FIRST)['decision_ref'] == row['decision_ref']
    assert len(model.calls) == 1
