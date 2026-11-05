"""SL02 independent acceptance for CAS, immutable evidence and atomic strategy freeze."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Barrier, Event

import pytest
from execution_fixtures import FIRST, execution_book
from test_execution_decision_audit import application, publish, register_proposed, strategy_request
from test_ledger_trading import DAY, Facts, book, call, plan_payload, revision

from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.kernel import LedgerError, canonical, ledger_lock

assert book and execution_book


def prepare(book, *, account='LLM', sequence=0, marker='initial', state='PREPARED', op=None,
            cutoff=None, target=DAY):
    return call(book, 'prepare_strategy_decision', {
        'target_session': target,
        'market_as_of': book.market.calendar_operation('previous_session', target).data,
        'input_cutoff': cutoff or book._now().isoformat(), 'expected_sequence': sequence,
        'state': state,
        'input_bundle': {'market_snapshot_ids': [], 'data_evidence': [],
                         'account_revision': revision(book, account), 'marker': marker},
    }, account=account, op=op)


def save(book, row, state, **extra):
    return call(book, 'save_strategy_decision', {
        'decision_id': row['decision_id'], 'target_session': row['target_session'],
        'expected_sequence': row['sequence'], 'state': state, **extra,
    }, account=row['account_id'])


def current(book, account='LLM', target=DAY):
    return book.get_strategy_decision(account, target_session=target)


def rewrite_index(book, row):
    """Simulate on-disk corruption; never used as an implementation shortcut."""
    import sqlite3

    with sqlite3.connect(book.settings.database) as connection:
        connection.execute('UPDATE strategy_decisions SET data=? WHERE account_id=? AND id=?',
                           (canonical(row), row['account_id'], row['decision_id']))


def test_decision_unique_target_compare_and_swap_and_nonfinancial_revision(book):
    baseline = revision(book)
    first = prepare(book, op='prepare-once')
    assert first['state'] == 'APPLIED' and first['sequence'] == 1
    assert prepare(book, op='prepare-once')['state'] == 'ALREADY_APPLIED'
    assert prepare(book, sequence=0, marker='racing process')['state'] == 'CONFLICT'
    assert prepare(book, sequence=True)['state'] == 'CONFLICT'
    second = prepare(book, sequence=1, marker='new complete input')
    assert second['state'] == 'APPLIED' and second['sequence'] == 2
    assert second['decision_id'] == first['decision_id']
    assert second['input_hash'] != first['input_hash']
    assert revision(book) == baseline
    rows = book.list_strategy_decisions('LLM')
    assert len(rows) == 1 and len(rows[0]['history']) == 2
    for event in rows[0]['history']:
        body = json.loads((book.root / event['document']['path']).read_text())
        assert body['sequence'] == event['sequence']


def test_waiting_input_can_be_reprepared_but_old_sequence_cannot_overwrite(book):
    waiting = prepare(book, state='WAITING_INPUT')
    assert waiting['state'] == 'APPLIED'
    ready = prepare(book, sequence=waiting['sequence'], marker='now ready')
    assert ready['state'] == 'APPLIED'
    before = current(book)
    assert prepare(book, sequence=waiting['sequence'], marker='late stale worker')['state'] == 'CONFLICT'
    assert current(book) == before


@pytest.mark.parametrize('state', ['MODEL_FAILED', 'CONFIG_CHANGED', 'STALE_INPUT', 'INVALID', 'MISSED_CUTOFF'])
def test_terminal_decision_cannot_reopen_or_change_terminal_reason(book, state):
    assert prepare(book)['state'] == 'APPLIED'
    result = save(book, current(book), state, reasons=['first final reason'])
    assert result['state'] == 'APPLIED' and result['decision_state'] == state
    final = current(book)
    assert prepare(book, sequence=final['sequence'], marker='replacement')['state'] == 'CONFLICT'
    assert save(book, final, 'INVALID', reasons=['overwrite'])['state'] == 'CONFLICT'
    assert current(book) == final


def test_frozen_status_cannot_be_manufactured_by_save_api(book):
    assert prepare(book)['state'] == 'APPLIED'
    assert save(book, current(book), 'FROZEN')['state'] == 'INVALID_INPUT'
    assert current(book)['state'] == 'PREPARED'


def test_prepare_policy_mutual_exclusion_and_future_range(book):
    assert prepare(book)['state'] == 'APPLIED'
    assert call(book, 'schedule_account_policy', {
        'effective_session': DAY, 'reason': 'occupied',
    })['state'] == 'CONFLICT'
    with ledger_lock(book.root):
        assert prepare(book, sequence=1)['state'] == 'CONFLICT'
    assert current(book)['sequence'] == 1


def test_prepare_blocks_unregistered_unknown_and_inactive_accounts(book):
    result = prepare(book, account='S1')
    assert result['state'] == 'INVALID_INPUT'
    assert book.get_strategy_decision('S1', target_session=DAY) is None
    assert book.create_account({'account_id': 'UNKNOWN', 'strategy_id': 'not-registered',
        'initial_cash_fen': 1}, operation_id='unknown')['state'] == 'INVALID_INPUT'


def test_prepare_rejects_knowledge_cutoff_before_market_close(settings):
    # The policy exists at the start of the session, but same-day prices are not yet final.
    now = datetime.fromisoformat('2026-10-29T08:00:00+08:00')
    book = LedgerService(settings, market=Facts(), clock=lambda: now, test_mode=True)
    book.initialize()
    assert call(book, 'activate_simulation', {'target_session': DAY})['state'] == 'APPLIED'
    result = prepare(book, cutoff='2026-10-29T08:00:00+08:00')
    assert result['state'] in {'INVALID_INPUT', 'NOT_DUE'}, result


def test_input_bundle_collections_must_be_structured_lists(book):
    result = call(book, 'prepare_strategy_decision', {
        'target_session': DAY, 'market_as_of': '2026-10-29',
        'input_cutoff': book._now().isoformat(), 'expected_sequence': 0,
        'input_bundle': {'market_snapshot_ids': 'not-a-list', 'data_evidence': {},
                         'account_revision': revision(book)},
    })
    assert result['state'] == 'INVALID_INPUT', result


def test_two_real_writers_cannot_overwrite_same_decision_sequence(book):
    assert prepare(book)['state'] == 'APPLIED'
    gate = Barrier(2)

    def contender(marker):
        gate.wait(timeout=5)
        return prepare(book, sequence=1, marker=marker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ['concurrent-a', 'concurrent-b']))
    assert sorted(r['state'] for r in results) == ['APPLIED', 'CONFLICT']
    row = current(book)
    assert row['sequence'] == 2 and len(row['history']) == 2
    assert revision(book) == 1


def test_policy_writer_cannot_enter_during_decision_prepare_transaction(book, monkeypatch):
    entered, release = Event(), Event()
    original = book._registered_policy_tx

    def hold_transaction(tx, target, cutoff=None):
        entered.set()
        assert release.wait(timeout=5)
        return original(tx, target, cutoff)

    monkeypatch.setattr(book, '_registered_policy_tx', hold_transaction)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(prepare, book)
        try:
            assert entered.wait(timeout=5)
            result = call(book, 'schedule_account_policy', {
                'effective_session': DAY, 'reason': 'concurrent change',
                'strategy_parameters': {'max_candidates': 3},
            })
            assert result['state'] == 'CONFLICT'
        finally:
            release.set()
        assert pending.result(timeout=5)['state'] == 'APPLIED'
    assert len(book.account_policies('LLM')) == 1
    assert current(book)['policy_hash'] == book.get_account_policy('LLM', DAY)['policy_hash']


@pytest.mark.parametrize('damage', ['current_document', 'input_document', 'history_reordered', 'history_duplicate'])
def test_decision_reads_reject_missing_or_rearranged_history_evidence(book, damage):
    assert prepare(book)['state'] == 'APPLIED'
    assert prepare(book, sequence=1, marker='second')['state'] == 'APPLIED'
    row = current(book)
    if damage in {'current_document', 'input_document'}:
        key = 'document' if damage == 'current_document' else damage
        (book.root / row[key]['path']).unlink()
    else:
        row['history'] = (list(reversed(row['history'])) if damage == 'history_reordered'
                          else [row['history'][0], row['history'][0]])
        rewrite_index(book, row)
    with pytest.raises(LedgerError) as failure:
        current(book)
    assert failure.value.state == 'INTEGRITY_ERROR'


def test_proposed_requires_complete_execution_validation_before_model_success(execution_book):
    book = execution_book
    request, core = strategy_request(book)
    request['ordered_intents'][0]['quantity'] = 123
    core['proposal'] = {key: value for key, value in request.items() if key != 'decision_ref'}
    request = publish(book, request, core)
    row = current(book)
    result = save(book, row, 'PROPOSED', proposal=request, decision_core=core)
    assert result['state'] in {'INVALID_INPUT', 'CONFLICT'}, result
    assert current(book)['state'] == 'PREPARED'
    assert book.report_snapshot('LLM')['model_attempts'][0]['status'] == 'RECEIVED'


def test_proposed_atomically_accepts_received_model_response_and_cannot_replace_input(execution_book):
    book = execution_book
    request, core = strategy_request(book)
    before_revision = revision(book)
    register_proposed(book, request)
    row = current(book)
    assert row['state'] == 'PROPOSED'
    assert book.report_snapshot('LLM')['model_attempts'][0]['status'] == 'SUCCEEDED'
    assert revision(book) == before_revision
    assert prepare(book, sequence=row['sequence'], marker='new input')['state'] == 'CONFLICT'
    altered = deepcopy(request)
    altered['ordered_intents'][0]['reason'] = 'rewritten decision'
    altered_core = deepcopy(core)
    altered_core['proposal'] = {k: v for k, v in altered.items() if k != 'decision_ref'}
    altered = publish(book, altered, altered_core)
    assert save(book, row, 'PROPOSED', proposal=altered, decision_core=altered_core)['state'] == 'CONFLICT'
    assert current(book) == row


@pytest.mark.parametrize('empty', [False, True])
def test_strategy_freeze_atomic_with_plan_reservations_and_replay(execution_book, monkeypatch, empty):
    book = execution_book
    request, _ = strategy_request(book, empty=empty)
    register_proposed(book, request)
    row = current(book)
    before = book.settings.database.read_bytes()
    original = book._freeze_registered_decision_tx

    def fail_after_decision_write(tx, decision, plan):
        original(tx, decision, plan)
        raise LedgerError('CONFLICT', 'injected interruption after FROZEN before reservations')

    monkeypatch.setattr(book, '_freeze_registered_decision_tx', fail_after_decision_write)
    assert application(book).freeze(request, operation_id='freeze-atomic')['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before
    assert current(book) == row
    assert book._read('LLM', lambda tx: tx.list('plans')) == []
    assert book._read('LLM', lambda tx: tx.list('reservations')) == []
    monkeypatch.setattr(book, '_freeze_registered_decision_tx', original)
    result = application(book).freeze(request, operation_id='freeze-atomic')
    assert result['state'] == 'APPLIED', result
    frozen = current(book)
    assert frozen['state'] == 'FROZEN'
    assert frozen['plan_id'] == result['plan']['plan_id']
    after = book.settings.database.read_bytes()
    again = application(book).freeze(request, operation_id='freeze-atomic')
    assert again['state'] == 'ALREADY_APPLIED', again
    assert book.settings.database.read_bytes() == after
    assert current(book) == frozen


def test_strategy_freeze_cannot_use_valid_but_unregistered_forged_core(execution_book):
    book = execution_book
    request, core = strategy_request(book)
    register_proposed(book, request)
    forged = deepcopy(core)
    forged['calculation']['explanation'] = 'independently valid but never accepted'
    forged_request = publish(book, request, forged)
    before = book.settings.database.read_bytes()
    result = application(book).freeze(forged_request, operation_id='forged-ref')
    assert result['state'] in {'CONFLICT', 'INTEGRITY_ERROR'}, result
    assert book.settings.database.read_bytes() == before
    assert current(book)['state'] == 'PROPOSED'


def test_freeze_replay_rechecks_frozen_decision_history_original(execution_book):
    book = execution_book
    request, _ = strategy_request(book)
    register_proposed(book, request)
    assert application(book).freeze(request, operation_id='replay-evidence')['state'] == 'APPLIED'
    row = current(book)
    (book.root / row['document']['path']).unlink()
    result = application(book).freeze(request, operation_id='replay-evidence')
    assert result['state'] == 'INTEGRITY_ERROR', result


def test_definition_change_after_proposed_blocks_freeze(execution_book, monkeypatch):
    book = execution_book
    request, _ = strategy_request(book)
    register_proposed(book, request)
    changed = deepcopy(STRATEGY_DEFINITIONS['LLM-Daily'])
    changed['implementation_hash'] = 'c' * 64
    monkeypatch.setitem(STRATEGY_DEFINITIONS, 'LLM-Daily', changed)
    assert application(book).freeze(request, operation_id='definition-changed')['state'] == 'CONFLICT'
    assert current(book)['state'] == 'PROPOSED'


def test_financial_revision_change_after_response_invalidates_original_decision(execution_book):
    book = execution_book
    request, core = strategy_request(book)
    row = current(book)
    # A financial transaction on this account intervenes while the response is being validated.
    # The isolated fixture has no position to settle; model the existing revision guard directly.
    def change_financial_baseline(tx):
        return {'reason': 'isolated concurrent financial commit'}

    assert book._mutate('LLM', 'financial-interleave', {}, revision(book), FIRST,
                        change_financial_baseline, financial=True)['state'] == 'APPLIED'
    result = save(book, row, 'PROPOSED', proposal=request, decision_core=core)
    assert result['state'] == 'APPLIED' and result['decision_state'] == 'STALE_INPUT', result
    assert current(book)['account_revision'] == row['account_revision']
    assert book.report_snapshot('LLM')['model_attempts'][0]['account_revision_at_claim'] == row['account_revision']
    assert application(book).freeze(request, operation_id='stale-input')['state'] != 'APPLIED'
    assert book._read('LLM', lambda tx: tx.list('plans')) == []


def test_late_response_can_be_preserved_but_decision_cannot_freeze(execution_book):
    book = execution_book
    request, core = strategy_request(book)
    row = current(book)
    stored = book.report_snapshot('LLM')['model_attempts'][0]['response_document']
    book.test_now[0] = datetime.fromisoformat(FIRST + 'T09:00:00+08:00')
    result = save(book, row, 'PROPOSED', proposal=request, decision_core=core)
    assert result['state'] == 'APPLIED' and result['decision_state'] == 'MISSED_CUTOFF', result
    assert book.report_snapshot('LLM')['model_attempts'][0]['response_document'] == stored
    assert application(book).freeze(request, operation_id='late-response')['state'] != 'APPLIED'
    assert book._read('LLM', lambda tx: tx.list('plans')) == []


def test_response_arriving_after_cutoff_is_saved_then_records_missed_cutoff(book):
    prepared = prepare(book)
    assert prepared['state'] == 'APPLIED'
    attempt = call(book, 'claim_model_attempt', {'target_session': DAY,
        'decision_id': prepared['decision_id'], 'input_hash': prepared['input_hash']})
    assert attempt['state'] == 'APPLIED'
    book.test_now[0] = datetime.fromisoformat(DAY + 'T09:00:01+08:00')
    received = call(book, 'finish_model_attempt', {'target_session': DAY,
        'attempt_id': attempt['attempt_id'], 'status': 'RECEIVED',
        'response': {'text': 'network response after deadline'}})
    assert received['state'] == 'APPLIED'
    row = current(book)
    result = save(book, row, 'PROPOSED')
    assert result['state'] == 'APPLIED' and result['decision_state'] == 'MISSED_CUTOFF'
    assert book.report_snapshot('LLM')['model_attempts'][0]['status'] == 'RECEIVED'
    assert call(book, 'freeze_plan', plan_payload(book, orders=[]))['state'] != 'APPLIED'
    assert call(book, 'claim_model_attempt', {'target_session': DAY,
        'decision_id': row['decision_id'], 'input_hash': row['input_hash']})['state'] != 'APPLIED'
