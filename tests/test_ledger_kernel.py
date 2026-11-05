"""A01/A16/A17/A19: migration, transactional cash, crash recovery and evidence backups."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from china_stock_sim.ledger.kernel import LedgerError, LedgerKernel, digest, ledger_lock
from china_stock_sim.ledger.maintenance import MaintenanceMixin
from china_stock_sim.ledger.store import initialize, read_snapshot
from china_stock_sim.market.contracts import DataResult

NOW = datetime(2026, 10, 29, 10, tzinfo=timezone.utc)
DAY = '2026-10-29'


class Ledger(MaintenanceMixin, LedgerKernel):
    def _verify_evidence(self, value, required=True):
        if required and value.get('evidence') != 'verified-fixture':
            raise LedgerError('WAITING_DATA', 'missing evidence')


@pytest.fixture
def book(settings):
    initialize(settings)
    return Ledger(settings, clock=lambda: NOW, test_mode=True)


def write(book, key='one', revision=0, amount=100):
    def action(tx):
        tx.cash(f'cash:{key}', 'TEST_ADJUSTMENT', amount, DAY)
        tx.append('job_runs', key, {'state': 'COMPLETE'})
        return {'state': 'APPLIED'}
    return book._mutate('S1', key, {'amount': amount}, revision, DAY, action)


def legacy(settings):
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.database)) as db:
        db.executescript('''
            CREATE TABLE accounts(account_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,
                currency TEXT NOT NULL,initial_cash_fen INTEGER NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE cash_entries(entry_id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(account_id),
                kind TEXT NOT NULL CHECK(kind='INITIAL_CAPITAL'),amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
                recorded_at TEXT NOT NULL,UNIQUE(account_id,kind));
            PRAGMA user_version=1;
        ''')
        for spec in settings.accounts:
            db.execute('INSERT INTO accounts VALUES (?,?,?,?,?)',
                       (spec.id, spec.strategy, 'CNY', spec.initial_cash_fen, '2026-10-28T15:00:00+08:00'))
            db.execute('INSERT INTO cash_entries VALUES (?,?,?,?,?)',
                       ('original-'+spec.id, spec.id, 'INITIAL_CAPITAL', spec.initial_cash_fen, '2026-10-28T15:00:00+08:00'))
        db.commit()


def test_legacy_reads_and_init_do_not_implicitly_migrate(settings):
    legacy(settings)
    before = settings.database.read_bytes()
    assert read_snapshot(settings)['schema_version'] == 1
    assert initialize(settings)['created_accounts'] == 0
    assert settings.database.read_bytes() == before


def test_explicit_migration_preserves_all_original_identity_amounts_and_times(settings):
    legacy(settings)
    original = read_snapshot(settings)
    book = Ledger(settings)
    result = book.migrate()
    assert result['state'] == 'APPLIED'
    current = read_snapshot(settings)
    for before, after in zip(original['accounts'], current['accounts'], strict=True):
        assert all(after[key] == value for key, value in before.items())
    with closing(sqlite3.connect(settings.database)) as db:
        assert {r[0] for r in db.execute('SELECT entry_id FROM cash_entries')} == {'original-S1', 'original-S1-LT', 'original-LLM'}
    assert book.migrate()['state'] == 'ALREADY_APPLIED'
    assert initialize(settings)['created_accounts'] == 0
    assert len(list((settings.root/'data/backups/ledger').glob('migration-*/manifest.json'))) == 1


def test_migration_failure_rolls_back_and_preserves_pre_migration_backup(settings, monkeypatch):
    legacy(settings)
    before = settings.database.read_bytes()
    from china_stock_sim.ledger import maintenance
    original = maintenance.create_schema
    def fail(db, **kwargs):
        original(db, **kwargs)
        raise RuntimeError('power lost')
    monkeypatch.setattr(maintenance, 'create_schema', fail)
    with pytest.raises(RuntimeError, match='power lost'):
        Ledger(settings).migrate()
    assert settings.database.read_bytes() == before
    assert read_snapshot(settings)['schema_version'] == 1
    assert list((settings.root/'data/backups/ledger').glob('migration-*/manifest.json'))


def test_unknown_schema_is_not_migrated_or_reset(book):
    with closing(sqlite3.connect(book.settings.database)) as db:
        db.execute('PRAGMA user_version=99')
    before = book.settings.database.read_bytes()
    with pytest.raises(ValueError, match='版本'):
        book.migrate()
    assert book.settings.database.read_bytes() == before


def test_same_operation_is_exactly_once_before_revision_check(book):
    first = write(book)
    assert first['state'] == 'APPLIED'
    again = write(book, revision=0)
    assert again['state'] == 'ALREADY_APPLIED' and again['commit_id'] == first['commit_id']
    assert book._read('S1', lambda tx: tx.balance()) == 5000100
    assert write(book, amount=101)['state'] == 'CONFLICT'
    assert write(book, key='different', revision=0)['state'] == 'STALE_ACCOUNT'


@pytest.mark.parametrize('failure_stage', [0, 1, 2])
def test_fault_in_each_transaction_step_has_no_cash_record_or_revision_side_effect(book, failure_stage):
    before = book.settings.database.read_bytes()
    def fail(tx):
        if failure_stage == 0:
            raise RuntimeError('crash')
        tx.cash('pending', 'TEST', -200, DAY)
        if failure_stage == 1:
            raise RuntimeError('crash')
        tx.append('fills', 'pending', {'order_id': 'order'})
        raise RuntimeError('crash')
    with pytest.raises(RuntimeError, match='crash'):
        book._mutate('S1', 'crash', {}, 0, DAY, fail)
    assert book.settings.database.read_bytes() == before
    assert book._read('S1', lambda tx: tx.list('cash_entries'))[0]['amount_fen'] == 5000000


def test_waiting_attempt_does_not_prevent_new_terminal_operation(book):
    wait = book._mutate('S1', 'wait:1', {}, 0, DAY,
                        lambda tx: {'state': 'WAITING_DATA', 'reason': 'missing'})
    assert wait['state'] == 'WAITING_DATA'
    assert write(book, key='terminal', revision=1)['state'] == 'APPLIED'
    assert book._read('S1', lambda tx: tx.state)['account_revision'] == 2


def test_metadata_uses_report_revision_without_changing_money_revision(book):
    report = book._mutate('S1', 'failure', {}, 0, DAY,
                          lambda tx: {'state': 'APPLIED'}, financial=False)
    assert report['account_revision'] == 0 and report['report_revision'] == 1
    def acknowledge(tx):
        tx.state['last_rendered_revision'] = tx.state['report_revision']
    book._mutate('S1', 'published', {}, 0, DAY, acknowledge, financial=False, report=False)
    assert book._read('S1', lambda tx: tx.state)['report_revision'] == 1


def test_read_is_consistent_and_cannot_write_or_create_missing_database(book, settings, tmp_path):
    before = book.settings.database.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        book._read('S1', lambda tx: tx.save('job_runs', 'bad', {}))
    assert book.settings.database.read_bytes() == before
    from dataclasses import replace
    missing = replace(settings, database=tmp_path/'absent.sqlite3')
    with pytest.raises(FileNotFoundError):
        Ledger(missing)._read('S1', lambda tx: tx.balance())
    assert not missing.database.exists()


def test_single_writer_lock_releases_after_failure(book):
    with ledger_lock(book.root):
        assert write(book)['state'] == 'CONFLICT'
    assert write(book)['state'] == 'APPLIED'


def test_clock_regression_and_unsafe_test_clock_rejected(book):
    write(book)
    earlier = Ledger(book.settings, clock=lambda: datetime(2026, 10, 28, tzinfo=timezone.utc), test_mode=True)
    assert write(earlier, key='past', revision=1)['state'] == 'INTEGRITY_ERROR'
    with pytest.raises(ValueError, match='测试时钟'):
        Ledger(book.settings, clock=lambda: NOW)


def test_cross_account_and_float_payload_rejected_without_cash_changes(book):
    assert book._mutate('S1', 'float', {'money': 1.1}, 0, DAY, lambda tx: {})['state'] == 'INVALID_INPUT'
    def wrong(tx):
        tx.append('fills', 'wrong', {'account_id': 'LLM'})
    assert book._mutate('S1', 'cross', {}, 0, DAY, wrong)['state'] == 'INVALID_INPUT'
    assert book._read('LLM', lambda tx: tx.balance()) == 5000000
    assert write(book, amount=-5000001)['state'] == 'CONFLICT'
    assert book._read('S1', lambda tx: tx.balance()) == 5000000


def test_backup_restore_and_corrupt_backup_rejection(book, tmp_path):
    write(book)
    result = book.backup()
    restored = tmp_path/'recovered'
    assert book.restore_backup(result['backup_id'], restored)['state'] == 'RESTORED'
    from china_stock_sim.config import load_settings
    assert read_snapshot(load_settings(restored))['accounts'][1]['cash_fen'] == 5000100
    with pytest.raises(ValueError, match='隔离'):
        book.restore_backup(result['backup_id'], book.root)
    with pytest.raises(ValueError, match='为空'):
        book.restore_backup(result['backup_id'], restored)
    path = book.root/'data/backups/ledger'/result['backup_id']/'ledger.sqlite3'
    path.write_bytes(b'broken')
    with pytest.raises(ValueError, match='摘要'):
        book.restore_backup(result['backup_id'], tmp_path/'corrupt')


def test_backup_failure_retains_previous_success(book, monkeypatch):
    successful = book.backup()
    original = sqlite3.connect
    def fail(path, *args, **kwargs):
        if isinstance(path, Path) and 'backups' in path.parts:
            raise OSError('disk full')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', fail)
    with pytest.raises(OSError, match='disk full'):
        book.backup()
    assert (book.root/'data/backups/ledger'/successful['backup_id']/'manifest.json').is_file()
    assert book._read('S1', lambda tx: tx.balance()) == 5000000


def test_missing_evidence_is_visible_not_a_complete_audit(book):
    book._mutate('S1', 'evidence', {}, 0, DAY, lambda tx: tx.append('job_runs', 'e', {
        'response_path': 'data/plans/missing.json', 'market_snapshot_ids': ['a'*64]}))
    audit = book.audit_status()
    assert audit['state'] == 'PARTIAL'
    assert len(audit['evidence']['issues']) == 2
    assert book.backup()['state'] == 'PARTIAL'


def test_maintenance_correction_appends_and_preserves_original(book):
    write(book, amount=100)
    correction = {'correction_id': 'adjust', 'kind': 'CASH_ADJUSTMENT', 'effective_date': DAY,
        'reference_id': 'cash:one', 'old_amount_fen': 100, 'new_amount_fen': 150,
        'reason': 'confirmed source change', 'evidence': 'verified-fixture'}
    result = book.apply_correction('S1', correction, operation_id='correction', expected_revision=1)
    assert result['state'] == 'APPLIED'
    assert book._read('S1', lambda tx: tx.get('cash_entries', 'cash:one'))['amount_fen'] == 100
    assert book._read('S1', lambda tx: tx.balance()) == 5000150
    assert book.apply_correction('S1', correction, operation_id='correction', expected_revision=0)['state'] == 'ALREADY_APPLIED'
    unsupported = {**correction, 'kind': 'REVERSE_TRADE'}
    assert book.apply_correction('S1', unsupported, operation_id='unsafe', expected_revision=2)['state'] == 'CONFLICT'


def test_correction_cannot_invalidate_already_frozen_plan(book):
    write(book, amount=100)
    book._mutate('S1', 'freeze', {}, 1, DAY,
        lambda tx: tx.append('plans', 'p', {'target_session': DAY, 'state': 'FROZEN'}))
    result = book.apply_correction('S1', {'correction_id': 'c', 'kind': 'CASH_ADJUSTMENT',
        'effective_date': DAY, 'reference_id': 'cash:one', 'old_amount_fen': 100,
        'new_amount_fen': 150, 'reason': 'change', 'evidence': 'verified-fixture'},
        operation_id='c', expected_revision=2)
    assert result['state'] == 'CONFLICT'
    assert book._read('S1', lambda tx: tx.balance()) == 5000100


def test_payload_hash_is_stable_and_float_is_forbidden():
    assert digest({'b': 2, 'a': 1}) == digest({'a': 1, 'b': 2})
    with pytest.raises(LedgerError):
        digest({'money': 1.0})


class Calendar:
    def calendar_operation(self, *args):
        return DataResult('READY', '2026-10-28')


def test_correction_flags_statement_revisions_without_overwriting_old_statement(book):
    book.market = Calendar()
    def prepare(tx):
        tx.cash('old', 'TEST', 100, DAY)
        tx.append('daily_nav', 'old-statement', {'session': DAY, 'statement_revision': 1})
        tx.state['settled_through'] = DAY
    book._mutate('S1', 'prepare', {}, 0, DAY, prepare)
    correction = {'correction_id': 'fix', 'kind': 'CASH_ADJUSTMENT', 'effective_date': DAY,
        'reference_id': 'old', 'old_amount_fen': 100, 'new_amount_fen': 200,
        'reason': 'revision', 'evidence': 'verified-fixture'}
    assert book.apply_correction('S1', correction, operation_id='fix', expected_revision=1)['state'] == 'APPLIED'
    assert book._read('S1', lambda tx: tx.list('daily_nav'))[0]['statement_revision'] == 1
    assert book._read('S1', lambda tx: tx.list('job_runs'))[0]['kind'] == 'RESTATE_SESSION'
    assert book._read('S1', lambda tx: tx.state)['settled_through'] == '2026-10-28'


def test_business_duplicate_does_not_increase_revisions_or_persist_mutations(book):
    def duplicate(tx):
        tx.cash('must-rollback', 'TEST', 1, DAY)
        return {'state': 'ALREADY_APPLIED', 'plan_id': 'existing'}
    result = book._mutate('S1', 'alias-operation', {}, 0, DAY, duplicate)
    assert result['state'] == 'ALREADY_APPLIED'
    assert result['account_revision'] == result['report_revision'] == 0
    assert book._read('S1', lambda tx: tx.balance()) == 5000000


@pytest.mark.parametrize('field', ['quantity', 'cost_fen'])
def test_negative_lot_invariant_rolls_back_even_nontrading_writers(book, field):
    result = book._mutate('S1', 'negative', {}, 0, DAY,
        lambda tx: tx.append('position_lots', 'bad-lot', {'quantity': 1, 'cost_fen': 1, field: -1}))
    assert result['state'] == 'INTEGRITY_ERROR'
    assert book._read('S1', lambda tx: tx.list('position_lots')) == []


def test_frozen_at_is_server_metadata_not_idempotency_business_input(book):
    first = book._mutate('S1', 'clock-metadata', {'frozen_at': 'ignored'}, 0, DAY,
                         lambda tx: {'state': 'APPLIED'})
    second = book._mutate('S1', 'clock-metadata', {'frozen_at': 'also ignored'}, 0, DAY,
                          lambda tx: {'state': 'APPLIED'})
    assert second['state'] == 'ALREADY_APPLIED'
    assert first['commit_id'] == second['commit_id']


def test_new_correction_id_cannot_repeat_an_already_applied_adjustment(book):
    write(book, amount=100)
    correction = {'correction_id': 'adjust', 'kind': 'CASH_ADJUSTMENT', 'effective_date': DAY,
        'reference_id': 'cash:one', 'old_amount_fen': 100, 'new_amount_fen': 150,
        'reason': 'source change', 'evidence': 'verified-fixture'}
    assert book.apply_correction('S1', correction, operation_id='c', expected_revision=1)['state'] == 'APPLIED'
    assert book.apply_correction('S1', {**correction, 'correction_id': 'duplicate'},
                                 operation_id='d', expected_revision=2)['state'] == 'CONFLICT'
    assert book._read('S1', lambda tx: tx.balance()) == 5000150


def test_market_manifest_is_copied_and_query_only_evidence_remains_visible(book, tmp_path):
    from china_stock_sim.market.repository import MarketRepository
    from china_stock_sim.market.service import MarketDataService
    repo = MarketRepository(book.root)
    ids = repo.put('calendar', [{'date': DAY, 'is_trading_day': True}],
                   {'provider': 'fixture', 'fetched_at': '2026-10-29T09:00:00+08:00'})
    book.market = MarketDataService(book.root)
    book._mutate('S1', 'query', {}, 0, DAY,
        lambda tx: tx.append('job_runs', 'query', {'data_evidence': [{'result': {'observation_ids': ids}}]}))
    backup = book.backup()
    assert backup['state'] == 'COMPLETE'
    market_files = [item for item in backup['evidence']['files']
                    if not item['path'].startswith('data/plans/')]
    assert len(market_files) == 1
    file = market_files[0]['path']
    assert (book.root/'data/backups/ledger'/backup['backup_id']/'evidence'/file).exists()
    result = book.restore_backup(backup['backup_id'], tmp_path/'query-restore')
    assert result['state'] == 'RESTORED_AUDIT_PARTIAL'
    assert result['market_archive_required'] is True
    assert (tmp_path/'query-restore'/file).exists()


def test_confirmed_records_cannot_be_silently_overwritten(book):
    book._mutate('S1', 'save', {}, 0, DAY,
                 lambda tx: tx.append('plans', 'frozen', {'target_session': DAY, 'quantity': 100}))
    result = book._mutate('S1', 'replace', {}, 1, DAY,
        lambda tx: tx.save('plans', 'frozen', {'target_session': DAY, 'quantity': 200}))
    assert result['state'] == 'CONFLICT'
    assert book._read('S1', lambda tx: tx.get('plans', 'frozen'))['quantity'] == 100


def test_account_scoped_journal_ids_do_not_couple_independent_accounts(book):
    def action(tx):
        tx.cash('same-business-id', 'TEST', 10, DAY)
    one = book._mutate('S1', 'same', {}, 0, DAY, action)
    two = book._mutate('LLM', 'same', {}, 0, DAY, action)
    assert one['state'] == two['state'] == 'APPLIED'
    assert book._read('S1', lambda tx: tx.balance()) == book._read('LLM', lambda tx: tx.balance()) == 5000010


def test_crashed_other_process_releases_writer_lock(book):
    import subprocess
    import sys
    code = ('import sys; from pathlib import Path; '
            'from china_stock_sim.ledger.kernel import ledger_lock; '
            'lock=ledger_lock(Path(sys.argv[1])); lock.__enter__(); '
            'print("locked",flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', code, str(book.root)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        assert write(book)['state'] == 'CONFLICT'
        child.kill()
        child.wait(timeout=5)
        assert write(book)['state'] == 'APPLIED'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stdin.close()


def test_daily_backup_retention_keeps_thirty_and_migration_backups(book):
    with ledger_lock(book.root):
        migration = book._backup_locked('migration')
    for _ in range(32):
        book.backup()
    parent = book.root/'data/backups/ledger'
    assert len(list(parent.glob('daily-*/manifest.json'))) == 30
    assert (parent/migration['backup_id']/'manifest.json').exists()


def test_public_trade_fee_correction_reissues_statement_and_backup_keeps_document(settings, monkeypatch):
    from test_ledger_trading import DAY as trade_day
    from test_ledger_trading import Facts, buy_and_close, call

    from china_stock_sim.ledger import LedgerService
    current = [datetime.fromisoformat('2026-10-29T20:00:00+08:00')]
    market = Facts()
    service = LedgerService(settings, market=market, clock=lambda: current[0], test_mode=True)
    service.test_now = current
    service.initialize()
    assert call(service, 'activate_simulation', {'target_session': trade_day})['state'] == 'APPLIED'
    plan = buy_and_close(service)
    old_statement = service.daily_statement('LLM', trade_day)
    order_id = plan['order_ids'][0]
    correction = {'correction_id': 'correct-known-fee', 'kind': 'FEE_ADJUSTMENT',
        'effective_date': trade_day, 'reference_id': f'commission_fen:{order_id}',
        'old_amount_fen': -500, 'new_amount_fen': -400, 'reason': 'confirmed fee refund',
        'knowledge_cutoff': service._now().isoformat(),
        'market_snapshot_ids': [market.snapshot('SH600000', trade_day, service._now().isoformat())]}
    result = call(service, 'apply_correction', correction)
    assert result['state'] == 'APPLIED', result
    assert service.account_state('LLM', '2026-11-02')['cash_fen'] == 3999590
    close = call(service, 'close_session', {'session': trade_day, 'knowledge_cutoff': service._now().isoformat()})
    assert close['state'] == 'APPLIED', close
    updated = service.daily_statement('LLM', trade_day)
    assert updated['statement_revision'] == old_statement['statement_revision'] + 1
    assert updated['net_assets_fen'] == old_statement['net_assets_fen'] + 100
    assert updated['pnl_decomposition_fen'] == updated['cumulative_pnl_fen']
    assert service.history('LLM', include_revisions=True)['total'] == 2
    # The fixture intentionally has no raw file exporter: backup must be partial, not pretend complete.
    manifest = service.backup()
    assert manifest['state'] == 'PARTIAL'
    plan_files = [item for item in manifest['evidence']['files'] if item['path'].startswith('data/plans/')]
    assert plan_files


def test_backup_refuses_tampered_business_document_even_when_it_still_exists(book):
    from china_stock_sim.ledger.evidence import document
    proof = document(book.root, 'tests', {'response': 'original'})
    book._mutate('S1', 'proof', {}, 0, DAY,
                 lambda tx: tx.append('job_runs', 'proof', {'evidence_file': proof}))
    assert book.backup()['state'] == 'COMPLETE'
    (book.root/proof['path']).write_text('{"response":"changed"}')
    result = book.backup()
    assert result['state'] == 'PARTIAL'
    assert proof['path'] not in {item['path'] for item in result['evidence']['files']}
    assert result['evidence']['issues'][0]['kind'] == 'MISSING_EVIDENCE_FILE'


def test_fifo_fee_refund_allocates_sold_and_held_cost_without_changing_fills(settings):
    from test_ledger_trading import DAY as trade_day
    from test_ledger_trading import Facts, buy_and_close, call, execute_payload, plan_payload

    from china_stock_sim.ledger import LedgerService
    now = [datetime.fromisoformat('2026-10-29T20:00:00+08:00')]
    market = Facts()
    service = LedgerService(settings, market=market, clock=lambda: now[0], test_mode=True)
    service.test_now = now
    service.initialize()
    call(service, 'activate_simulation', {'target_session': trade_day})
    buy_plan = buy_and_close(service)
    sell = plan_payload(service, '2026-11-02', [{'code': 'SH600000', 'side': 'SELL', 'quantity': 400,
                                               'limit_price': '10.30', 'reason': 'fixture'}])
    assert call(service, 'freeze_plan', sell)['state'] == 'APPLIED'
    plan = service.get_plan('LLM', sell['plan_id'])
    now[0] = datetime.fromisoformat('2026-11-02T20:00:00+08:00')
    assert call(service, 'apply_execution_results', execute_payload(service, plan, plan['orders'][0], price='10.30'))['state'] == 'APPLIED'
    assert call(service, 'close_session', {'session': '2026-11-02', 'knowledge_cutoff': service._now().isoformat()})['state'] == 'APPLIED'
    original_fills = service.fills('LLM')['rows']
    original_cost = service.positions('LLM', '2026-11-02')[0]['cost_fen']
    correction = {'correction_id': 'fifo-fee-refund', 'kind': 'FEE_ADJUSTMENT',
        'effective_date': trade_day, 'reference_id': f'commission_fen:{buy_plan["order_ids"][0]}',
        'old_amount_fen': -500, 'new_amount_fen': -400, 'reason': 'confirmed fee refund',
        'knowledge_cutoff': service._now().isoformat(),
        'market_snapshot_ids': [market.snapshot('SH600000', trade_day, service._now().isoformat())]}
    assert call(service, 'apply_correction', correction)['state'] == 'APPLIED'
    assert service.positions('LLM', '2026-11-02')[0]['cost_fen'] == original_cost - 60
    assert service.fills('LLM')['rows'] == original_fills
    corrections = service._read('LLM', lambda tx: tx.list('corrections'))
    assert sum(c.get('realized_cost_adjustment_fen', 0) for c in corrections) == 40
    for day in (trade_day, '2026-11-02'):
        closed = call(service, 'close_session', {'session': day, 'knowledge_cutoff': service._now().isoformat()})
        assert closed['state'] == 'APPLIED', closed
        statement = service.daily_statement('LLM', day)
        assert statement['pnl_decomposition_fen'] == statement['cumulative_pnl_fen']
    assert service.daily_statement('LLM', '2026-11-02')['cumulative_transaction_fees_fen'] == sum(f['fees_fen'] for f in original_fills) - 100


def test_complex_share_cost_chain_rejects_fee_correction_atomically(book):
    def seed(tx):
        tx.cash('commission:buy', 'COMMISSION_FEN', -500, DAY, 'buy')
        tx.append('fills', 'buy', {'order_id': 'buy', 'side': 'BUY'})
        tx.append('position_lots', 'buy:buy', {'lot_id': 'buy:buy', 'quantity': 100, 'cost_fen': 100500})
        tx.append('lot_movements', 'bonus', {'lot_id': 'buy:buy', 'kind': 'SHARE_COST_TRANSFER'})
    book._mutate('S1', 'seed', {}, 0, DAY, seed)
    before = book.settings.database.read_bytes()
    result = book.apply_correction('S1', {'correction_id': 'complex', 'kind': 'FEE_ADJUSTMENT',
        'effective_date': DAY, 'reference_id': 'commission:buy', 'old_amount_fen': -500,
        'new_amount_fen': -400, 'reason': 'fee change', 'evidence': 'verified-fixture'},
        operation_id='complex', expected_revision=1)
    assert result['state'] == 'CONFLICT'
    assert book.settings.database.read_bytes() == before


def test_sell_fee_correction_changes_realized_pnl_without_changing_sold_lot(settings):
    from test_ledger_trading import DAY as trade_day
    from test_ledger_trading import Facts, buy_and_close, call, execute_payload, plan_payload

    from china_stock_sim.ledger import LedgerService
    now = [datetime.fromisoformat('2026-10-29T20:00:00+08:00')]
    market = Facts()
    service = LedgerService(settings, market=market, clock=lambda: now[0], test_mode=True)
    service.test_now = now
    service.initialize()
    call(service, 'activate_simulation', {'target_session': trade_day})
    buy_and_close(service)
    sell = plan_payload(service, '2026-11-02', [{'code': 'SH600000', 'side': 'SELL', 'quantity': 1000,
                                               'limit_price': '10.30', 'reason': 'fixture'}])
    assert call(service, 'freeze_plan', sell)['state'] == 'APPLIED'
    plan = service.get_plan('LLM', sell['plan_id'])
    now[0] = datetime.fromisoformat('2026-11-02T20:00:00+08:00')
    assert call(service, 'apply_execution_results', execute_payload(service, plan, plan['orders'][0], price='10.30'))['state'] == 'APPLIED'
    assert call(service, 'close_session', {'session': '2026-11-02', 'knowledge_cutoff': service._now().isoformat()})['state'] == 'APPLIED'
    previous = service.daily_statement('LLM', '2026-11-02')
    correction = {'correction_id': 'sell-fee-refund', 'kind': 'FEE_ADJUSTMENT',
        'effective_date': '2026-11-02', 'reference_id': f'commission_fen:{plan["order_ids"][0]}',
        'old_amount_fen': -500, 'new_amount_fen': -400, 'reason': 'confirmed refund',
        'knowledge_cutoff': service._now().isoformat(),
        'market_snapshot_ids': [market.snapshot('SH600000', '2026-11-02', service._now().isoformat())]}
    assert call(service, 'apply_correction', correction)['state'] == 'APPLIED'
    assert call(service, 'close_session', {'session': '2026-11-02', 'knowledge_cutoff': service._now().isoformat()})['state'] == 'APPLIED'
    current = service.daily_statement('LLM', '2026-11-02')
    assert current['realized_pnl_fen'] == previous['realized_pnl_fen'] + 100
    assert current['net_assets_fen'] == previous['net_assets_fen'] + 100
    assert current['cumulative_transaction_fees_fen'] == previous['cumulative_transaction_fees_fen'] - 100
    assert not service.positions('LLM', '2026-11-02')


@pytest.mark.parametrize('payload', [
    {'api_key': 'fixture-not-a-real-key'},
    {'Authorization': 'fixture-not-a-real-token'},
    {'nested': [{'password': 'fixture-password'}]},
    {'reason': 'example sk-' + 'fixture_not_a_real_key'},
    {'reason': 'Authorization: Bearer fixture-only'},
    {'reason': '{"api_key":"fixture-only"}'},
])
def test_kernel_rejects_credentials_before_sql_or_log_write(book, payload):
    before = {str(p): p.read_bytes() for p in book.root.rglob('*') if p.is_file()}
    result = book._mutate('S1', 'credentials', payload, 0, DAY,
                          lambda tx: tx.append('job_runs', 'secret', payload))
    assert result['state'] == 'INVALID_INPUT'
    assert 'fixture' not in str(result)
    assert before == {str(p): p.read_bytes() for p in book.root.rglob('*') if p.is_file()}


def test_callback_cannot_persist_credentials_generated_after_input_guard(book):
    before = book.settings.database.read_bytes()
    result = book._mutate('S1', 'generated', {}, 0, DAY,
        lambda tx: tx.append('job_runs', 'generated', {'reason': 'Bearer fixture-only'}))
    assert result['state'] == 'INVALID_INPUT'
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('table,field', [('reservations', 'shares'), ('tax_items', 'assessed_fen')])
def test_negative_share_reserve_and_assessed_tax_are_atomic(book, table, field):
    before = book.settings.database.read_bytes()
    result = book._mutate('S1', 'negative-domain-field', {}, 0, DAY,
                         lambda tx: tx.append(table, 'bad-row', {field: -1}))
    assert result['state'] == 'INTEGRITY_ERROR'
    assert book.settings.database.read_bytes() == before


def test_partial_daily_backups_cannot_evict_last_complete_backup(book, monkeypatch):
    complete = book.backup()
    folder = book.root / 'data/backups/ledger' / complete['backup_id']
    original = book._evidence_inventory
    def missing_evidence(db, **kwargs):
        result = original(db, **kwargs)
        result.update(state='PARTIAL', issues=[{'kind': 'MISSING_EVIDENCE_FILE'}])
        return result
    monkeypatch.setattr(book, '_evidence_inventory', missing_evidence)
    for _ in range(31):
        assert book.backup()['state'] == 'PARTIAL'
    assert (folder / 'manifest.json').is_file()
    assert (folder / 'ledger.sqlite3').is_file()


def test_audit_accounts_and_evidence_share_one_read_transaction(book, monkeypatch):
    from china_stock_sim.ledger import maintenance
    original_connection = maintenance.readonly_connection
    inserted = False
    def concurrent_commit_before_audit_connection(path):
        nonlocal inserted
        if not inserted:
            inserted = True
            assert write(book, key='audit-concurrent', amount=100)['state'] == 'APPLIED'
        return original_connection(path)
    observed = {}
    original_inventory = book._evidence_inventory
    def inspect_same_snapshot(db, **kwargs):
        observed['cash'] = db.execute("SELECT SUM(amount_fen) FROM cash_entries WHERE account_id='S1'").fetchone()[0]
        return original_inventory(db, **kwargs)
    monkeypatch.setattr(maintenance, 'readonly_connection', concurrent_commit_before_audit_connection)
    monkeypatch.setattr(book, '_evidence_inventory', inspect_same_snapshot)
    result = book.audit_status('S1')
    assert result['state'] == 'READY'
    assert result['accounts'][0]['cash_fen'] == observed['cash'] == 5000100
    assert result['accounts'][0]['account_revision'] == 1
    assert result['read_only'] is True
