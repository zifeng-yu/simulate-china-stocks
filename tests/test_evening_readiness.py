"""Isolated financial proof, date and process-boundary checks for ER01–ER07."""

import fcntl
import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from execution_fixtures import FIRST, advance, execution_book
from test_ledger_trading import call
from test_strategy_acceptance import ControlledModel, response, strategy_world

from china_stock_sim.ledger.decision_evidence import verification_scope
from china_stock_sim.ledger.readiness import read_facts
from china_stock_sim.readiness import probe, status
from china_stock_sim.readiness.domain import calendar_window, evaluate
from china_stock_sim.scheduler.state import WorkflowJournal

assert execution_book and strategy_world
D, N = FIRST, '2026-11-02'
NOW = datetime.fromisoformat(D + 'T20:00:00+08:00')


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def update(book, table, account, identity, change):
    with sqlite3.connect(book.settings.database) as db:
        condition = 'account_id=?' if table == 'account_state' else 'account_id=? AND id=?'
        args = (account,) if table == 'account_state' else (account, identity)
        row = json.loads(db.execute(f'SELECT data FROM {table} WHERE {condition}', args).fetchone()[0])
        change(row)
        db.execute(f'UPDATE {table} SET data=? WHERE {condition}', (json.dumps(row), *args))


def insert(book, table, identity, payload, account='S1'):
    with sqlite3.connect(book.settings.database) as db:
        db.execute(f'INSERT INTO {table}(id,account_id,data) VALUES(?,?,?)',
                   (identity, account, json.dumps(payload)))


def journal(book, *, phase=None, live=False):
    log = WorkflowJournal(book.settings)
    run = 'isolated-readiness'
    start = NOW if log.path.exists() else NOW - timedelta(minutes=5)
    log.start(run, now=start, metadata={'process_birth': 'isolated-birth'})
    log.context(run, now=start, business_session=D, target_session=N)
    for spec in book.settings.accounts:
        for name in ('input_review', 'plan'):
            log.phase(run, name, now=NOW, state='RUNNING', session=D, target_session=N,
                      account_id=spec.id)
            log.phase(run, name, now=NOW, state='COMPLETE', session=D, target_session=N,
                      account_id=spec.id, result={'state': 'FROZEN' if name == 'plan' else 'ALREADY_APPLIED'})
    if phase:
        log.phase(run, phase, now=NOW, state='RUNNING', session=D, target_session=N)
    if not live:
        log.finish(run, now=NOW, state='COMPLETE', result={'state': 'COMPLETE'})
    return log


@pytest.fixture
def ready_book(strategy_world):
    book, make_app, _ = strategy_world
    advance(book, D)
    for config in book.settings.raw['strategies'].values():
        config['enabled'] = True
    for spec in book.settings.accounts:
        result = call(book, 'close_session', {'session': D, 'knowledge_cutoff': NOW.isoformat()}, account=spec.id)
        assert result['state'] == 'APPLIED', result
    model = ControlledModel([response(N, intents=[])])
    app = make_app(client=model)
    # Same outer persistence scope as WorkflowApplication.run. These are actual
    # strategy/execution/ledger calls; no validation receipt is fabricated.
    with verification_scope(book.root, persist=True):
        for spec in book.settings.accounts:
            result = app.publish(spec.id, N)
            assert result['state'] == 'FROZEN', result
    assert len(model.calls) == 1
    journal(book)
    return book


def codes(output):
    return {r['code'] for r in output['reasons']} | {
        r['code'] for account in output['accounts'] for r in account['reasons']}


def test_natural_publish_persists_proofs_and_new_process_reads_without_materializing(ready_book):
    book = ready_book
    before = tree(book.root)
    program = '''import json,sys
from datetime import datetime
from china_stock_sim.config import load_settings
from china_stock_sim.readiness.status import build_status
import china_stock_sim.ledger.plan_storage as plans
import china_stock_sim.ledger.decision_evidence as evidence
def forbidden(*a,**k): raise AssertionError('immutable body expanded by readiness')
plans.resolve_plan=forbidden
evidence.read_business_document=forbidden
print(json.dumps(build_status(load_settings(sys.argv[1]),now=datetime.fromisoformat(sys.argv[2]))))
'''
    reply = subprocess.run([sys.executable, '-c', program, str(book.root), NOW.isoformat()],
                           capture_output=True, text=True, timeout=20, check=True)
    value = json.loads(reply.stdout)
    assert value['state'] == 'READY', value
    assert all(a['ready'] for a in value['accounts'])
    assert '合法空计划' in next(a['summary'] for a in value['accounts'] if a['account_id'] == 'LLM')
    assert datetime.fromisoformat(value['expires_at']) - NOW <= timedelta(seconds=90)
    assert tree(book.root) == before


@pytest.mark.parametrize(('timestamp', 'day', 'target', 'daytime'), [
    ('2026-10-30T18:09:59+08:00', '2026-10-30', '2026-11-02', True),
    ('2026-10-30T18:10:00+08:00', '2026-10-30', '2026-11-02', False),
    ('2026-10-31T00:00:00+08:00', '2026-10-30', '2026-11-02', False),
    ('2026-11-01T20:00:00+08:00', '2026-10-30', '2026-11-02', False),
    ('2026-11-02T08:59:59+08:00', '2026-10-30', '2026-11-02', False),
    ('2026-11-02T09:00:00+08:00', '2026-11-02', '2026-11-03', True),
    ('2026-11-02T18:10:00+08:00', '2026-11-02', '2026-11-03', False),
])
def test_calendar_lamp_cycles_and_lease_do_not_reuse_yesterday_green(timestamp, day, target, daytime):
    rows = [{'date': date, 'is_trading_day': flag} for date, flag in (
        ('2026-10-29', 1), ('2026-10-30', 1), ('2026-10-31', 0), ('2026-11-01', 0),
        ('2026-11-02', 1), ('2026-11-03', 1))]
    now = datetime.fromisoformat(timestamp)
    result = calendar_window(rows, now)
    assert (result['business_session'], result['target_session'], result['daytime']) == (day, target, daytime)
    assert now < datetime.fromisoformat(result['expires_at']) <= now + timedelta(seconds=90)
    if timestamp.endswith('08:59:59+08:00'):
        assert result['expires_at'] == '2026-11-02T09:00:00+08:00'


def test_unknown_calendar_does_not_guess_weekend(settings):
    before = tree(settings.root)
    assert status.build_status(settings, NOW)['state'] == 'UNKNOWN'
    assert tree(settings.root) == before
    with pytest.raises(ValueError):
        calendar_window([{'date': D, 'is_trading_day': 1}], NOW)


@pytest.mark.parametrize(('field', 'value', 'expected'), [
    ('simulation_start_session', None, 'ACCOUNT_NOT_ACTIVE_FOR_DAY'),
    ('settled_through', '2026-10-29', 'CONTINUOUS_WATERMARK_PENDING'),
    ('valued_through', '2026-10-29', 'CONTINUOUS_WATERMARK_PENDING'),
    ('restate_from', D, 'RESTATEMENT_PENDING'),
])
def test_watermark_activation_and_restatement_prevent_green(ready_book, field, value, expected):
    update(ready_book, 'account_state', 'S1', None, lambda row: row.update({field: value}))
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'WAITING', result
    assert expected in codes(result)


def test_provisional_and_missing_plan_are_not_complete(ready_book):
    book = ready_book
    with sqlite3.connect(book.settings.database) as db:
        db.execute("UPDATE daily_nav SET data=json_set(data,'$.valuation_status','PROVISIONAL') WHERE account_id='S1'")
        db.execute("DELETE FROM plans WHERE account_id='LLM'")
    value = status.build_status(book.settings, NOW)
    assert value['state'] == 'WAITING'
    assert {'FINAL_STATEMENT_PENDING', 'NEXT_PLAN_PENDING'} <= codes(value)


def test_missing_account_and_strategy_identity_are_blocked(ready_book):
    with sqlite3.connect(ready_book.settings.database) as db:
        db.execute("DELETE FROM accounts WHERE account_id='LLM'")
        db.execute("UPDATE accounts SET strategy_id='S1-LT' WHERE account_id='S1'")
    value = status.build_status(ready_book.settings, NOW)
    assert value['state'] == 'BLOCKED'
    assert {'ACCOUNT_MISSING', 'ACCOUNT_STRATEGY_MISMATCH'} <= codes(value)


@pytest.mark.parametrize(('table', 'changes', 'expected'), [
    ('orders', {'quantity': 999}, 'ORDER_CONTRACT_MISMATCH'),
    ('reservations', {'cash_fen': 1}, 'RESERVATION_MISMATCH'),
    ('reservations', {'status': 'RELEASED'}, 'RESERVATION_MISMATCH'),
])
def test_actual_order_and_reservation_must_match_frozen_economic_contract(ready_book, table, changes, expected):
    with sqlite3.connect(ready_book.settings.database) as db:
        identity = db.execute("SELECT id FROM orders WHERE account_id='S1' LIMIT 1").fetchone()[0]
    update(ready_book, table, 'S1', identity, lambda row: row.update(changes))
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'BLOCKED', result
    assert expected in codes(result)


def test_missing_proof_is_unknown_and_probe_does_not_rebuild_it(ready_book, monkeypatch):
    import china_stock_sim.ledger.decision_evidence as evidence

    evidence._VALIDATIONS.clear()
    for path in (ready_book.root / 'data/state/verification').glob('*.json'):
        body = json.loads(path.read_bytes())
        if body['verification'].get('kind') == 'frozen_plan_header':
            path.unlink()
    before = tree(ready_book.root)
    monkeypatch.setattr(evidence, 'read_business_document', lambda *a, **k: pytest.fail('no full reads'))
    value = status.build_status(ready_book.settings, NOW)
    assert value['state'] == 'UNKNOWN'
    assert 'PLAN_PROOF_UNAVAILABLE' in codes(value)
    assert tree(ready_book.root) == before


def test_changed_original_dependency_invalidates_green_without_reading_it(ready_book):
    with sqlite3.connect(ready_book.settings.database) as db:
        plan = json.loads(db.execute("SELECT data FROM plans WHERE account_id='S1'").fetchone()[0])
    path = ready_book.root / plan['document']['path']
    path.write_bytes(path.read_bytes() + b' ')
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'UNKNOWN'
    assert 'PLAN_PROOF_UNAVAILABLE' in codes(result)


@pytest.mark.parametrize(('table', 'payload', 'expected'), [
    ('orders', {'target_session': D, 'status': 'WAITING_DATA'}, 'HISTORICAL_ORDERS_PENDING'),
    ('tax_items', {'status': 'WAITING_DATA', 'assessed_fen': None}, 'TAX_OBLIGATION_PENDING'),
    ('entitlements', {'status': 'WAITING_ALLOCATION'}, 'SHARE_ALLOCATION_PENDING'),
    ('corporate_action_cases', {'status': 'WAITING_DATA', 'actions': {}}, 'CORPORATE_ACTION_PENDING'),
])
def test_relevant_pending_economics_block_readiness(ready_book, table, payload, expected):
    insert(ready_book, table, 'pending-extra', payload)
    value = status.build_status(ready_book.settings, NOW)
    assert value['state'] == 'WAITING', value
    assert expected in codes(value)


def test_future_not_due_case_and_active_parent_do_not_block_ready(ready_book):
    insert(ready_book, 'corporate_action_cases', 'future', {'status': 'WAITING_DATA',
           'actions': {'cash_payment': {'state': 'NOT_DUE', 'operation_date': N}}})
    insert(ready_book, 'rebalance_tasks', 'ongoing', {'status': 'ACTIVE', 'end_session': '2026-11-06'})
    assert status.build_status(ready_book.settings, NOW)['state'] == 'READY'


def test_same_snapshot_and_second_boundary_reject_concurrent_change(ready_book, monkeypatch):
    original = status.read_facts
    called = 0

    def changed(*args):
        nonlocal called
        value = original(*args)
        called += 1
        if called == 1:
            update(ready_book, 'account_state', 'S1', None,
                   lambda row: row.update(account_revision=row['account_revision'] + 1))
        return value

    monkeypatch.setattr(status, 'read_facts', changed)
    value = status.build_status(ready_book.settings, NOW)
    assert value['state'] == 'UNKNOWN'
    assert 'INPUT_CHANGED_DURING_PROBE' in codes(value)


def test_proof_reader_uses_one_sqlite_snapshot_for_all_accounts(ready_book, monkeypatch):
    import china_stock_sim.ledger.readiness as reader

    with sqlite3.connect(ready_book.settings.database) as db:
        db.execute('PRAGMA journal_mode=WAL')
    original = reader._plan
    changed = False

    def interleave(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            changed = True
            update(ready_book, 'account_state', 'LLM', None, lambda row: row.update(restate_from=D))
        return result

    monkeypatch.setattr(reader, '_plan', interleave)
    facts = read_facts(ready_book.settings, D, N, NOW)
    assert all(not a['restate_from'] for a in facts['accounts'])
    assert read_facts(ready_book.settings, D, N, NOW)['fingerprint'] != facts['fingerprint']


def test_new_market_fact_waits_for_successful_business_review(ready_book, monkeypatch):
    current = probe.market_version(ready_book.settings, NOW)
    monkeypatch.setattr(probe, 'market_version', lambda *a: {**current, 'latest_at': NOW + timedelta(seconds=1)})
    value = status.build_status(ready_book.settings, NOW + timedelta(seconds=2))
    assert value['state'] == 'WAITING'
    assert 'NEW_FACTS_PENDING_REVIEW' in codes(value)


@pytest.mark.parametrize(('phase', 'tail'), [('backup', True), ('report_plan', True), ('execution', False)])
def test_tail_requires_same_live_process_and_completed_plan_phases(ready_book, monkeypatch, phase, tail):
    journal(ready_book, phase=phase, live=True)
    monkeypatch.setattr(probe, 'probe_process', lambda pid: {'state': 'ALIVE', 'birth': 'isolated-birth'})
    facts = probe.journal_facts(ready_book.settings, D, N, NOW)
    assert facts['tail_safe'] is tail
    monkeypatch.setattr(probe, 'probe_process', lambda pid: {'state': 'ALIVE', 'birth': 'reused-pid'})
    assert probe.journal_facts(ready_book.settings, D, N, NOW)['state'] == 'UNKNOWN'


def test_noncreating_shared_lock_probe_and_ledger_busy_has_no_backup_exception(tmp_path):
    path = tmp_path / 'writer.lock'
    assert probe.lock_fact(path)['state'] == 'FREE' and not path.exists()
    path.touch()
    with path.open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert probe.lock_fact(path)['state'] == 'HELD'
    assert probe.lock_fact(path)['state'] == 'FREE'
    locks = {name: {'state': 'FREE', 'owner': None} for name in ('writer', 'ledger-write', 'market')}
    locks['writer'] = {'state': 'HELD', 'owner': 123,
                       'owner_metadata': {'pid': 123, 'process_birth': 'birth'}}
    journal_value = {'state': 'WORKING', 'tail_safe': True, 'pid': 123, 'birth': 'birth', 'phase': 'backup'}
    value = probe.operational(journal_value, locks, {'active': False})
    assert value['state'] == 'READY' and value['optional_work'] == ['备份仍进行中']
    locks['ledger-write']['state'] = 'HELD'
    assert probe.operational(journal_value, locks, {'active': False})['state'] == 'WORKING'
    locks['ledger-write']['state'] = 'FREE'
    locks['writer']['owner'] = 456
    assert probe.operational(journal_value, locks, {'active': False})['state'] == 'UNKNOWN'


def test_lsof_must_match_published_pid_fd_device_and_inode(monkeypatch, tmp_path):
    def reply(text):
        monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **k:
                            subprocess.CompletedProcess(a[0], 0, text, ''))
    owner = {'pid': 123, 'fd': 7, 'lock_dev': 16, 'lock_ino': 100}
    reply('p123\nf7\ntREG\n')
    assert probe.lock_owner(tmp_path / 'lock', owner) is None
    reply('p123\nf7\nD0x10\ni100\ntREG\n')
    assert probe.lock_owner(tmp_path / 'lock', owner) == 123
    reply('p123\nf9\nD0x10\ni100\ntREG\n')
    assert probe.lock_owner(tmp_path / 'lock', owner) is None


def test_pre19_can_be_green_if_already_done_and_daytime_is_gray():
    window = {'business_session': D, 'target_session': N, 'daytime': False,
              'expires_at': D + 'T18:31:30+08:00'}
    facts = {'accounts': [{'account_id': 'S1', 'state': 'READY', 'reasons': [],
                          'plan': {'status': 'EMPTY', 'order_count': 0}}]}
    at = datetime.fromisoformat(D + 'T18:30:00+08:00')
    assert evaluate(facts, at, window)['state'] == 'READY'
    facts['accounts'][0]['reasons'] = ['NEXT_PLAN_PENDING']
    assert 'WAITING_BUSINESS_WINDOW' in codes(evaluate(facts, at, window))
    value = evaluate(facts, at, {**window, 'daytime': True})
    assert value['state'] == 'UNKNOWN' and value['label'] == '待晚间'


def test_maintenance_and_report_revision_are_separate_from_financial_readiness(ready_book):
    book = ready_book
    update(book, 'account_state', 'S1', None, lambda row: row.update(last_rendered_revision=0))
    assert status.build_status(book.settings, NOW)['state'] == 'READY'
    (book.root / 'data/state/dashboard-maintenance.json').write_text('{"active": true}')
    result = status.build_status(book.settings, NOW)
    assert result['state'] == 'UNKNOWN' and 'MAINTENANCE_ACTIVE' in codes(result)


def test_ledger_and_lock_symlinks_are_not_followed(ready_book, tmp_path):
    lock = ready_book.root / 'data/state/market.lock'
    if lock.exists():
        lock.unlink()
    destination = tmp_path / 'outside'
    destination.write_text('unchanged')
    lock.symlink_to(destination)
    assert status.build_status(ready_book.settings, NOW)['state'] == 'UNKNOWN'
    assert destination.read_text() == 'unchanged'


def test_permission_and_probe_failure_do_not_expose_exception_text(settings, monkeypatch):
    monkeypatch.setattr(probe, 'market_version', lambda *a: (_ for _ in ()).throw(
        OSError('do-not-publish-this-credential')))
    result = status.build_status(settings, NOW)
    assert result['state'] == 'UNKNOWN'
    assert 'credential' not in json.dumps(result)
    assert result['diagnostic']['stack'] and all(set(frame) == {'file', 'line', 'function'}
                                               for frame in result['diagnostic']['stack'])


def test_later_reused_plan_cannot_hide_failed_input_review(ready_book):
    log = journal(ready_book, live=True)
    later = NOW + timedelta(seconds=1)
    log.phase('isolated-readiness', 'input_review', now=later, state='WAITING', session=D,
              target_session=N, account_id='S1', result={'state': 'WAITING_DATA'})
    log.phase('isolated-readiness', 'plan', now=later, state='SKIPPED', session=D,
              target_session=N, account_id='S1', result={'state': 'SKIPPED', 'reason': 'FROZEN_PLAN_EXISTS'})
    log.finish('isolated-readiness', now=later, state='PARTIAL', result={'state': 'PARTIAL'})
    value = status.build_status(ready_book.settings, later)
    assert value['state'] == 'WAITING'
    assert 'BUSINESS_REVIEW_UNAVAILABLE' in codes(value)
    # Successful no-change review is sufficient; no financial revision is needed.
    log.start('review-recovery', now=later, metadata={})
    for name in ('input_review', 'plan'):
        log.phase('review-recovery', name, now=later + timedelta(seconds=1), state='RUNNING',
                  session=D, target_session=N, account_id='S1')
        log.phase('review-recovery', name, now=later + timedelta(seconds=1), state='COMPLETE',
                  session=D, target_session=N, account_id='S1', result={'state': 'ALREADY_APPLIED'})
    log.finish('review-recovery', now=later + timedelta(seconds=1), state='COMPLETE', result={'state': 'COMPLETE'})
    assert status.build_status(ready_book.settings, later + timedelta(seconds=1))['state'] == 'READY'


def test_market_visibility_cutoff_is_utc_not_shanghai_text(ready_book):
    with sqlite3.connect(ready_book.root / 'data/state/market.sqlite3') as db:
        db.execute("UPDATE observations SET available_at='2026-10-30T10:00:00+00:00'")
        row = db.execute('SELECT rowid FROM observations LIMIT 1').fetchone()[0]
        db.execute("UPDATE observations SET available_at='2026-10-30T14:00:00+00:00' WHERE rowid=?", (row,))
    facts = probe.market_version(ready_book.settings, NOW)
    assert facts['latest_at'].isoformat() == '2026-10-30T18:00:00+08:00'


def test_daytime_has_three_not_yet_checked_account_rows(ready_book):
    value = status.build_status(ready_book.settings, datetime.fromisoformat('2026-11-02T12:00:00+08:00'))
    assert value['state'] == 'UNKNOWN' and value['label'] == '待晚间'
    assert (value['business_session'], value['target_session']) == ('2026-11-02', '2026-11-03')
    assert len(value['accounts']) == 3 and all(not a['ready'] for a in value['accounts'])
    assert all(a['summary'] == '今晚尚未开始核验' for a in value['accounts'])


@pytest.mark.parametrize(('due', 'expected'), [(N, 'READY'), ('2026-11-05', 'READY'),
                                             (None, 'READY'), (D, 'WAITING')])
def test_known_tax_only_blocks_when_due_for_business_day(ready_book, due, expected):
    insert(ready_book, 'tax_items', 'known-future-tax', {'status': 'PAYABLE',
           'assessed_fen': 100, 'paid_fen': 0, 'due_date': due})
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == expected, result
    assert ('TAX_OBLIGATION_PENDING' in codes(result)) is (expected == 'WAITING')


def test_future_nontrading_day_action_does_not_invent_new_blocker(ready_book):
    insert(ready_book, 'corporate_action_cases', 'saturday-action', {'status': 'WAITING_DATA',
           'actions': {'cash_payment': {'state': 'NOT_DUE', 'operation_date': '2026-10-31'}}})
    assert status.build_status(ready_book.settings, NOW)['state'] == 'READY'
    update(ready_book, 'corporate_action_cases', 'S1', 'saturday-action', lambda row:
           row['actions']['cash_payment'].update(operation_date=D))
    assert 'CORPORATE_ACTION_PENDING' in codes(status.build_status(ready_book.settings, NOW))


def test_due_entitlement_cannot_hide_behind_old_final_statement(ready_book):
    insert(ready_book, 'entitlements', 'unpaid-dividend',
           {'kind': 'CASH', 'status': 'RECEIVABLE', 'payment_date': D})
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'WAITING' and 'DUE_ENTITLEMENT_PENDING' in codes(result)


def test_valid_frozen_plan_cannot_override_current_unfunded_reservation(ready_book):
    with sqlite3.connect(ready_book.settings.database) as db:
        db.execute("UPDATE cash_entries SET amount_fen=1 WHERE account_id='S1'")
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'BLOCKED' and 'CASH_RESERVATION_UNFUNDED' in codes(result)


def test_authoritative_policy_cannot_be_relabelled_under_valid_frozen_proof(ready_book):
    with sqlite3.connect(ready_book.settings.database) as db:
        identity = db.execute("SELECT id FROM account_policies WHERE account_id='S1' LIMIT 1").fetchone()[0]
    update(ready_book, 'account_policies', 'S1', identity,
           lambda row: row['definition'].update(implementation_hash='0' * 64))
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'BLOCKED' and 'POLICY_PLAN_MISMATCH' in codes(result)


def test_next_day_pending_order_cannot_coexist_with_future_fill(ready_book):
    insert(ready_book, 'fills', 'future-fill', {'target_session': N})
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'BLOCKED' and 'FUTURE_FILL_PRESENT' in codes(result)


@pytest.mark.parametrize(('kind', 'payment', 'listing'), [('CASH', N, D), ('SHARES', D, N)])
def test_entitlement_only_uses_its_own_economic_due_date(ready_book, kind, payment, listing):
    insert(ready_book, 'entitlements', 'future-own-kind', {'kind': kind, 'status': 'RECEIVABLE',
           'payment_date': payment, 'listing_date': listing})
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'READY', result


def test_changed_original_account_identifier_is_rejected_before_read(settings):
    altered = replace(settings, accounts=(replace(settings.accounts[0], id='renamed'), *settings.accounts[1:]))
    result = status.build_status(altered, NOW)
    assert result['state'] == 'UNKNOWN' and 'ACCOUNT_CONFIGURATION_INVALID' in codes(result)


def test_calendar_source_change_between_boundary_reads_invalidates_result(ready_book, monkeypatch):
    original = status.read_calendar

    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        path = ready_book.root / value.dependencies[0][0]
        path.write_bytes(path.read_bytes() + b' ')
        return value

    monkeypatch.setattr(status, 'read_calendar', changed)
    result = status.build_status(ready_book.settings, NOW)
    assert result['state'] == 'UNKNOWN' and 'INPUT_CHANGED_DURING_PROBE' in codes(result)


def test_fact_arriving_inside_successful_review_is_not_falsely_covered(ready_book, monkeypatch):
    log = journal(ready_book, live=True)
    start, middle, end = (NOW + timedelta(seconds=i) for i in (1, 2, 3))
    log.phase('isolated-readiness', 'input_review', now=start, state='RUNNING',
              session=D, target_session=N, account_id='S1')
    log.phase('isolated-readiness', 'input_review', now=end, state='COMPLETE',
              session=D, target_session=N, account_id='S1', result={'state': 'ALREADY_APPLIED'})
    log.phase('isolated-readiness', 'plan', now=end, state='RUNNING',
              session=D, target_session=N, account_id='S1')
    log.phase('isolated-readiness', 'plan', now=end, state='SKIPPED', session=D, target_session=N,
              account_id='S1', result={'state': 'SKIPPED', 'reason': 'FROZEN_PLAN_EXISTS'})
    log.finish('isolated-readiness', now=end, state='COMPLETE', result={'state': 'COMPLETE'})
    market = probe.market_version(ready_book.settings, end)
    monkeypatch.setattr(probe, 'market_version', lambda *a: {**market, 'latest_at': middle})
    facts = probe.journal_facts(ready_book.settings, D, N, end)
    assert facts['reviews']['S1'] == start
    result = status.build_status(ready_book.settings, end)
    assert result['state'] == 'WAITING' and 'NEW_FACTS_PENDING_REVIEW' in codes(result)
