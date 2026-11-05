"""SL04: explicit old-layout migration and recovery from a backup's own facts."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest
from execution_fixtures import execution_book

from china_stock_sim.config import load_settings
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import canonical, digest
from china_stock_sim.ledger.schema import (
    SCHEMA2_TABLES,
    SCHEMA_VERSION,
    STRATEGY_TABLES,
    create_schema,
    initial_state,
)
from china_stock_sim.ledger.store import initialize, read_snapshot

NOW = '2026-11-01T12:30:00+08:00'
CREATED = '2026-10-28T15:00:00+08:00'
assert execution_book


def service(settings):
    return LedgerService(settings, clock=lambda: datetime.fromisoformat(NOW), test_mode=True)


def schema2(settings, *, extra=False, evidence=False):
    """Build an actual schema2 source, including optional already-booked business."""
    settings.database.parent.mkdir(parents=True)
    proof = document(settings.root, 'old-schema2', {'source': 'existing evidence'}) if evidence else None
    with closing(sqlite3.connect(settings.database)) as db:
        create_schema(db)
        for table in STRATEGY_TABLES:
            db.execute(f'DROP TABLE {table}')
        rows = [(a.id, a.strategy, a.initial_cash_fen) for a in settings.accounts]
        if extra:
            rows.append(('ALT', 'S1', 7654321))
        for identity, strategy, principal in rows:
            db.execute('INSERT INTO accounts VALUES (?,?,?,?,?)',
                       (identity, strategy, 'CNY', principal, CREATED))
            db.execute('INSERT INTO cash_entries VALUES (?,?,?,?,?,?,?,?)',
                       ('original:' + identity, identity, 'INITIAL_CAPITAL', principal,
                        CREATED, None, None, '{}'))
            db.execute('INSERT INTO account_state VALUES (?,?)', (identity, canonical(initial_state())))
        if evidence:
            state = {**initial_state(), 'account_revision': 7,
                     'simulation_start_session': '2026-10-29'}
            db.execute('UPDATE account_state SET data=? WHERE account_id=?', (canonical(state), 'S1'))
            db.execute('INSERT INTO cash_entries VALUES (?,?,?,?,?,?,?,?)',
                       ('old-correction', 'S1', 'CORRECTION', 123, CREATED, '2026-10-29', None, '{}'))
            db.execute('INSERT INTO job_runs VALUES (?,?,?)',
                       ('old-run', 'S1', canonical({'evidence': proof, 'state': 'COMPLETE'})))
        db.execute('PRAGMA user_version=2')
        db.commit()
    return proof


def original_rows(settings):
    with closing(sqlite3.connect(settings.database)) as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1,2').fetchall()
                for table in ('accounts', 'account_state', 'cash_entries', 'operations', *SCHEMA2_TABLES)}


def policies(settings):
    with closing(sqlite3.connect(settings.database)) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT data FROM account_policies ORDER BY account_id,id')]


def test_new_initialization_creates_base_documents_and_still_only_one_deposit(settings):
    assert initialize(settings, recorded_at=NOW)['created_accounts'] == 3
    assert read_snapshot(settings)['schema_version'] == SCHEMA_VERSION == 4
    rows = policies(settings)
    assert len(rows) == 3
    for row in rows:
        assert row['policy_revision'] == 0 and row['effective_session'] is None
        assert (settings.root / row['document']['path']).is_file()
    before = settings.database.read_bytes()
    assert initialize(settings)['created_accounts'] == 0
    assert settings.database.read_bytes() == before


def test_schema2_migration_preserves_every_old_row_and_archives_old_evidence(settings):
    proof = schema2(settings, extra=True, evidence=True)
    before = original_rows(settings)
    result = service(settings).migrate()
    assert result['state'] == 'APPLIED' and result['schema_version'] == SCHEMA_VERSION
    assert original_rows(settings) == before
    rows = policies(settings)
    assert len(rows) == 4
    assert all(row['policy_revision'] == 0 and row['effective_session'] is None for row in rows)
    folder = settings.root / 'data/backups/ledger' / result['backup_id']
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['schema_version'] == 2 and manifest['state'] == 'COMPLETE'
    assert proof['path'] in {item['path'] for item in manifest['evidence']['files']}
    with closing(sqlite3.connect(folder / 'ledger.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert not set(STRATEGY_TABLES).intersection(
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    assert service(settings).migrate()['state'] == 'ALREADY_APPLIED'
    assert len(list(folder.parent.glob('migration-*/manifest.json'))) == 1


def test_schema2_compatible_inspection_does_not_migrate_and_business_reads_require_upgrade(settings):
    schema2(settings)
    before = settings.database.read_bytes()
    assert read_snapshot(settings)['schema_version'] == 2
    assert initialize(settings)['created_accounts'] == 0
    assert service(settings).audit_status()['schema_version'] == 2
    with pytest.raises(ValueError, match='显式迁移'):
        service(settings).account_state('S1', '2026-11-02')
    assert settings.database.read_bytes() == before


def test_schema2_incomplete_backup_blocks_migration_without_touching_database(settings):
    proof = schema2(settings, evidence=True)
    (settings.root / proof['path']).unlink()
    before = settings.database.read_bytes()
    with pytest.raises(ValueError, match='备份证据不完整'):
        service(settings).migrate()
    assert settings.database.read_bytes() == before
    assert read_snapshot(settings)['schema_version'] == 2


def test_schema2_policy_initialization_failure_rolls_back_and_keeps_complete_backup(settings, monkeypatch):
    from china_stock_sim.ledger import account_policies

    schema2(settings, evidence=True)
    before = settings.database.read_bytes()
    original = account_policies.initialize_base_policy

    def fail(db, **kwargs):
        original(db, **kwargs)
        raise OSError('simulated interrupted migration')

    monkeypatch.setattr(account_policies, 'initialize_base_policy', fail)
    with pytest.raises(OSError, match='interrupted'):
        service(settings).migrate()
    assert settings.database.read_bytes() == before
    manifests = list((settings.root / 'data/backups/ledger').glob('migration-*/manifest.json'))
    assert len(manifests) == 1 and json.loads(manifests[0].read_text())['state'] == 'COMPLETE'


def test_migration_initializes_policies_at_actual_migration_time(settings, monkeypatch):
    from china_stock_sim.ledger import account_policies

    schema2(settings)
    original, observed = account_policies.initialize_base_policy, []

    def record(db, **kwargs):
        observed.append((kwargs['account']['created_at'], kwargs['recorded_at']))
        return original(db, **kwargs)

    monkeypatch.setattr(account_policies, 'initialize_base_policy', record)
    service(settings).migrate()
    assert len(observed) == 3
    assert all(created == CREATED and datetime.fromisoformat(recorded) == datetime.fromisoformat(NOW)
               for created, recorded in observed)


def test_unknown_migrated_strategy_keeps_identity_without_guessing_model_capability(settings):
    schema2(settings)
    with closing(sqlite3.connect(settings.database)) as db:
        db.execute("UPDATE accounts SET strategy_id='External-Unregistered' WHERE account_id='S1'")
        db.commit()
    service(settings).migrate()
    snapshot = read_snapshot(settings)
    assert next(row for row in snapshot['accounts'] if row['account_id'] == 'S1')['strategy_id'] == 'External-Unregistered'
    row = next(row for row in policies(settings) if row['policy']['strategy_id'] == 'External-Unregistered')
    assert row['definition_status'] == 'UNREGISTERED'
    assert row['definition']['uses_model'] is None
    assert row['policy']['parameters'] == {}


@pytest.mark.parametrize('source_version', [1, 2, 3, SCHEMA_VERSION])
def test_restore_source_layout_uses_its_archived_configuration(settings, tmp_path, source_version):
    if source_version == 1:
        from test_ledger_kernel import legacy
        legacy(settings)
    elif source_version == 2:
        schema2(settings, extra=True)
    else:
        initialize(settings, recorded_at=NOW)
        if source_version == 3:
            with sqlite3.connect(settings.database) as db:
                db.execute('PRAGMA user_version=3')
    book = service(settings)
    if source_version < SCHEMA_VERSION:
        from china_stock_sim.ledger.kernel import ledger_lock
        with ledger_lock(settings.root):
            backup = book._backup_locked('migration', legacy=True)
    else:
        backup = book.backup(label='manual')
    expected_config = (settings.root / 'configs/default.toml').read_bytes()
    expected_rules = (settings.root / 'configs/execution_rules.toml').read_bytes()
    expected_accounts = read_snapshot(settings)['accounts']
    (settings.root / 'configs/default.toml').write_text('not = [valid')
    (settings.root / 'configs/execution_rules.toml').write_text('current rules must not replace archive')
    target = tmp_path / f'recovered-schema{source_version}'
    assert book.restore_backup(backup['backup_id'], target)['state'] == 'RESTORED'
    assert (target / 'configs/default.toml').read_bytes() == expected_config
    assert (target / 'configs/execution_rules.toml').read_bytes() == expected_rules
    restored = read_snapshot(load_settings(target))
    assert restored['schema_version'] == source_version
    assert restored['accounts'] == expected_accounts


def test_backup_of_migrated_dynamic_accounts_restores_policy_and_nested_decision_documents(settings, tmp_path):
    from china_stock_sim.ledger.strategy_decisions import _append_history, decision_identity

    schema2(settings, extra=True)
    book = service(settings)
    book.migrate()
    reply = document(settings.root, 'model-replies', {'text': 'redacted public response'})
    identity = decision_identity('ALT', '2026-11-02')
    policy = book.get_account_policy('ALT', '2026-11-02')
    inputs = {'market_snapshot_ids': [], 'data_evidence': [], 'account_revision': 0}
    decision_row = _append_history(settings.root, {'decision_id': identity, 'account_id': 'ALT',
        'target_session': '2026-11-02', 'state': 'WAITING_INPUT', 'model_reply': reply,
        'policy_id': policy['policy_id'], 'policy_hash': policy['policy_hash'],
        'policy_document': policy['document'], 'strategy_id': policy['policy']['strategy_id'],
        'strategy_version': policy['policy']['strategy_version'],
        'strategy_implementation_hash': policy['definition']['implementation_hash'],
        'input_document': document(settings.root, 'strategy-input', inputs),
        'input_hash': digest(inputs)}, NOW, 'PREPARE')
    decision = decision_row['document']
    with closing(sqlite3.connect(settings.database)) as db:
        db.execute('INSERT INTO strategy_decisions VALUES (?,?,?)',
                   (identity, 'ALT', canonical(decision_row)))
        db.commit()
    expected_policies = policies(settings)
    backup = book.backup(label='manual')
    expected_paths = {row['document']['path'] for row in expected_policies} | {reply['path'], decision['path']}
    assert expected_paths <= {row['path'] for row in backup['evidence']['files']}
    for relative in expected_paths:
        (settings.root / relative).unlink()
    target = tmp_path / 'dynamic-restore'
    assert book.restore_backup(backup['backup_id'], target)['state'] == 'RESTORED'
    restored_settings = load_settings(target)
    assert policies(restored_settings) == expected_policies
    assert next(row for row in read_snapshot(restored_settings)['accounts'] if row['account_id'] == 'ALT')['cash_fen'] == 7654321
    assert all((target / relative).is_file() for relative in expected_paths)


@pytest.mark.parametrize('damage', ['configuration', 'policy', 'missing_configuration'])
def test_restore_rejects_missing_or_corrupted_archived_material_before_creating_target(settings, tmp_path, damage):
    initialize(settings, recorded_at=NOW)
    book = service(settings)
    backup = book.backup(label='manual')
    folder = settings.root / 'data/backups/ledger' / backup['backup_id']
    if damage == 'configuration':
        (folder / 'configuration/configs/default.toml').write_text('corrupted')
    elif damage == 'policy':
        (folder / 'evidence' / policies(settings)[0]['document']['path']).write_text('{}')
    else:
        manifest = json.loads((folder / 'manifest.json').read_text())
        del manifest['configuration']
        (folder / 'manifest.json').write_text(json.dumps(manifest))
    target = tmp_path / 'should-not-exist'
    with pytest.raises(ValueError, match='摘要|自身配置'):
        book.restore_backup(backup['backup_id'], target)
    assert not target.exists()


def test_backup_configuration_whitelist_never_archives_credentials(settings):
    initialize(settings, recorded_at=NOW)
    (settings.root / '.env').write_text('private test credential')
    (settings.root / 'configs/models.toml').write_text('private test model endpoint')
    backup = service(settings).backup(label='manual')
    paths = {row['path'] for row in backup['configuration']['files']}
    assert paths == {'configs/default.toml', 'configs/execution_rules.toml'}
    folder = settings.root / 'data/backups/ledger' / backup['backup_id']
    assert not (folder / '.env').exists()
    assert not (folder / 'configuration/configs/models.toml').exists()


def test_tampered_policy_index_cannot_be_a_complete_backup_even_if_original_is_valid(settings):
    initialize(settings, recorded_at=NOW)
    with closing(sqlite3.connect(settings.database)) as db:
        identity, account_id, raw = db.execute('SELECT id,account_id,data FROM account_policies LIMIT 1').fetchone()
        row = json.loads(raw)
        row['policy']['parameters']['target_count'] = 999
        db.execute('UPDATE account_policies SET data=? WHERE id=? AND account_id=?',
                   (canonical(row), identity, account_id))
        db.commit()
    backup = service(settings).backup(label='manual')
    assert backup['state'] == 'PARTIAL'
    assert any(issue['kind'] == 'STRATEGY_INDEX_INTEGRITY_ERROR' for issue in backup['evidence']['issues'])


def test_base_policy_and_target_decision_uniqueness_are_database_constraints(settings):
    initialize(settings, recorded_at=NOW)
    with closing(sqlite3.connect(settings.database)) as db:
        account_id, raw = db.execute('SELECT account_id,data FROM account_policies LIMIT 1').fetchone()
        row = json.loads(raw)
        row['policy_revision'] = 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO account_policies VALUES (?,?,?)', ('another-base', account_id, canonical(row)))
        db.execute('INSERT INTO strategy_decisions VALUES (?,?,?)',
                   ('decision-one', account_id, canonical({'target_session': '2026-11-02'})))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO strategy_decisions VALUES (?,?,?)',
                       ('decision-two', account_id, canonical({'target_session': '2026-11-02'})))


def prepared_model(settings):
    from test_ledger_trading import Facts

    book = LedgerService(settings, market=Facts(),
                         clock=lambda: datetime.fromisoformat(NOW), test_mode=True)
    book.initialize()
    target = '2026-11-02'

    def call(name, payload):
        revision = book.account_state('LLM', target)['account_revision']
        result = getattr(book, name)('LLM', payload, operation_id=name, expected_revision=revision)
        assert result['state'] == 'APPLIED', result
        return result

    policy = call('schedule_account_policy', {'effective_session': target,
        'strategy_parameters': {'max_holdings': 4}, 'reason': 'isolated integrity fixture'})
    call('activate_simulation', {'target_session': target})
    revision = book.account_state('LLM', target)['account_revision']
    decision = call('prepare_strategy_decision', {'target_session': target,
        'market_as_of': '2026-10-30', 'input_cutoff': NOW, 'expected_sequence': 0,
        'input_bundle': {'account_revision': revision, 'market_snapshot_ids': [], 'data_evidence': []}})
    attempt = call('claim_model_attempt', {'target_session': target,
        'decision_id': decision['decision_id'], 'input_hash': decision['input_hash'],
        'request': {'text': 'isolated public request'}})
    call('finish_model_attempt', {'target_session': target, 'attempt_id': attempt['attempt_id'],
        'status': 'RECEIVED', 'response': {'text': 'isolated response'}})
    return book, policy, decision, attempt


def test_deleted_referenced_policy_is_not_a_complete_backup_or_restorable(settings, tmp_path):
    book, policy, _, _ = prepared_model(settings)
    assert book.backup(label='manual')['state'] == 'COMPLETE'
    with closing(sqlite3.connect(settings.database)) as db:
        db.execute('DELETE FROM account_policies WHERE account_id=? AND id=?', ('LLM', policy['policy_id']))
        db.commit()
    assert book.audit_status()['state'] == 'PARTIAL'
    backup = book.backup(label='manual')
    assert backup['state'] == 'PARTIAL'
    assert {issue['table'] for issue in backup['evidence']['issues']} >= {'strategy_decisions', 'model_attempts'}
    target = tmp_path / 'broken-policy-restore'
    with pytest.raises(ValueError, match='政策或决策索引'):
        book.restore_backup(backup['backup_id'], target)
    assert not target.exists()


@pytest.mark.parametrize('field', ['account_policy_hash', 'input_hash', 'request_hash',
                                  'response_hash', 'decision_id', 'decision_sequence'])
def test_model_attempt_cross_references_and_original_hashes_are_audited(settings, field):
    book, _, _, attempt = prepared_model(settings)
    with closing(sqlite3.connect(settings.database)) as db:
        raw = db.execute('SELECT data FROM model_attempts WHERE account_id=? AND id=?',
                         ('LLM', attempt['attempt_id'])).fetchone()[0]
        row = json.loads(raw)
        row[field] = 999 if field == 'decision_sequence' else 'f' * 64
        db.execute('UPDATE model_attempts SET data=? WHERE account_id=? AND id=?',
                   (canonical(row), 'LLM', attempt['attempt_id']))
        db.commit()
    backup = book.backup(label='manual')
    assert backup['state'] == 'PARTIAL'
    assert any(issue.get('table') == 'model_attempts' for issue in backup['evidence']['issues'])


def test_missing_model_attempt_policy_binding_is_not_accepted_as_legacy(settings):
    book, _, _, attempt = prepared_model(settings)
    with closing(sqlite3.connect(settings.database)) as db:
        raw = db.execute('SELECT data FROM model_attempts WHERE account_id=? AND id=?',
                         ('LLM', attempt['attempt_id'])).fetchone()[0]
        row = json.loads(raw)
        for key in ('account_policy_id', 'account_policy_hash', 'account_policy',
                    'strategy_definition', 'definition_status', 'account_revision_at_claim'):
            row.pop(key)
        db.execute('UPDATE model_attempts SET data=? WHERE account_id=? AND id=?',
                   (canonical(row), 'LLM', attempt['attempt_id']))
        db.commit()
    assert book.backup(label='manual')['state'] == 'PARTIAL'


@pytest.mark.parametrize('damage', ['missing_plan', 'wrong_ref'])
def test_frozen_new_decision_requires_its_matching_plan(execution_book, damage):
    from test_execution_decision_audit import freeze, strategy_request

    book = execution_book
    request, _ = strategy_request(book, empty=True)
    plan = freeze(book, request)
    assert book.backup(label='manual')['state'] == 'COMPLETE'
    with closing(sqlite3.connect(book.settings.database)) as db:
        if damage == 'missing_plan':
            db.execute('DELETE FROM plans WHERE account_id=? AND id=?', ('LLM', plan['plan_id']))
        else:
            raw = db.execute('SELECT data FROM plans WHERE account_id=? AND id=?',
                             ('LLM', plan['plan_id'])).fetchone()[0]
            row = json.loads(raw)
            row['decision_ref'] = None
            db.execute('UPDATE plans SET data=? WHERE account_id=? AND id=?',
                       (canonical(row), 'LLM', plan['plan_id']))
        db.commit()
    backup = book.backup(label='manual')
    assert backup['state'] == 'PARTIAL'
    assert any(issue.get('table') == 'strategy_decisions' for issue in backup['evidence']['issues'])


def test_legacy_attempt_and_plan_remain_recoverable_before_and_after_schema2_migration(settings, tmp_path):
    schema2(settings)
    request = document(settings.root, 'legacy-model', {'prompt': 'original schema2 request'})
    response = document(settings.root, 'legacy-model', {'text': 'original schema2 response'})
    old_core = document(settings.root, 'legacy-strategy', {'original': 'schema2 plan evidence'})
    old_plan = {'plan_id': 'old-plan', 'account_id': 'LLM', 'target_session': '2026-10-29',
                'provenance_mode': 'STRATEGY', 'decision_ref': {'decision_core': old_core}}
    old_plan['document'] = document(settings.root, 'legacy-frozen', old_plan)
    old_attempt = {'attempt_id': 'old-attempt', 'target_session': '2026-10-29', 'attempt_number': 1,
                   'claimed_at': CREATED, 'status': 'SUCCEEDED', 'request_hash': request['content_hash'],
                   'request_document': request, 'response_document': response, 'model_ref': 'daily_planner'}
    with closing(sqlite3.connect(settings.database)) as db:
        db.execute('INSERT INTO model_attempts VALUES(?,?,?)', ('old-attempt', 'LLM', canonical(old_attempt)))
        db.execute('INSERT INTO plans VALUES(?,?,?)', ('old-plan', 'LLM', canonical(old_plan)))
        db.commit()
    book = service(settings)
    assert book.audit_status()['state'] == 'READY'
    result = book.migrate()
    before = tmp_path / 'restored-legacy2'
    assert book.restore_backup(result['backup_id'], before)['state'] == 'RESTORED'
    assert read_snapshot(load_settings(before))['schema_version'] == 2
    assert book.audit_status()['state'] == 'READY'
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    after = tmp_path / 'restored-migrated3'
    assert book.restore_backup(backup['backup_id'], after)['state'] == 'RESTORED'
    with closing(sqlite3.connect(after / 'data/state/ledger.sqlite3')) as db:
        assert json.loads(db.execute("SELECT data FROM model_attempts WHERE id='old-attempt'").fetchone()[0]) == old_attempt
        assert db.execute('SELECT COUNT(*) FROM strategy_decisions').fetchone()[0] == 0
