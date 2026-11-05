"""Same-parameter maintenance adoption preserves the original live rebalance chain."""

import sqlite3
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime

import pytest
from test_execution_matching import bundle
from test_ledger_trading import call, revision
from test_policy_limit_transition import (
    CODES,
    DAY,
    NEXT,
    adoption,
    freeze_for,
    legacy_book,
    parent,
    task_row,
)
from test_strategy_rules import context

from china_stock_sim.common import strategy_definitions as definitions
from china_stock_sim.common.strategy_transitions import (
    MAINTENANCE_KIND,
    parent_transition_matches,
    task_contract_hash,
)
from china_stock_sim.config import load_settings
from china_stock_sim.execution.matching import DailyOpenMatcher
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest
from china_stock_sim.strategies.rules import plan

assert legacy_book
FUTURE = '2026-11-04'
WIRE = 'src/china_stock_sim/common/wire.py'


def maintenance(book, account='S1', target=FUTURE):
    old = book.get_account_policy(account, target)
    return {'effective_session': target, 'reason': 'explicit isolated storage maintenance',
        'transition': {'kind': MAINTENANCE_KIND, 'from_policy_id': old['policy_id'],
                       'from_policy_hash': old['policy_hash']}}


def change_manifest(monkeypatch, *, path=WIRE, value='1' * 64, transform=None):
    original = definitions._implementation_files

    def changed(strategy):
        files = original(strategy)
        files[path] = value
        if transform:
            transform(files)
        return files

    monkeypatch.setattr(definitions, '_implementation_files', changed)


def adopt_cap(book, account='S1', *, frozen=False):
    if account != 'LLM':
        parent(book, account)
    if frozen:
        freeze_for(book, account, task=account != 'LLM')
    book.test_upgrade()
    result = call(book, 'schedule_account_policy', adoption(book, account), account=account)
    assert result['state'] == 'APPLIED', result
    book.test_now[0] = datetime.fromisoformat(NEXT + 'T12:00:00+08:00')


def rewrite_policy(book, row):
    body = {key: row[key] for key in ('policy', 'policy_revision', 'definition',
        'definition_status', 'display_name', 'reason', 'transition') if key in row}
    row['document'] = document(book.root, 'maintenance-test-forgery', body)
    with sqlite3.connect(book.settings.database) as db:
        db.execute('UPDATE account_policies SET data=? WHERE account_id=? AND id=?',
                   (canonical(row), row['account_id'], row['policy_id']))


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_future_maintenance_preserves_original_policies_pending_orders_and_parent(legacy_book, monkeypatch, account):
    book = legacy_book
    adopt_cap(book, account, frozen=True)
    old = book.get_account_policy(account, NEXT)
    prior = book.get_plan(account, target_session=DAY)
    original = task_row(book, account) if account != 'LLM' else None
    money = book.account_state(account, NEXT)
    originals = {r['document']['path']: (book.root / r['document']['path']).read_bytes() for r in (old, prior)}
    change_manifest(monkeypatch)
    payload = maintenance(book, account)
    financial_revision = revision(book, account)
    result = call(book, 'schedule_account_policy', payload, account=account, op='maintenance')
    assert result['state'] == 'APPLIED', result
    new = book.get_account_policy(account, FUTURE)
    assert new['policy']['parameters'] == old['policy']['parameters']
    assert new['policy']['strategy_version'] == old['policy']['strategy_version']
    assert new['definition']['implementation_hash'] != old['definition']['implementation_hash']
    assert new['transition']['parameters_hash'] == digest(old['policy']['parameters'])
    assert book.get_account_policy(account, NEXT) == old
    assert book.get_plan(account, target_session=DAY) == prior
    assert revision(book, account) == financial_revision
    after = book.account_state(account, NEXT)
    for field in ('cash_fen', 'reserved_cash_fen', 'positions', 'initial_cash_fen', 'simulation_start_session'):
        assert after[field] == money[field]
    if original:
        assert task_row(book, account) == original
        projection = book.get_rebalance_policy_compatibility(account, original['task_id'], FUTURE)['transition']
        assert projection == new['transition']
        assert parent_transition_matches({**original, 'policy_transition': projection},
                                         new['policy'], target_session=FUTURE)
    else:
        assert new['transition']['tasks'] == []
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', payload, account=account,
                op='maintenance', expected=0)['state'] == 'ALREADY_APPLIED'
    assert book.settings.database.read_bytes() == before
    assert all((book.root / path).read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize('change', ['rule_file', 'prompt_file', 'unknown_file', 'removed_file',
    'unchanged_manifest', 'parameters', 'display', 'version', 'uses_model', 'stale_policy',
    'stale_revision', 'today', 'past_parent_window'])
def test_maintenance_rejects_nonmaintenance_changes_without_database_write(legacy_book, monkeypatch, change):
    book = legacy_book
    adopt_cap(book)
    payload = maintenance(book)
    expected = revision(book, 'S1')
    path = {'rule_file': 'src/china_stock_sim/strategies/rules.py',
            'prompt_file': 'src/china_stock_sim/strategies/llm_prompt.py',
            'unknown_file': 'src/china_stock_sim/arbitrary.py'}.get(change, WIRE)
    if change != 'unchanged_manifest':
        change_manifest(monkeypatch, path=path, transform=(
            lambda files: files.pop('src/china_stock_sim/common/fees.py')) if change == 'removed_file' else None)
    if change == 'parameters':
        payload['strategy_parameters'] = {'target_count': 9}
    elif change == 'display':
        payload['display_name'] = 'changed'
    elif change in {'version', 'uses_model'}:
        new = deepcopy(definitions.STRATEGY_DEFINITIONS['S1'])
        new['strategy_version' if change == 'version' else 'uses_model'] = (
            'S1-implementation-3' if change == 'version' else True)
        monkeypatch.setitem(definitions.STRATEGY_DEFINITIONS, 'S1', new)
    elif change == 'stale_policy':
        payload['transition']['from_policy_hash'] = '0' * 64
    elif change == 'stale_revision':
        expected -= 1
    elif change == 'today':
        payload['effective_session'] = NEXT
    elif change == 'past_parent_window':
        payload['effective_session'] = '2026-11-09'
    before = book.settings.database.read_bytes()
    result = call(book, 'schedule_account_policy', payload, account='S1', expected=expected)
    assert result['state'] in {'INVALID_INPUT', 'CONFLICT', 'STALE_ACCOUNT'}, result
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('table', ['orders', 'strategy_decisions', 'model_attempts', 'plans'])
def test_future_business_still_blocks_maintenance(legacy_book, monkeypatch, table):
    book = legacy_book
    adopt_cap(book)
    change_manifest(monkeypatch)
    with sqlite3.connect(book.settings.database) as db:
        db.execute(f'INSERT INTO {table} VALUES (?,?,?)', ('future', 'S1', canonical({
            'target_session': FUTURE, 'status': 'PENDING'})))
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', maintenance(book), account='S1')['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before


def test_self_reported_predecessor_binding_does_not_authorize_an_unrelated_parent(legacy_book, monkeypatch):
    book = legacy_book
    adopt_cap(book)
    old = book.get_account_policy('S1', NEXT)
    payload = maintenance(book)
    # Forge a mutually agreeing task digest and predecessor document. The chain
    # must still prove the original 5->0 link to its actual base policy.
    task = task_row(book)
    task['policy_hash'] = 'f' * 64
    with sqlite3.connect(book.settings.database) as db:
        db.execute("UPDATE rebalance_tasks SET data=? WHERE account_id='S1'", (canonical(task),))
    old['transition']['tasks'][0]['immutable_contract_hash'] = task_contract_hash(task)
    rewrite_policy(book, old)
    change_manifest(monkeypatch)
    before = book.settings.database.read_bytes()
    assert call(book, 'schedule_account_policy', payload, account='S1')['state'] == 'INTEGRITY_ERROR'
    with pytest.raises(LedgerError, match='政策衔接'):
        book.get_account_policy('S1', NEXT)
    assert book.settings.database.read_bytes() == before
    assert book.backup(label='manual')['state'] != 'COMPLETE'


@pytest.mark.parametrize('corruption', ['cycle', 'revision_gap', 'missing_original'])
def test_maintenance_read_rechecks_entire_policy_chain(legacy_book, monkeypatch, corruption):
    book = legacy_book
    adopt_cap(book)
    change_manifest(monkeypatch)
    assert call(book, 'schedule_account_policy', maintenance(book), account='S1')['state'] == 'APPLIED'
    old = book.get_account_policy('S1', NEXT)
    if corruption == 'missing_original':
        (book.root / old['transition']['from_policy_document']['path']).unlink()
    else:
        if corruption == 'cycle':
            old['transition']['from_policy_id'] = old['policy_id']
        else:
            old['policy_revision'] = 7
        rewrite_policy(book, old)
    with pytest.raises(LedgerError):
        book.get_rebalance_policy_compatibility('S1', 'original-parent', FUTURE)
    assert book.backup(label='manual')['state'] != 'COMPLETE'


def test_live_old_fills_continue_in_same_parent_and_future_child_freeze(legacy_book, monkeypatch):
    book = legacy_book
    adopt_cap(book, frozen=True)
    old_plan = book.get_plan('S1', target_session=DAY)
    original = task_row(book)
    # Both explicit future adoptions precede the old session's fills. Do not
    # rewind a persisted financial clock merely to simulate delayed settlement.
    book.test_now[0] = datetime.fromisoformat(DAY + 'T12:00:00+08:00')
    change_manifest(monkeypatch)
    assert call(book, 'schedule_account_policy', maintenance(book), account='S1')['state'] == 'APPLIED'
    book.test_now[0] = datetime.fromisoformat(DAY + 'T20:00:00+08:00')
    matcher = DailyOpenMatcher(clock=book._now)
    while results := matcher.match(old_plan, book.execution_view('S1', old_plan['plan_id']), bundle(book, old_plan)):
        saved = call(book, 'apply_execution_results', asdict(results[0]), account='S1')
        assert saved['state'] == 'APPLIED', saved
    assert call(book, 'close_session', {'session': DAY,
        'knowledge_cutoff': book._now().isoformat()}, account='S1')['state'] == 'APPLIED'
    book.test_now[0] = datetime.fromisoformat(NEXT + 'T20:00:00+08:00')
    assert call(book, 'close_session', {'session': NEXT,
        'knowledge_cutoff': book._now().isoformat()}, account='S1')['state'] == 'APPLIED'
    live = task_row(book)
    assert live['completed_quantities'] == {code + ':BUY': 100 for code in CODES[:5]}
    assert task_contract_hash(live) == task_contract_hash(original)
    row = book.get_account_policy('S1', FUTURE)
    proof = book.get_rebalance_policy_compatibility('S1', 'original-parent', FUTURE)['transition']
    ctx = replace(context(), account_id='S1', target_session=FUTURE, market_as_of=NEXT,
        policy=row['policy'], account=book.account_state('S1', FUTURE),
        parent_task={**live, 'policy_transition': proof}, task_sessions=tuple(live['sessions']),
        input_cutoff=book._now().isoformat(), generated_at=book._now().isoformat())
    decision = plan(ctx)
    assert decision.task_id == original['task_id']
    assert decision.calculation['frozen_ranking'] == original['ranking']
    for intent in decision.ordered_intents:
        assert intent['quantity'] <= 400 - live['completed_quantities'].get(intent['code'] + ':BUY', 0)
    child = freeze_for(book, 'S1', day=FUTURE, intents=list(decision.ordered_intents),
                       task=True, identity='maintenance-child')
    assert child['task_id'] == original['task_id'] and len(child['orders']) > 5
    assert task_row(book)['end_session'] == original['end_session']
    assert len(book._read('S1', lambda tx: tx.list('rebalance_tasks'))) == 1
    assert plan(replace(ctx, target_session='2026-11-09')).no_order_reason == 'REBALANCE_WINDOW_CLOSED'
    forged = deepcopy(row['policy'])
    forged['parameters']['target_count'] = 9
    assert not parent_transition_matches(ctx.parent_task, forged, target_session=FUTURE)


def test_repeated_maintenance_chain_and_backup_restore_keep_all_original_links(legacy_book, monkeypatch, tmp_path):
    book = legacy_book
    adopt_cap(book)
    original = task_row(book)
    change_manifest(monkeypatch)
    assert call(book, 'schedule_account_policy', maintenance(book), account='S1')['state'] == 'APPLIED'
    change_manifest(monkeypatch, value='2' * 64)
    assert call(book, 'schedule_account_policy', maintenance(book, target='2026-11-05'), account='S1')['state'] == 'APPLIED'
    selected = book.get_account_policy('S1', '2026-11-05')
    assert selected['policy_revision'] == 3
    assert book.get_rebalance_policy_compatibility('S1', 'original-parent', '2026-11-05')['transition'] == selected['transition']
    assert task_row(book) == original
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE', backup
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
    destination = tmp_path / 'restored-maintenance'
    assert book.restore_backup(backup['backup_id'], destination)['state'] == 'RESTORED'
    restored = LedgerService(load_settings(destination), market=book.market, clock=book._now, test_mode=True)
    assert restored.get_account_policy('S1', '2026-11-05') == selected
    assert restored.get_rebalance_policy_compatibility('S1', 'original-parent', '2026-11-05')['transition'] == selected['transition']
    assert task_row(restored) == original
