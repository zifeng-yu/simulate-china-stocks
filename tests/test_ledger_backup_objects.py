"""CAS-backed ledger backups retain a complete recoverable closure without expanding old rows."""

import json
import os
import sqlite3
from datetime import datetime

import pytest

from china_stock_sim.common import evidence_objects
from china_stock_sim.ledger import LedgerService, decision_evidence, maintenance
from china_stock_sim.ledger.document_objects import archive_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, canonical
from china_stock_sim.ledger.store import initialize

NOW = '2026-11-03T12:00:00+08:00'


@pytest.fixture
def object_book(settings):
    initialize(settings, recorded_at=NOW)
    return LedgerService(settings, clock=lambda: datetime.fromisoformat(NOW), test_mode=True)


def add_record(book, value, identity='proof'):
    with sqlite3.connect(book.settings.database) as db:
        db.execute('INSERT INTO job_runs VALUES(?,?,?)', (identity, 'S1', canonical(value)))


def files(root):
    return {str(path.relative_to(root)): maintenance.file_hash(path)
            for path in root.rglob('*') if path.is_file()}


def test_backup_includes_wrappers_recursive_cas_and_opaque_original_without_expansion(object_book, tmp_path, monkeypatch):
    book = object_book
    proof = document(book.root, 'tests', {'original': 'business document'})
    wrapped = archive_document(book.root, proof)
    child = evidence_objects.put_json(book.root, {'document': proof})
    witness = evidence_objects.put_json(book.root, {'child': child})
    original = evidence_objects.put_blob(book.root, b'NOT_JSON_OLD_EXACT_BYTES' * 10000)
    add_record(book, {'execution_evidence': witness, 'original_record_ref': original})
    reader = evidence_objects.read_json

    def reject_opaque(root, reference, **kwargs):
        assert reference != original, 'opaque historical payload was expanded'
        return reader(root, reference, **kwargs)

    monkeypatch.setattr(evidence_objects, 'read_json', reject_opaque)
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    archived = {row['path'] for row in backup['evidence']['files']}
    expected = {proof['path'], *(r['_evidence_ref']['path'] for r in (
        child, witness, original, wrapped['object_ref']))}
    assert expected <= archived
    folder = book.root / 'data/backups/ledger' / backup['backup_id']
    assert (folder / 'ledger.sqlite3').stat().st_ino != book.settings.database.stat().st_ino
    assert (folder / 'configuration/configs/default.toml').stat().st_ino != (
        book.root / 'configs/default.toml').stat().st_ino
    for name in expected:
        assert (folder / 'evidence' / name).stat().st_ino == (book.root / name).stat().st_ino
    before = files(book.root)
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
    assert files(book.root) == before
    target = tmp_path / 'restored'
    assert book.restore_backup(backup['backup_id'], target)['state'] == 'RESTORED'
    assert b''.join(evidence_objects.iter_bytes(target, original)) == b'NOT_JSON_OLD_EXACT_BYTES' * 10000
    assert decision_evidence.read_business_document(target, proof)[0] == {'original': 'business document'}
    for name in expected:
        assert (target / name).stat().st_ino != (book.root / name).stat().st_ino


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'malformed_reference', 'missing_manifest_entry'])
def test_missing_or_corrupt_cas_never_verifies_or_restores(object_book, tmp_path, damage):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'cash_fen': 123})
    add_record(book, {'account_view_evidence': ref})
    backup = book.backup(label='manual')
    folder = book.root / 'data/backups/ledger' / backup['backup_id']
    object_path = folder / 'evidence' / ref['_evidence_ref']['path']
    if damage == 'missing':
        object_path.unlink()
    elif damage == 'corrupt':
        # Replace this backup's link, preserving the live immutable source.
        object_path.unlink()
        object_path.write_bytes(b'corrupt')
    else:
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        if damage == 'missing_manifest_entry':
            manifest['evidence']['files'] = [item for item in manifest['evidence']['files']
                                            if item['path'] != ref['_evidence_ref']['path']]
        else:
            with sqlite3.connect(folder / 'ledger.sqlite3') as db:
                db.execute('UPDATE job_runs SET data=?', (canonical({'account_view_evidence': {
                    '_evidence_ref': {'path': ref['_evidence_ref']['path']}}}),))
            manifest['database_sha256'] = maintenance.file_hash(folder / 'ledger.sqlite3')
        manifest_path.write_text(json.dumps(manifest))
    assert book.verify_backup(backup['backup_id'])['state'] == 'INTEGRITY_ERROR'
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        book.restore_backup(backup['backup_id'], target)
    assert not target.exists()


def test_stable_backup_validation_reuses_proof_but_same_size_same_mtime_damage_invalidates(object_book, monkeypatch):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'value': 123})
    add_record(book, {'account_view_evidence': ref})
    backup = book.backup(label='manual')
    original = maintenance._ledger_closure
    monkeypatch.setattr(maintenance, '_ledger_closure', lambda *a: pytest.fail('unchanged backup fully reread'))
    before = files(book.root)
    with decision_evidence.verification_scope(book.root, persist=True):
        assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
    assert files(book.root) == before
    monkeypatch.setattr(maintenance, '_ledger_closure', original)
    path = book.root / 'data/backups/ledger' / backup['backup_id'] / 'evidence' / ref['_evidence_ref']['path']
    stamp, raw = path.stat(), path.read_bytes()
    path.unlink()
    path.write_bytes(b'X' + raw[1:])
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert book.verify_backup(backup['backup_id'])['state'] == 'INTEGRITY_ERROR'


def test_immutable_link_failure_falls_back_to_independent_copy(object_book, monkeypatch):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'value': 123})
    add_record(book, {'account_view_evidence': ref})
    monkeypatch.setattr(maintenance.os, 'link', lambda *a: (_ for _ in ()).throw(OSError('EXDEV')))
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE' and backup['storage']['hardlinked_files'] == 0
    target = book.root / 'data/backups/ledger' / backup['backup_id'] / 'evidence' / ref['_evidence_ref']['path']
    assert target.stat().st_ino != (book.root / ref['_evidence_ref']['path']).stat().st_ino


def test_schema3_migrate_refuses_before_backup_or_base_policy_initialization(object_book, monkeypatch):
    book = object_book
    with sqlite3.connect(book.settings.database) as db:
        db.execute('PRAGMA user_version=3')
    before = files(book.root)
    monkeypatch.setattr(book, '_backup_locked', lambda *a, **k: pytest.fail('implicit migration backup'))
    with pytest.raises(LedgerError) as error:
        book.migrate()
    assert error.value.state == 'LEDGER_COMPACTION_REQUIRED'
    # The explicit migration lock can exist; financial and policy bytes cannot change.
    assert all(maintenance.file_hash(book.root / path) == value for path, value in before.items())


def test_thirty_daily_restore_points_share_objects_and_keep_manual(object_book):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'value': 123})
    add_record(book, {'original_record_ref': ref})
    manual = book.backup(label='manual')
    for _ in range(31):
        latest = book.backup(label='daily')
    folder = book.root / 'data/backups/ledger'
    assert len(list(folder.glob('daily-*'))) == 30
    assert (folder / manual['backup_id']).exists()
    assert book.verify_backup(latest['backup_id'])['state'] == 'READY'
    assert len({path.stat().st_ino for path in folder.glob('*/evidence/' + ref['_evidence_ref']['path'])}) == 1


def test_live_missing_cas_produces_partial_without_evicting_complete_backup(object_book):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'value': 123})
    add_record(book, {'account_view_evidence': ref})
    complete = book.backup(label='daily')
    (book.root / ref['_evidence_ref']['path']).unlink()
    partial = book.backup(label='daily')
    assert complete['state'] == 'COMPLETE' and partial['state'] == 'PARTIAL'
    assert book.verify_backup(complete['backup_id'])['state'] == 'READY'


def test_incomplete_coverage_cannot_be_promoted_by_cached_verification(object_book):
    book = object_book
    backup = book.backup(label='manual')
    folder = book.root / 'data/backups/ledger' / backup['backup_id']
    with sqlite3.connect(folder / 'ledger.sqlite3') as db:
        db.execute('INSERT INTO job_runs VALUES(?,?,?)', ('missing-snapshot', 'S1',
                    canonical({'snapshot_ids': ['a' * 64]})))
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['database_sha256'] = maintenance.file_hash(folder / 'ledger.sqlite3')
    manifest_path.write_text(json.dumps(manifest))
    assert book.verify_backup(backup['backup_id'])['state'] == 'PARTIAL'
    assert book.verify_backup(backup['backup_id'])['state'] == 'PARTIAL'


def test_large_account_view_is_opaque_but_plan_root_retains_its_document(object_book, monkeypatch):
    book = object_book
    proof = document(book.root, 'tests', {'original': 'frozen business contract'})
    # Legacy full account snapshots can exceed the generic 64 MiB JSON expansion limit.
    raw = b'{"legacy":"' + b'x' * (65 * 1024 * 1024) + b'"}'
    witness = evidence_objects.put_blob(book.root, raw)
    add_record(book, {'account_view_evidence': witness, 'frozen_plan_binding': {'document': proof}})
    reader = evidence_objects.read_json
    def bounded(root, reference, **kwargs):
        assert reference != witness, 'large account evidence must be verified without expansion'
        return reader(root, reference, **kwargs)
    monkeypatch.setattr(evidence_objects, 'read_json', bounded)
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    assert {proof['path'], witness['_evidence_ref']['path']} <= {
        row['path'] for row in backup['evidence']['files']}
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
