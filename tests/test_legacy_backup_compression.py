"""Old manifest identities survive a verified compressed physical representation."""

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from china_stock_sim.common import evidence_objects
from china_stock_sim.ledger import LedgerService, decision_evidence, maintenance
from china_stock_sim.ledger.document_objects import archive_document
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import canonical, ledger_lock
from china_stock_sim.ledger.store import initialize

NOW = '2026-11-03T12:00:00+08:00'


def tree(root):
    return {str(p.relative_to(root)): (maintenance.file_hash(p), p.stat().st_mtime_ns,
                                     p.stat().st_ctime_ns)
            for p in root.rglob('*') if p.is_file()}


def replace_bytes(path, raw):
    temporary = path.with_name(path.name + '.replacement')
    temporary.write_bytes(raw)
    os.replace(temporary, path)


@pytest.fixture
def legacy_archive(settings):
    initialize(settings, recorded_at=NOW)
    book = LedgerService(settings, clock=lambda: datetime.fromisoformat(NOW), test_mode=True)
    body = {'exact': '旧原文', 'numbers': [1, 2, 3]}
    proof = document(book.root, 'legacy-test', body)
    raw = ('  ' + json.dumps(body, ensure_ascii=False, indent=3) + ' \n\n').encode()
    (book.root / proof['path']).write_bytes(raw)
    other = document(book.root, 'legacy-test', {'another': 'plain raw member'})
    native = document(book.root, 'legacy-test', {'native': 'a' * 70_000})
    with sqlite3.connect(settings.database) as db:
        db.execute('INSERT INTO job_runs VALUES(?,?,?)', (
            'legacy-proof', 'S1', canonical({'proof': proof, 'other': other, 'native': native})))
        db.execute('PRAGMA user_version=3')
    with ledger_lock(book.root):
        backup = book._backup_locked('migration', legacy=True)
    assert backup['state'] == 'COMPLETE'
    folder = book.root / Path(backup['manifest']).parent
    item = next(i for i in backup['evidence']['files'] if i['path'] == proof['path'])
    return {'book': book, 'backup': backup, 'folder': folder, 'proof': proof,
            'raw': raw, 'item': item, 'other': other, 'native': native}


def compress(fixture):
    return archive_document(fixture['folder'] / 'evidence', fixture['proof'])


def test_old_raw_and_mixed_compression_preserve_manifest_database_and_exact_restore(legacy_archive, tmp_path):
    f = legacy_archive
    book, folder, backup = f['book'], f['folder'], f['backup']
    manifest_raw = (folder / 'manifest.json').read_bytes()
    database_sha = maintenance.file_hash(folder / 'ledger.sqlite3')
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
    archived = compress(f)
    layout = maintenance.backup_member_layout(folder / 'evidence', f['item'])
    assert layout['legacy_compressed'] is True
    assert layout['object_ref'] == archived['object_ref']
    assert layout['physical_sha256'] != f['item']['sha256']
    assert (folder / 'manifest.json').read_bytes() == manifest_raw
    assert maintenance.file_hash(folder / 'ledger.sqlite3') == database_sha
    native_item = next(i for i in backup['evidence']['files'] if i['path'] == f['native']['path'])
    assert maintenance.backup_member_layout(folder / 'evidence', native_item)['legacy_compressed'] is False
    before = tree(book.root)
    with decision_evidence.verification_scope(book.root, persist=True):
        verified = book.verify_backup(backup['backup_id'])
    assert verified['state'] == 'READY' and verified['schema_version'] == 3, verified
    assert verified['evidence_file_count'] == len(backup['evidence']['files'])
    assert tree(book.root) == before
    target = tmp_path / 'restored'
    assert book.restore_backup(backup['backup_id'], target)['state'] == 'RESTORED'
    assert (target / f['proof']['path']).read_bytes() == f['raw']
    assert (target / f['native']['path']).read_bytes() == (folder / 'evidence' / f['native']['path']).read_bytes()
    assert (target / f['other']['path']).read_bytes() == (folder / 'evidence' / f['other']['path']).read_bytes()
    assert not (target / archived['object_ref']['_evidence_ref']['path']).exists()
    assert maintenance.file_hash(target / 'data/state/ledger.sqlite3') == database_sha
    assert (folder / 'manifest.json').read_bytes() == manifest_raw
    assert maintenance.file_hash(folder / 'ledger.sqlite3') == database_sha
    assert decision_evidence.read_business_document(target, f['proof'])[0]['exact'] == '旧原文'


@pytest.mark.parametrize('damage', [
    'missing_cas', 'corrupt_cas', 'wrong_raw_sha', 'wrong_logical_hash',
    'extra_field', 'unknown_format', 'not_wrapper', 'wrong_raw_size', 'wrong_stored_sha',
])
def test_invalid_legacy_layout_rejects_before_restore_tree(legacy_archive, tmp_path, damage):
    f = legacy_archive
    archive = compress(f)
    path = f['folder'] / 'evidence' / f['proof']['path']
    wrapper = json.loads(path.read_bytes())
    cas = f['folder'] / 'evidence' / archive['object_ref']['_evidence_ref']['path']
    if damage == 'missing_cas':
        cas.unlink()
    elif damage == 'corrupt_cas':
        raw = cas.read_bytes()
        replace_bytes(cas, b'X' + raw[1:])
    else:
        if damage == 'wrong_raw_sha':
            wrapper['object_ref'] = evidence_objects.put_blob(f['folder'] / 'evidence', b'other bytes')
        elif damage == 'wrong_logical_hash':
            wrapper['logical_content_hash'] = 'a' * 64
        elif damage == 'extra_field':
            wrapper['unknown'] = True
        elif damage == 'unknown_format':
            wrapper['storage_format'] = 'cas-json-unknown'
        elif damage == 'not_wrapper':
            wrapper = {'not': 'an accepted representation'}
        elif damage == 'wrong_raw_size':
            wrapper['object_ref']['_evidence_ref']['bytes'] += 1
        else:
            wrapper['object_ref']['_evidence_ref']['stored_sha256'] = 'a' * 64
        replace_bytes(path, canonical(wrapper).encode())
    assert f['book'].verify_backup(f['backup']['backup_id'])['state'] == 'INTEGRITY_ERROR'
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        f['book'].restore_backup(f['backup']['backup_id'], target)
    assert not target.exists()


@pytest.mark.parametrize('damage', ['missing_content_hash', 'wrong_content_hash', 'wrong_path', 'not_plan_path'])
def test_legacy_member_manifest_path_and_logical_identity_must_match(legacy_archive, damage):
    f = legacy_archive
    compress(f)
    item = dict(f['item'])
    if damage == 'missing_content_hash':
        item.pop('content_hash')
    elif damage == 'wrong_content_hash':
        item['content_hash'] = 'f' * 64
    else:
        source = f['folder'] / 'evidence' / item['path']
        item['path'] = ('data/plans/legacy-test/' + 'e' * 64 + '.json' if damage == 'wrong_path'
                        else 'data/other/' + item['content_hash'] + '.json')
        destination = f['folder'] / 'evidence' / item['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    with pytest.raises(ValueError):
        maintenance.backup_member_layout(f['folder'] / 'evidence', item)


@pytest.mark.parametrize('omitted', ['native_cas', 'raw_document'])
def test_only_legacy_wrapper_dependencies_can_extend_manifest_closure(legacy_archive, omitted):
    f = legacy_archive
    compress(f)
    manifest_path = f['folder'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    native_wrapper = json.loads((f['folder'] / 'evidence' / f['native']['path']).read_bytes())
    missing = (native_wrapper['object_ref']['_evidence_ref']['path'] if omitted == 'native_cas'
               else f['other']['path'])
    manifest['evidence']['files'] = [i for i in manifest['evidence']['files'] if i['path'] != missing]
    assert (f['folder'] / 'evidence' / missing).exists(), 'the physical extra must exist'
    manifest_path.write_text(canonical(manifest))
    assert f['book'].verify_backup(f['backup']['backup_id'])['state'] == 'INTEGRITY_ERROR'


@pytest.mark.parametrize('damage', ['wrapper', 'cas', 'missing_cas'])
def test_cached_legacy_validation_binds_wrapper_and_cas_ctime(legacy_archive, monkeypatch, damage):
    f = legacy_archive
    archive = compress(f)
    relative = (f['proof']['path'] if damage == 'wrapper'
                else archive['object_ref']['_evidence_ref']['path'])
    path = f['folder'] / 'evidence' / relative
    # CAS is intentionally read-only; allow the isolated corruption injection
    # before establishing the validation proof, without changing the inode.
    if damage == 'cas':
        path.chmod(0o600)
    assert f['book'].verify_backup(f['backup']['backup_id'])['state'] == 'READY'
    original = maintenance._ledger_closure
    monkeypatch.setattr(maintenance, '_ledger_closure', lambda *a: pytest.fail('stable backup was reread'))
    before = tree(f['book'].root)
    assert f['book'].verify_backup(f['backup']['backup_id'])['state'] == 'READY'
    assert tree(f['book'].root) == before
    monkeypatch.setattr(maintenance, '_ledger_closure', original)
    stamp = path.stat()
    if damage == 'missing_cas':
        path.unlink()
    else:
        raw = path.read_bytes()
        # In-place same inode, byte count and restored mtime: only ctime changes.
        with path.open('r+b') as stream:
            stream.write(b'X' + raw[1:])
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert path.stat().st_ino == stamp.st_ino
        assert path.stat().st_size == stamp.st_size
        assert path.stat().st_mtime_ns == stamp.st_mtime_ns
        assert path.stat().st_ctime_ns != stamp.st_ctime_ns
    assert f['book'].verify_backup(f['backup']['backup_id'])['state'] == 'INTEGRITY_ERROR'


def test_layout_only_preflight_never_claims_full_content_verification(legacy_archive, monkeypatch):
    f = legacy_archive
    archive = compress(f)
    path = f['folder'] / 'evidence' / archive['object_ref']['_evidence_ref']['path']
    raw = path.read_bytes()
    replace_bytes(path, b'X' + raw[1:])
    original = evidence_objects.verify
    monkeypatch.setattr(evidence_objects, 'verify', lambda *a, **k: pytest.fail('preflight inflated CAS'))
    layout = maintenance.backup_member_layout(f['folder'] / 'evidence', f['item'], verify=False)
    assert layout['legacy_compressed'] is True
    monkeypatch.setattr(evidence_objects, 'verify', original)
    with pytest.raises(ValueError):
        maintenance.backup_member_layout(f['folder'] / 'evidence', f['item'])


def test_native_matching_sha_does_not_reinterpret_wrapper(legacy_archive, monkeypatch):
    f = legacy_archive
    item = next(i for i in f['backup']['evidence']['files'] if i['path'] == f['native']['path'])
    monkeypatch.setattr(maintenance, 'document_layout', lambda *a: pytest.fail('native member was reinterpreted'))
    result = maintenance.backup_member_layout(f['folder'] / 'evidence', item)
    assert result['legacy_compressed'] is False
    assert result['physical_sha256'] == item['sha256']
    assert result['storage_dependencies'] == []


@pytest.mark.parametrize('symlink', ['wrapper', 'cas'])
def test_legacy_member_rejects_symlink_even_to_valid_local_bytes(legacy_archive, symlink):
    f = legacy_archive
    archive = compress(f)
    relative = f['proof']['path'] if symlink == 'wrapper' else archive['object_ref']['_evidence_ref']['path']
    path = f['folder'] / 'evidence' / relative
    saved = path.with_name(path.name + '.original')
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises(ValueError):
        maintenance.backup_member_layout(f['folder'] / 'evidence', f['item'])


def test_legacy_restore_streams_original_without_serializing_or_copying_wrapper(legacy_archive, tmp_path, monkeypatch):
    f = legacy_archive
    compress(f)
    assert f['book'].verify_backup(f['backup']['backup_id'])['state'] == 'READY'
    original = evidence_objects.iter_bytes
    seen = []

    def chunks(root, reference, **kwargs):
        for chunk in original(root, reference, **kwargs):
            if reference['_evidence_ref']['sha256'] == f['item']['sha256']:
                seen.append(hashlib.sha256(chunk).hexdigest())
                for start in range(0, len(chunk), 7):
                    yield chunk[start:start + 7]
            else:
                yield chunk

    monkeypatch.setattr(evidence_objects, 'iter_bytes', chunks)
    target = tmp_path / 'streamed'
    assert f['book'].restore_backup(f['backup']['backup_id'], target)['state'] == 'RESTORED'
    assert seen and (target / f['proof']['path']).read_bytes() == f['raw']
