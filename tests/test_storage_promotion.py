import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from china_stock_sim.common.evidence_objects import put_json, verify
from china_stock_sim.ledger.storage_migration import capture_baseline
from china_stock_sim.ledger.storage_promotion import (
    StoragePromotionError,
    apply_promotion,
    prepare_promotion,
    rollback_promotion,
)
from china_stock_sim.scheduler.workspace import writer_lock


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root):
    production = root / 'data/state/ledger.sqlite3'
    production.parent.mkdir(parents=True)
    (root / 'configs').mkdir()
    (root / 'configs/default.toml').write_text('enabled = true\n')
    (root / 'logs').mkdir()
    with sqlite3.connect(production) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE accounts(account_id TEXT,strategy_id TEXT)')
        db.execute("INSERT INTO accounts VALUES('S1','S1')")
        db.execute('CREATE TABLE account_state(account_id TEXT,data TEXT)')
        db.execute('INSERT INTO account_state VALUES(?,?)', ('S1', '{"account_revision":9}'))
        db.execute('CREATE TABLE cash_entries(account_id TEXT,amount_fen INTEGER)')
        db.execute("INSERT INTO cash_entries VALUES('S1',4999990)")
        db.execute('CREATE TABLE fills(id TEXT,account_id TEXT,data TEXT)')
        db.execute('INSERT INTO fills VALUES(?,?,?)', ('fill', 'S1', '{"quantity":100,"price":"1.00"}'))
        db.execute('PRAGMA user_version=3')
        baseline = capture_baseline(db)
    source_sha = _hash(production)
    stage = root / 'data/maintenance/test-staging'
    compact = stage / 'data/state/ledger.compact.sqlite3'
    compact.parent.mkdir(parents=True)
    with sqlite3.connect(production) as source, sqlite3.connect(compact) as target:
        source.backup(target)
    reference = put_json(stage, {'old': 'preserved proof'})
    with sqlite3.connect(compact) as db:
        db.execute('UPDATE fills SET data=?', (json.dumps({'quantity': 100, 'price': '1.00',
            'account_view_evidence': reference}),))
        db.execute('PRAGMA user_version=4')
    staging_receipt = root / 'logs/staging.json'
    staging_receipt.write_text(json.dumps({'state': 'COMPLETE', 'source_sha256': source_sha,
        'source_bytes': production.stat().st_size, 'final_baseline': baseline,
        'cas': {'checks': [verify(stage, reference)]}}))
    projection_receipt = root / 'logs/projection.json'
    projection_receipt.write_text(json.dumps({'state': 'COMPLETE',
        'compact_path': str(compact.relative_to(root)), 'compact_sha256': _hash(compact),
        'source_staging_receipt_sha256': _hash(staging_receipt),
        'financial_sha256': baseline['financial_sha256']}))
    plan = prepare_promotion(root, staging_receipt='logs/staging.json',
                             projection_receipt='logs/projection.json')
    return production, compact, reference, plan


def test_prepare_read_only_then_cutover_keeps_complete_rollback_and_shared_cas(tmp_path):
    production, compact, ref, plan = _fixture(tmp_path)
    original = production.read_bytes()
    assert not (tmp_path / ref['_evidence_ref']['path']).exists()
    state = apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    assert state['phase'] == 'VERIFIED'
    assert state['equivalence']['fill_count'] == 1
    assert _hash(production) == plan['compact_sha256']
    assert (tmp_path / plan['rollback_path']).read_bytes() == original
    assert not production.samefile(compact)
    assert verify(tmp_path, ref)['state'] == 'READY'
    assert apply_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'VERIFIED'
    assert rollback_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'ROLLED_BACK'
    assert production.read_bytes() == original
    assert _hash(tmp_path / plan['forward_path']) == plan['compact_sha256']
    assert rollback_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'ROLLED_BACK'


@pytest.mark.parametrize('phase', ['STAGED', 'OLD_MOVED', 'SWITCHED'])
def test_resume_after_each_interruption_boundary(tmp_path, phase):
    production, _, _, plan = _fixture(tmp_path)

    def interrupt(name, state):
        if name == phase:
            raise RuntimeError('isolated interruption')

    with pytest.raises(RuntimeError, match='isolated interruption'):
        apply_promotion(tmp_path, plan, maintenance_confirmed=True, checkpoint=interrupt)
    state = apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    assert state['phase'] == 'VERIFIED'
    assert _hash(production) == plan['compact_sha256']
    assert _hash(tmp_path / plan['rollback_path']) == plan['source_sha256']


def test_rollback_refuses_new_business_even_when_schema_is_same(tmp_path):
    production, _, _, plan = _fixture(tmp_path)
    apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    with sqlite3.connect(production) as db:
        db.execute('UPDATE cash_entries SET amount_fen=amount_fen-1')
    with pytest.raises(StoragePromotionError, match='BUSINESS_ADVANCED'):
        rollback_promotion(tmp_path, plan, maintenance_confirmed=True)
    with sqlite3.connect(production) as db:
        assert db.execute('SELECT amount_fen FROM cash_entries').fetchone()[0] == 4999989


def test_rollback_only_needs_retained_original_database_not_staging(tmp_path):
    production, compact, _, plan = _fixture(tmp_path)
    apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    compact.unlink()
    assert rollback_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'ROLLED_BACK'
    assert _hash(production) == plan['source_sha256']


def test_wrong_source_hash_config_or_maintenance_stops_before_switch(tmp_path):
    production, _, _, plan = _fixture(tmp_path)
    with pytest.raises(StoragePromotionError, match='MAINTENANCE_REQUIRED'):
        apply_promotion(tmp_path, plan)
    with writer_lock(SimpleNamespace(root=tmp_path)):
        with pytest.raises(RuntimeError):
            apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    (tmp_path / 'configs/default.toml').write_text('enabled=false\n')
    with pytest.raises(StoragePromotionError, match='CONFIG_CHANGED'):
        apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    (tmp_path / 'configs/default.toml').write_text('enabled = true\n')
    with sqlite3.connect(production) as db:
        db.execute('UPDATE cash_entries SET amount_fen=amount_fen+1')
    with pytest.raises(StoragePromotionError, match='SOURCE_CHANGED'):
        apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    assert not (tmp_path / plan['rollback_path']).exists()


def test_pending_sqlite_wal_blocks_cutover_and_rollback(tmp_path):
    production, _, _, plan = _fixture(tmp_path)
    wal = Path(str(production) + '-wal')
    wal.write_bytes(b'pending transaction')
    with pytest.raises(StoragePromotionError, match='SIDECAR'):
        apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    wal.unlink()
    apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    wal.write_bytes(b'new uncheckpointed business')
    with pytest.raises(StoragePromotionError, match='SIDECAR'):
        rollback_promotion(tmp_path, plan, maintenance_confirmed=True)


def test_rollback_can_recover_old_database_gap(tmp_path):
    production, _, _, plan = _fixture(tmp_path)

    def interrupt(name, state):
        if name == 'OLD_MOVED':
            raise RuntimeError('stopped')

    with pytest.raises(RuntimeError):
        apply_promotion(tmp_path, plan, maintenance_confirmed=True, checkpoint=interrupt)
    assert not production.exists()
    assert rollback_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'ROLLED_BACK'
    assert _hash(production) == plan['source_sha256']


def test_rollback_completion_receipt_can_be_recovered(tmp_path):
    production, _, _, plan = _fixture(tmp_path)
    apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    os.replace(production, tmp_path / plan['forward_path'])
    state_path = tmp_path / plan['state_path']
    state = json.loads(state_path.read_text())
    state['phase'] = 'ROLLBACK_NEW_MOVED'
    state_path.write_text(json.dumps(state))
    os.replace(tmp_path / plan['rollback_path'], production)
    assert rollback_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'ROLLED_BACK'


def test_truncated_copy_is_recreated(tmp_path):
    production, compact, _, plan = _fixture(tmp_path)
    temporary = tmp_path / plan['temporary_path']
    temporary.write_bytes(b'partial copy after crash')
    assert apply_promotion(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'VERIFIED'
    assert not production.samefile(compact)


def test_database_hardlink_is_rejected(tmp_path):
    production, compact, _, plan = _fixture(tmp_path)
    os.link(compact, tmp_path / plan['temporary_path'])
    with pytest.raises(StoragePromotionError, match='HARDLINK_FORBIDDEN'):
        apply_promotion(tmp_path, plan, maintenance_confirmed=True)
    assert _hash(production) == plan['source_sha256']
