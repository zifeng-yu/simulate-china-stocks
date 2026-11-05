"""Real temporary-ledger recovery around rule decisions and application locks."""

import multiprocessing
import os
from datetime import datetime

import pytest
from execution_fixtures import CODES, FIRST, PREVIOUS, advance, execution_book, set_quote
from test_ledger_trading import call, revision
from test_strategy_acceptance import strategy_world, synchronous_caller

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.application.strategy import planning_lock
from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.ledger.kernel import LedgerError
from china_stock_sim.market.contracts import DataResult

assert execution_book and strategy_world


def _process_planner(make_app, results, entered=None, release=None):
    """Forked isolated planners share only persistent fixtures and explicit IPC."""
    if entered is None:
        app = make_app()
    else:
        def controlled(client, messages, **kwargs):
            entered.set()  # claim is already committed before the model boundary
            if not release.wait(20):
                raise TimeoutError('isolated test did not release first planner')
            return synchronous_caller(client, messages, **kwargs)
        app = make_app(caller=controlled)
    result = app.plan('LLM', FIRST)
    results.put({'pid': os.getpid(), 'state': result['state']})


def financial_revision(book, account):
    """An isolated confirmed cash correction exercises revision causality, not fake input."""
    result = book._mutate(account, 'isolated-cash-correction', {'amount_fen': 100},
        revision(book, account), FIRST,
        lambda tx: tx.cash('isolated-correction', 'TEST_ADJUSTMENT', 100, FIRST))
    assert result['state'] == 'APPLIED', result


@pytest.mark.parametrize('changed_revision', [False, True])
def test_waiting_input_at_exact_cutoff_is_durable_terminal(strategy_world, monkeypatch,
                                                          changed_revision):
    book, make_app, model = strategy_world
    original = book.market.signal_prices
    monkeypatch.setattr(book.market, 'signal_prices', lambda *a, **k:
                        DataResult('MISSING', [], ({'kind': 'ISOLATED_MISSING'},)))
    prepared = make_app().prepare('S1', FIRST)
    assert prepared['state'] == 'WAITING_INPUT', prepared
    prior = book.get_strategy_decision('S1', target_session=FIRST)
    if changed_revision:
        financial_revision(book, 'S1')
    monkeypatch.setattr(book.market, 'signal_prices', original)
    advance(book, FIRST, '09:00:00')
    recovered = make_app().recover('S1', FIRST)
    assert recovered['state'] == 'MISSED_CUTOFF', recovered
    row = book.get_strategy_decision('S1', target_session=FIRST)
    assert row['state'] == 'MISSED_CUTOFF' and row['sequence'] > prior['sequence']
    assert book.get_plan('S1', target_session=FIRST) is None and not model.calls
    before = book.settings.database.read_bytes()
    assert make_app().recover('S1', FIRST)['state'] == 'MISSED_CUTOFF'
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('account', ['S1', 'LLM'])
def test_unclaimed_prepared_financial_change_reprepares_with_immutable_history(strategy_world,
                                                                             account):
    book, make_app, model = strategy_world
    assert make_app().prepare(account, FIRST)['state'] == 'PREPARED'
    original = book.get_strategy_decision(account, target_session=FIRST)
    original_bytes = (book.root / original['input_document']['path']).read_bytes()
    financial_revision(book, account)
    advance(book, PREVIOUS, '20:01:00')
    prepared = make_app().prepare(account, FIRST)
    assert prepared['state'] == 'PREPARED', prepared
    current = book.get_strategy_decision(account, target_session=FIRST)
    assert current['account_revision'] == revision(book, account)
    assert current['sequence'] == original['sequence'] + 1
    assert current['input_hash'] != original['input_hash']
    assert (book.root / original['input_document']['path']).read_bytes() == original_bytes
    assert current['history'][0] == original['history'][0]
    assert not book.report_snapshot(account, FIRST)['model_attempts'] and not model.calls


def test_saved_parent_crash_recovers_same_input_targets_and_single_parent(strategy_world,
                                                                        monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    original = app._proposal
    monkeypatch.setattr(app, '_proposal', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('isolated crash after parent commit')))
    interrupted = app.plan('S1', FIRST)
    assert interrupted['state'] == 'INVALID_INPUT', interrupted
    prepared = book.get_strategy_decision('S1', target_session=FIRST)
    assert prepared['state'] == 'PREPARED'
    parents = book.report_snapshot('S1', FIRST)['rebalance_tasks']
    assert len(parents) == 1 and parents[0]['decision_id'] == prepared['decision_id']
    before_parent = parents[0]
    for code in CODES:
        set_quote(book, code, PREVIOUS, '10.50', fetched=PREVIOUS + 'T20:01:00+08:00')
    advance(book, PREVIOUS, '20:02:00')
    monkeypatch.setattr(app, '_proposal', original)
    recovered = app.recover('S1', FIRST)
    assert recovered['state'] == 'FROZEN', recovered
    row = book.get_strategy_decision('S1', target_session=FIRST)
    assert row['input_hash'] == prepared['input_hash']
    parents = book.report_snapshot('S1', FIRST)['rebalance_tasks']
    assert len(parents) == 1 and parents[0]['targets'] == before_parent['targets']
    assert parents[0]['task_id'] == before_parent['task_id']
    assert all(order['limit_price'] == '10.10' for order in book.get_plan(
        'S1', target_session=FIRST)['orders'])


def test_saved_parent_financial_change_never_retargets(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    monkeypatch.setattr(app, '_proposal', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash')))
    app.plan('S1', FIRST)
    original = book.get_strategy_decision('S1', target_session=FIRST)
    parents = book.report_snapshot('S1', FIRST)['rebalance_tasks']
    assert len(parents) == 1
    financial_revision(book, 'S1')
    result = make_app().recover('S1', FIRST)
    assert result['state'] == 'STALE_INPUT', result
    current = book.get_strategy_decision('S1', target_session=FIRST)
    assert current['input_hash'] == original['input_hash']
    assert book.report_snapshot('S1', FIRST)['rebalance_tasks'][0]['targets'] == parents[0]['targets']
    assert book.get_plan('S1', target_session=FIRST) is None


def test_waiting_validation_invalid_recheck_is_persisted_terminal(strategy_world, monkeypatch):
    book, make_app, model = strategy_world
    app = make_app()
    validations = []

    def validate(proposal):
        validations.append(proposal)
        return {'status': 'WAITING_VALIDATION' if len(validations) == 1 else 'INVALID',
                'issues': [{'kind': 'ISOLATED_PENDING' if len(validations) == 1
                            else 'ISOLATED_INVALID'}]}

    monkeypatch.setattr(app.execution, 'validate', validate)
    waiting = app.plan('S1', FIRST)
    assert waiting['state'] == 'WAITING_VALIDATION', waiting
    original = book.get_strategy_decision('S1', target_session=FIRST)
    assert original['state'] == 'WAITING_VALIDATION' and original['proposal']
    original_bytes = (book.root / original['decision_core']['path']).read_bytes()
    outcome = app.recover('S1', FIRST)
    assert outcome['state'] == 'INVALID', outcome
    terminal = book.get_strategy_decision('S1', target_session=FIRST)
    assert terminal['state'] == 'INVALID'
    assert terminal['sequence'] == original['sequence'] + 1
    assert terminal['reasons'] == [{'kind': 'ISOLATED_INVALID'}]
    assert terminal['proposal'] == original['proposal']
    assert validations[0] == validations[1] == original['proposal']
    assert (book.root / original['decision_core']['path']).read_bytes() == original_bytes
    assert book.get_plan('S1', target_session=FIRST) is None and not model.calls
    before = book.settings.database.read_bytes()
    assert app.recover('S1', FIRST)['state'] == 'INVALID'
    assert book.settings.database.read_bytes() == before and len(validations) == 2


def test_waiting_input_changed_implementation_is_persisted_terminal(strategy_world, monkeypatch):
    book, make_app, model = strategy_world
    original_signal = book.market.signal_prices
    monkeypatch.setattr(book.market, 'signal_prices', lambda *a, **k:
                        DataResult('MISSING', [], ({'kind': 'ISOLATED_MISSING'},)))
    assert make_app().prepare('S1', FIRST)['state'] == 'WAITING_INPUT'
    original = book.get_strategy_decision('S1', target_session=FIRST)
    original_bytes = (book.root / original['input_document']['path']).read_bytes()
    monkeypatch.setattr(book.market, 'signal_prices', original_signal)
    # Change the registered implementation identity without touching any source file.
    monkeypatch.setitem(STRATEGY_DEFINITIONS['S1'], 'implementation_hash', 'a' * 64)
    changed = make_app().recover('S1', FIRST)
    assert changed['state'] == 'CONFIG_CHANGED', changed
    terminal = book.get_strategy_decision('S1', target_session=FIRST)
    assert terminal['state'] == 'CONFIG_CHANGED'
    assert terminal['sequence'] == original['sequence'] + 1
    assert terminal['input_hash'] == original['input_hash']
    assert (book.root / original['input_document']['path']).read_bytes() == original_bytes
    assert book.get_plan('S1', target_session=FIRST) is None and not model.calls
    before = book.settings.database.read_bytes()
    assert make_app().recover('S1', FIRST)['state'] == 'CONFIG_CHANGED'
    assert book.settings.database.read_bytes() == before


def test_empty_initial_evaluation_persists_without_empty_parent_and_does_not_repeat(strategy_world):
    book, make_app, _ = strategy_world
    # November 2 follows a month end. The next target is an ordinary session.
    advance(book, FIRST)
    assert book.create_account({'account_id': 'POOR', 'strategy_id': 'S1',
        'initial_cash_fen': 100}, operation_id='poor-account')['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': '2026-11-02'},
                account='POOR')['state'] == 'APPLIED'
    result = make_app().publish('POOR', '2026-11-02')
    assert result['state'] == 'FROZEN', result
    first = book.get_strategy_decision('POOR', target_session='2026-11-02')
    core, _ = read_business_document(book.root, first['decision_core'])
    assert core['calculation']['initial_evaluated'] is True
    assert not book.report_snapshot('POOR', '2026-11-02')['rebalance_tasks']
    advance(book, '2026-11-02')
    assert call(book, 'close_session', {'session': '2026-11-02',
        'knowledge_cutoff': book._now().isoformat()}, account='POOR')['state'] == 'APPLIED'
    result = make_app().publish('POOR', '2026-11-03')
    assert result['state'] == 'FROZEN', result
    next_plan = book.get_plan('POOR', target_session='2026-11-03')
    assert next_plan['no_order_reason'] == 'NON_REBALANCE_DAY'
    assert not book.report_snapshot('POOR', '2026-11-03')['rebalance_tasks']


def test_lock_is_per_account_target_and_releases_after_failure(tmp_path):
    with pytest.raises(RuntimeError, match='crash'):
        with planning_lock(tmp_path, 'S1', FIRST):
            with pytest.raises(LedgerError, match='已有规划进程'):
                with planning_lock(tmp_path, 'S1', FIRST):
                    pytest.fail('same target acquired twice')
            with planning_lock(tmp_path, 'S1-LT', FIRST):
                with planning_lock(tmp_path, 'S1', '2026-11-02'):
                    pass
            raise RuntimeError('crash')
    with planning_lock(tmp_path, 'S1', FIRST):
        assert datetime.fromisoformat(FIRST).day == 30


def test_two_planner_processes_share_real_lock_and_only_one_persisted_claim(strategy_world):
    book, make_app, _ = strategy_world
    # Fork inherits only the isolated test clock and fake model; socket blocking
    # remains in force in both processes. Neither process uses production state.
    context = multiprocessing.get_context('fork')
    entered, release = context.Event(), context.Event()
    results = context.Queue()
    first = context.Process(target=_process_planner, args=(make_app, results, entered, release))
    second = context.Process(target=_process_planner, args=(make_app, results))
    first.start()
    try:
        assert entered.wait(15), 'first planner did not reach persisted model claim'
        second.start()
        rejected = results.get(timeout=10)
        assert rejected['state'] == 'ALREADY_RUNNING', rejected
        second.join(10)
        assert second.exitcode == 0
        release.set()
        completed = results.get(timeout=15)
        first.join(10)
        assert first.exitcode == 0 and completed['state'] == 'PROPOSED', completed
        assert rejected['pid'] != completed['pid'] != os.getpid()
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None:
                process.join(2)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        results.close()
        results.join_thread()
    attempts = book.report_snapshot('LLM', FIRST)['model_attempts']
    assert len(attempts) == 1 and attempts[0]['status'] == 'SUCCEEDED'
    assert book.get_strategy_decision('LLM', target_session=FIRST)['state'] == 'PROPOSED'
    assert book.get_plan('LLM', target_session=FIRST) is None


@pytest.mark.parametrize('filled', [True, False])
def test_children_use_actual_settlement_and_keep_month_end_parent(strategy_world, filled, monkeypatch):
    from copy import deepcopy

    from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS

    # This recovery scenario intentionally exercises a historical one-code child
    # policy. Current rule policies use 0 and are tested by the NL regressions.
    historical = deepcopy(STRATEGY_DEFINITIONS['S1'])
    historical['defaults']['max_daily_symbols'] = 5
    historical['strategy_version'] = 'S1-implementation-1'
    monkeypatch.setitem(STRATEGY_DEFINITIONS, 'S1', historical)
    book, make_app, _ = strategy_world
    assert book.create_account({'account_id': 'DAILYONE', 'strategy_id': 'S1',
        'initial_cash_fen': 5_000_000, 'strategy_parameters': {
            'target_count': 2, 'max_daily_symbols': 1}},
        operation_id='single-daily-order')['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': FIRST},
                account='DAILYONE')['state'] == 'APPLIED'
    initial = make_app().publish('DAILYONE', FIRST)
    assert initial['state'] == 'FROZEN', initial
    original = book.get_plan('DAILYONE', target_session=FIRST)
    assert len(original['orders']) == 1
    parent = book.report_snapshot('DAILYONE', FIRST)['rebalance_tasks'][0]
    assert len(parent['targets']) == 2
    first_code = original['orders'][0]['code']
    if not filled:
        # A future session's limit-up quote is unavailable to the first plan.
        set_quote(book, first_code, FIRST, '11.00', preclose='10.00')
    advance(book, FIRST)
    execution = ExecutionApplication(book.settings, market=book.market, ledger=book,
                                     clock=lambda: book.test_now[0], test_mode=True)
    settled = execution.settle('DAILYONE', original['plan_id'], operation_id='settle-first-child')
    assert settled['completion_status'] == 'COMPLETE', settled
    assert book.fills('DAILYONE')['total'] == int(filled)
    assert call(book, 'close_session', {'session': FIRST,
        'knowledge_cutoff': book._now().isoformat()}, account='DAILYONE')['state'] == 'APPLIED'
    second = make_app().publish('DAILYONE', '2026-11-02')
    assert second['state'] == 'FROZEN', second
    child = book.get_plan('DAILYONE', target_session='2026-11-02')
    assert len(child['orders']) == 1
    assert (child['orders'][0]['code'] != first_code) is filled
    assert child['task_id'] == parent['task_id']
    after = book.report_snapshot('DAILYONE', '2026-11-02')['rebalance_tasks']
    assert len(after) == 1 and after[0]['targets'] == parent['targets']
    expected = original['orders'][0]['quantity'] if filled else 0
    assert after[0]['completed_quantities'].get(f'{first_code}:BUY', 0) == expected
    decision = book.get_strategy_decision('DAILYONE', target_session='2026-11-02')
    core, _ = read_business_document(book.root, decision['decision_core'])
    assert core['calculation']['trigger'] == 'MONTH_END_COALESCED'
