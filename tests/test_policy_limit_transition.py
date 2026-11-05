"""NL06-12: explicit future adoption keeps the original live parent and evidence."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta

import pytest
from execution_fixtures import advance, execution_book, set_quote
from test_execution_matching import bundle
from test_ledger_trading import Facts, call, plan_payload, revision
from test_strategy_acceptance import strategy_world
from test_strategy_rules import context

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
from china_stock_sim.common.strategy_transitions import TRANSITION_KIND, task_contract_hash
from china_stock_sim.config import load_settings
from china_stock_sim.execution.matching import DailyOpenMatcher
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.execution_evidence import account_input_hash
from china_stock_sim.ledger.kernel import LedgerError, canonical
from china_stock_sim.strategies.rules import plan

DAY = '2026-11-02'
NEXT = '2026-11-03'
CODES = [f'SH{600000 + i}' for i in range(10)]
assert execution_book and strategy_world


@pytest.fixture
def legacy_book(settings, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('NL must stay offline'))
    current = deepcopy(STRATEGY_DEFINITIONS)
    for strategy, definition in current.items():
        old = deepcopy(definition)
        old['strategy_version'] = strategy + '-implementation-1'
        old['defaults']['max_daily_symbols'] = 5
        monkeypatch.setitem(STRATEGY_DEFINITIONS, strategy, old)
    settings.raw['execution'].pop('daily_symbol_limit_scope')
    now = [datetime.fromisoformat('2026-11-01T20:00:00+08:00')]
    facts = Facts()
    start = date(2026, 1, 1)
    facts.calendar_rows = [{'date': (start + timedelta(days=i)).isoformat(),
        'is_trading_day': str(int((start + timedelta(days=i)).weekday() < 5))} for i in range(365)]
    service = LedgerService(settings, market=facts, clock=lambda: now[0], test_mode=True)
    service.test_now = now
    service.initialize()
    for account in ('S1', 'S1-LT', 'LLM'):
        assert call(service, 'activate_simulation', {'target_session': DAY}, account=account)['state'] == 'APPLIED'

    def upgrade():
        for strategy, definition in current.items():
            monkeypatch.setitem(STRATEGY_DEFINITIONS, strategy, deepcopy(definition))
        service.settings.raw['execution']['daily_symbol_limit_scope'] = 'LLM_ONLY'

    service.test_upgrade = upgrade
    return service


def task_row(book, account='S1'):
    return book._read(account, lambda tx: tx.get('rebalance_tasks', 'original-parent'))


def parent(book, account='S1'):
    old = book.get_account_policy(account, DAY)
    goals = [{'code': code, 'side': 'BUY', 'quantity': 400, 'target_shares': 400,
        'target_value_fen': 475_000, 'current_shares': 0, 'current_value_fen': 0,
        'kind': 'BUY', 'rank': index+1, 'reference_close': '10'} for index, code in enumerate(CODES[:8])]
    payload = {'task_id': 'original-parent', 'start_session': DAY, 'end_session': '2026-11-06',
        'targets': goals, 'parameters': old['policy']['parameters'],
        'ranking': [{'code': code, 'rank': i+1} for i, code in enumerate(CODES)],
        'policy_hash': old['policy_hash'],
        'strategy_implementation_hash': old['definition']['implementation_hash'],
        'input_document': document(book.root, 'nl-task-input', {'frozen': 'original targets'}),
        'initial_evaluated': True}
    assert call(book, 'save_rebalance_task', payload, account=account)['state'] == 'APPLIED'
    return task_row(book, account)


def adoption(book, account='S1', target=NEXT, **extra):
    old = book.account_policies(account)[0]
    return {'effective_session': target, 'reason': 'explicit NL isolated adoption',
        'strategy_parameters': {'max_daily_symbols': 0} if account != 'LLM' else {},
        'transition': {'kind': TRANSITION_KIND, 'from_policy_id': old['policy_id'],
                       'from_policy_hash': old['policy_hash']}, **extra}


def freeze_for(book, account, *, day=DAY, intents=None, task=False, identity=None):
    intents = intents or [{'code': code, 'side': 'BUY', 'quantity': 100,
                          'limit_price': '10.00'} for code in CODES[:5]]
    raw = plan_payload(book, day=day, orders=intents, plan_id=identity or 'legacy-plan')
    state = book.account_state(account, day)
    raw.update(account_id=account, strategy_id=state['strategy_id'], account_snapshot=state,
        account_snapshot_id=state['state_id'], account_snapshot_hash=account_input_hash(state),
        account_revision=state['account_revision'])
    if task:
        raw['task_id'] = 'original-parent'
    saved = call(book, 'freeze_plan', raw, account=account)
    assert saved['state'] == 'APPLIED', saved
    return book.get_plan(account, saved['plan_id'])


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_future_transition_allows_only_prior_pending_and_preserves_policy_and_plan_originals(legacy_book, account):
    book = legacy_book
    if account != 'LLM':
        original_task = parent(book, account)
    frozen = freeze_for(book, account, task=account != 'LLM', intents=None if account != 'LLM'
        else [{'code': code, 'side': 'BUY', 'quantity': 100, 'limit_price': '10.00'} for code in CODES[:2]])
    if account != 'LLM':
        original_task = task_row(book, account)
    prior = book.get_account_policy(account, DAY)
    originals = {row['document']['path']: (book.root / row['document']['path']).read_bytes()
                 for row in (prior, frozen)}
    book.test_upgrade()
    payload = adoption(book, account)
    before_revision = revision(book, account)
    result = call(book, 'schedule_account_policy', payload, account=account, op='NL-adopt')
    assert result['state'] == 'APPLIED', result
    assert revision(book, account) == before_revision
    assert book.get_account_policy(account, DAY) == prior
    new = book.get_account_policy(account, NEXT)
    assert new['transition']['from_policy_hash'] == prior['policy_hash']
    assert new['policy']['strategy_version'].endswith('-implementation-2')
    assert new['policy']['parameters']['max_daily_symbols'] == (5 if account == 'LLM' else 0)
    if account != 'LLM':
        assert task_row(book, account) == original_task
        proof = book.get_rebalance_policy_compatibility(account, 'original-parent', NEXT)
        assert proof['transition'] == new['transition']
    else:
        assert new['transition']['tasks'] == []
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', payload, account=account,
                op='NL-adopt', expected=0)['state'] == 'ALREADY_APPLIED'
    assert book.settings.database.read_bytes() == before
    assert all((book.root / path).read_bytes() == content for path, content in originals.items())
    assert 'daily_symbol_limit_scope' not in frozen['rule_policy']


@pytest.mark.parametrize('change', ['parameters', 'display', 'model', 'kind', 'extra',
    'from_hash', 'from_id', 'today', 'old_revision', 'null'])
def test_transition_rejects_widening_or_stale_identity_without_database_write(legacy_book, change):
    book = legacy_book
    parent(book)
    book.test_upgrade()
    payload = adoption(book)
    expected = revision(book, 'S1')
    if change == 'parameters':
        payload['strategy_parameters']['target_count'] = 9
    elif change == 'display':
        payload['display_name'] = 'renamed'
    elif change == 'model':
        payload['model_ref'] = 'different'
    elif change == 'kind':
        payload['transition']['kind'] = 'OTHER'
    elif change == 'extra':
        payload['transition']['allow_anything'] = True
    elif change in {'from_hash', 'from_id'}:
        payload['transition']['from_policy_' + change.removeprefix('from_')] = 'wrong'
    elif change == 'today':
        book.test_now[0] = datetime.fromisoformat(NEXT+'T08:00:00+08:00')
    elif change == 'old_revision':
        expected -= 1
    else:
        payload['transition'] = None
    before = book.settings.database.read_bytes()
    rejected = call(book, 'schedule_account_policy', payload, account='S1', expected=expected)
    assert rejected['state'] in {'INVALID_INPUT', 'CONFLICT', 'STALE_ACCOUNT'}, rejected
    assert book.settings.database.read_bytes() == before


def test_policy_cas_prevents_same_financial_revision_adopting_over_another_future_policy(legacy_book):
    book = legacy_book
    book.test_upgrade()
    stale = adoption(book)
    expected = revision(book, 'S1')
    assert call(book, 'schedule_account_policy', {**stale, 'effective_session': '2026-11-04'},
                account='S1')['state'] == 'APPLIED'
    assert revision(book, 'S1') == expected
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', stale, account='S1', expected=expected)['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before


def test_same_effective_day_and_changed_operation_payload_never_replace_a_policy(legacy_book):
    book = legacy_book
    book.test_upgrade()
    payload = adoption(book)
    assert call(book, 'schedule_account_policy', payload, account='S1', op='once')['state'] == 'APPLIED'
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', payload, account='S1')['state'] == 'CONFLICT'
    assert call(book, 'schedule_account_policy', {**payload, 'reason': 'different'},
                account='S1', op='once')['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('day', [NEXT, '2026-11-04'])
def test_pending_order_at_or_after_effective_session_still_blocks(legacy_book, day):
    book = legacy_book
    with sqlite3.connect(book.settings.database) as db:
        db.execute('INSERT INTO orders VALUES (?,?,?)',
                   ('pending', 'S1', canonical({'target_session': day, 'status': 'PENDING'})))
    book.test_upgrade()
    before = book.settings.database.read_bytes()
    result = call(book, 'schedule_account_policy', adoption(book), account='S1')
    assert result['state'] == 'CONFLICT', result
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('table', ['strategy_decisions', 'model_attempts', 'plans'])
def test_any_existing_future_decision_attempt_or_plan_blocks_transition(legacy_book, table):
    book = legacy_book
    # A controlled pre-existing registry row tests the early conflict guard;
    # rejection must occur before attempting to interpret its other contents.
    with sqlite3.connect(book.settings.database) as db:
        db.execute(f'INSERT INTO {table} VALUES (?,?,?)',
                   ('future', 'S1', canonical({'target_session': NEXT})))
    book.test_upgrade()
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', adoption(book), account='S1')['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before


def test_transition_keeps_live_partial_fills_and_more_than_five_future_children(legacy_book):
    book = legacy_book
    original_task = parent(book)
    original_plan = freeze_for(book, 'S1', task=True)
    original_bytes = (book.root / original_plan['document']['path']).read_bytes()
    book.test_upgrade()
    assert call(book, 'schedule_account_policy', adoption(book), account='S1')['state'] == 'APPLIED'
    assert task_row(book)['completed_quantities'] == {}
    book.market.quotes[(CODES[1], DAY)] = '10.50'  # one old order does not fill
    book.test_now[0] = datetime.fromisoformat(DAY+'T20:00:00+08:00')
    matcher = DailyOpenMatcher(clock=book._now)
    while True:
        results = matcher.match(original_plan, book.execution_view('S1', original_plan['plan_id']),
                                bundle(book, original_plan))
        if not results:
            break
        result = call(book, 'apply_execution_results', asdict(results[0]), account='S1')
        assert result['state'] == 'APPLIED', result
    assert call(book, 'close_session', {'session': DAY,
        'knowledge_cutoff': book._now().isoformat()}, account='S1')['state'] == 'APPLIED'
    live = task_row(book)
    assert task_contract_hash(live) == task_contract_hash(original_task)
    assert live['completed_quantities'] == {f'{code}:BUY': 100 for code in CODES[:5] if code != CODES[1]}
    row = book.get_account_policy('S1', NEXT)
    proof = book.get_rebalance_policy_compatibility('S1', 'original-parent', NEXT)['transition']
    ctx = replace(context(), account_id='S1', target_session=NEXT, market_as_of=DAY,
        policy=row['policy'], account=book.account_state('S1', NEXT),
        parent_task={**live, 'policy_transition': proof}, task_sessions=tuple(live['sessions']),
        input_cutoff=book._now().isoformat(), generated_at=book._now().isoformat())
    calculated = plan(ctx)
    assert len(calculated.ordered_intents) == 8
    assert calculated.task_id == original_task['task_id']
    assert calculated.calculation['frozen_ranking'] == original_task['ranking']
    for intent in calculated.ordered_intents:
        done = live['completed_quantities'].get(intent['code']+':BUY', 0)
        assert intent['quantity'] <= 400 - done
    frozen = freeze_for(book, 'S1', day=NEXT, intents=list(calculated.ordered_intents),
                         task=True, identity='future-child')
    assert len(frozen['orders']) == 8 and frozen['task_id'] == original_task['task_id']
    assert len(book._read('S1', lambda tx: tx.list('rebalance_tasks'))) == 1
    assert task_row(book)['sessions'] == original_task['sessions']
    assert (book.root / original_plan['document']['path']).read_bytes() == original_bytes
    forged = deepcopy(live)
    forged['parameters']['target_count'] = 9
    with pytest.raises(ValueError):
        plan(replace(ctx, parent_task={**forged, 'policy_transition': proof}))

    # The original deadline remains closed despite the new policy identity.
    expired = plan(replace(ctx, target_session='2026-11-09'))
    assert expired.no_order_reason == 'REBALANCE_WINDOW_CLOSED' and not expired.ordered_intents


@pytest.mark.parametrize('field', ['targets', 'ranking', 'end_session', 'input_document'])
def test_transition_task_corruption_is_rejected_in_read_freeze_and_backup(legacy_book, field):
    book = legacy_book
    parent(book)
    book.test_upgrade()
    assert call(book, 'schedule_account_policy', adoption(book), account='S1')['state'] == 'APPLIED'
    book.test_now[0] = datetime.fromisoformat(DAY+'T20:00:00+08:00')
    assert call(book, 'close_session', {'session': DAY,
        'knowledge_cutoff': book._now().isoformat()}, account='S1')['state'] == 'APPLIED'
    with sqlite3.connect(book.settings.database) as db:
        row = json.loads(db.execute("SELECT data FROM rebalance_tasks WHERE account_id='S1'").fetchone()[0])
        if field == 'targets':
            row['targets'][0]['quantity'] += 100
        elif field == 'ranking':
            row['ranking'][0]['rank'] += 1
        elif field == 'end_session':
            row['end_session'] = '2026-11-09'
        else:
            row['input_document']['content_hash'] = '0' * 64
        db.execute("UPDATE rebalance_tasks SET data=? WHERE account_id='S1'", (canonical(row),))
    with pytest.raises(LedgerError, match='政策衔接'):
        book.get_rebalance_policy_compatibility('S1', 'original-parent', NEXT)
    backup = book.backup(label='manual')
    assert backup['state'] != 'COMPLETE'
    book.test_now[0] = datetime.fromisoformat(DAY+'T20:00:00+08:00')
    raw = plan_payload(book, day=NEXT)
    state = book.account_state('S1', NEXT)
    raw.update(account_id='S1', strategy_id='S1', account_snapshot=state,
        account_snapshot_id=state['state_id'], account_snapshot_hash=account_input_hash(state),
        account_revision=state['account_revision'], task_id='original-parent')
    before = book.settings.database.read_bytes()
    rejected = call(book, 'freeze_plan', raw, account='S1')
    assert rejected['state'] == 'INTEGRITY_ERROR', rejected
    assert book.settings.database.read_bytes() == before


def test_backup_restore_retains_own_transition_policy_and_parent_contract(legacy_book, tmp_path):
    book = legacy_book
    original = parent(book)
    book.test_upgrade()
    assert call(book, 'schedule_account_policy', adoption(book), account='S1')['state'] == 'APPLIED'
    before = book.get_account_policy('S1', NEXT)
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE', backup
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
    restored_root = tmp_path / 'restored'
    restored = book.restore_backup(backup['backup_id'], restored_root)
    assert restored['state'] == 'RESTORED', restored
    copy = LedgerService(load_settings(restored_root), market=book.market,
                         clock=book._now, test_mode=True)
    assert copy.get_account_policy('S1', NEXT) == before
    assert task_row(copy) == original
    assert copy.get_rebalance_policy_compatibility('S1', 'original-parent', NEXT)['transition'] == before['transition']
    folder = book.root / 'data/backups/ledger' / backup['backup_id']
    archived_original = folder / 'evidence' / before['transition']['from_policy_document']['path']
    assert archived_original.is_file()
    archived_original.unlink()
    assert book.verify_backup(backup['backup_id'])['state'] != 'READY'
    with pytest.raises((ValueError, OSError)):
        book.restore_backup(backup['backup_id'], tmp_path / 'refused-restore')


@pytest.fixture
def legacy_strategy_world(settings, monkeypatch, request):
    current = deepcopy(STRATEGY_DEFINITIONS)
    for strategy, definition in current.items():
        old = deepcopy(definition)
        old['strategy_version'] = strategy + '-implementation-1'
        old['defaults']['max_daily_symbols'] = 5
        monkeypatch.setitem(STRATEGY_DEFINITIONS, strategy, old)
    settings.raw['execution'].pop('daily_symbol_limit_scope')
    world = request.getfixturevalue('strategy_world')

    def upgrade():
        for strategy, definition in current.items():
            monkeypatch.setitem(STRATEGY_DEFINITIONS, strategy, deepcopy(definition))
        settings.raw['execution']['daily_symbol_limit_scope'] = 'LLM_ONLY'

    return *world, upgrade


def test_real_strategy_inputs_and_registered_freeze_continue_original_parent_after_upgrade(legacy_strategy_world):
    book, make_app, _, upgrade = legacy_strategy_world
    old_day, future = '2026-10-30', '2026-11-02'
    assert make_app().publish('S1', old_day)['state'] == 'FROZEN'
    old_plan = book.get_plan('S1', target_session=old_day)
    old_core = book.get_strategy_decision('S1', target_session=old_day)['decision_core']
    original_bytes = (book.root / old_core['path']).read_bytes()
    original = book._read('S1', lambda tx: tx.get('rebalance_tasks', old_plan['task_id']))
    advance(book, old_day, '10:00:00')
    upgrade()
    applied = call(book, 'schedule_account_policy', adoption(book, target=future), account='S1')
    assert applied['state'] == 'APPLIED', applied
    # The old frozen strategy decision remains recoverable under its original
    # registered core; no re-planning/model call under the new implementation.
    assert make_app().recover('S1', old_day)['state'] == 'FROZEN'
    assert make_app().inspect('S1', future)['state'] == 'WAITING_INPUT'
    missed_code = old_plan['orders'][0]['code']
    set_quote(book, missed_code, old_day, '11.00', preclose='10.00')
    advance(book, old_day)
    execution = ExecutionApplication(book.settings, market=book.market, ledger=book,
                                    clock=book._now, test_mode=True)
    settled = execution.settle('S1', old_plan['plan_id'], operation_id='NL-real-old-settle')
    assert settled['completion_status'] == 'COMPLETE', settled
    assert call(book, 'close_session', {'session': old_day,
        'knowledge_cutoff': book._now().isoformat()}, account='S1')['state'] == 'APPLIED'
    prepared = make_app().inspect('S1', future, include_input=True)
    assert prepared['state'] == 'READY', prepared
    parent_projection = prepared['input_bundle']['context']['parent_task']
    assert parent_projection['task_id'] == original['task_id']
    assert parent_projection['policy_transition']['kind'] == TRANSITION_KIND
    assert parent_projection['completed_quantities'] != {}
    child = make_app().publish('S1', future)
    assert child['state'] == 'FROZEN', child
    future_plan = book.get_plan('S1', target_session=future)
    assert future_plan['task_id'] == original['task_id']
    assert len(future_plan['orders']) == 1 and future_plan['orders'][0]['code'] == missed_code
    live = book._read('S1', lambda tx: tx.get('rebalance_tasks', original['task_id']))
    assert task_contract_hash(live) == task_contract_hash(original)
    assert (book.root / old_core['path']).read_bytes() == original_bytes
