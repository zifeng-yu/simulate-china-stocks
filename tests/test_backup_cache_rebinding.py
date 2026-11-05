"""Writer-controlled aliases preserve strict, reusable validation across backups."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from execution_fixtures import FIRST, execution_book
from test_audit_summary_cache import warm_summary
from test_execution_decision_audit import freeze, strategy_request
from test_ledger_backup_objects import add_record, object_book
from test_terminal_execution_status import app, completed

from china_stock_sim.common import evidence_objects
from china_stock_sim.ledger import decision_evidence as cache
from china_stock_sim.ledger import maintenance
from china_stock_sim.ledger.backup_links import VerifiedLinkBatch
from china_stock_sim.ledger.document_objects import archive_document, file_identity
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import canonical, digest
from china_stock_sim.ledger.plan_storage import plan_header
from china_stock_sim.reporting import audit

assert execution_book and object_book


def raw_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_memory():
    cache._VALIDATIONS.clear()
    cache._DOCUMENTS.clear()
    cache._DOCUMENT_STAMPS.clear()
    cache._DOCUMENT_BYTES = 0


def seed(book, *, kind='public_plan_summary', label='one', compressed=False):
    ref = document(book.root, 'rebinding-tests', {'label': label, 'evidence': 'x' * 100})
    if compressed:
        archive_document(book.root, ref)
    _, info = cache.read_business_document(book.root, ref)
    files = [info, *info.get('storage_dependencies', [])]
    content_hash = digest({'test_key': label})
    details = {'files': deepcopy(files), 'semantic': 'must remain unchanged'}
    with cache.verification_scope(book.root, persist=True):
        cache.remember_validation(book.root, kind, content_hash, files, details=details)
    return kind, content_hash, files


def linked(book, files, name='manual-alias'):
    batch = VerifiedLinkBatch(book.root)
    destination = book.root / 'data/backups/ledger' / name / 'evidence'
    for info in files:
        maintenance._copy_evidence(book.root, destination, info, link_batch=batch)
    return batch, destination


def rebind(book, batch, **kwargs):
    with cache.verification_scope(book.root, persist=True):
        return cache.rebind_verified_dependencies(book.root, batch.transitions(), **kwargs)


def test_fixed_proof_and_returned_closure_rebind_without_changing_details(object_book):
    book = object_book
    kind, key, files = seed(book, kind='plan_decision', compressed=True)
    identity = cache._validation_key(book.root, kind, key)
    old = deepcopy(cache._receipt(book.root, identity))
    batch, _ = linked(book, files)
    assert not cache.cached_validation(book.root, kind, key)
    result = rebind(book, batch)
    assert result['state'] == 'COMPLETE' and result['rebound'] == 1
    current = cache._receipt(book.root, identity)
    assert current['details'] == old['details']
    assert current['dependencies'] != old['dependencies']
    projected = cache._project_cached_files(current)
    for item in projected:
        assert item['file_identity'] == file_identity(book.root / item['path'])
        for child in item.get('storage_dependencies', []):
            assert child['file_identity'] == file_identity(book.root / child['path'])
    clear_memory()
    assert cache.cached_validation(book.root, kind, key)
    # A summary cache miss must be able to consume current closure metadata.
    with cache.verification_scope(book.root, persist=True):
        cache.remember_validation(book.root, 'public_plan_summary', key, projected)
    assert cache.cached_validation(book.root, 'public_plan_summary', key)


def test_cached_reader_reloads_other_writer_rebound_proof_without_write(object_book):
    book = object_book
    kind, key, files = seed(book)
    identity = cache._validation_key(book.root, kind, key)
    stale_reader_memory = deepcopy(cache._VALIDATIONS[identity])
    batch, _ = linked(book, files)
    rebind(book, batch)
    cache._VALIDATIONS[identity] = stale_reader_memory
    path = cache._receipt_path(book.root, identity)
    before = path.read_bytes()
    assert cache.cached_validation(book.root, kind, key)
    assert path.read_bytes() == before


def test_only_public_summary_miss_can_use_rebound_plan_decision(execution_book, monkeypatch):
    book = execution_book
    request, _ = strategy_request(book, nested=True)
    freeze(book, request)
    report = book.report_snapshot('LLM', FIRST, mode='recent')
    warm_summary(book, report)
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    clear_memory()
    for path in (book.root / 'data/state/verification').glob('*.json'):
        proof = json.loads(path.read_bytes())['verification']
        if proof.get('kind') == 'public_plan_summary':
            path.unlink()
    real_project = cache._project_cached_files
    calls = []
    def observed(proof):
        calls.append(proof['kind'])
        return real_project(proof)
    monkeypatch.setattr(cache, '_project_cached_files', observed)
    with cache.verification_scope(book.root, persist=True):
        result = audit.audit_account(book.root, report, materialize=False)
    assert result['state'] == 'COMPLETE', result
    assert calls == ['plan_decision']


def test_readonly_scope_never_rebinds_or_writes(object_book):
    book = object_book
    kind, key, files = seed(book)
    batch, _ = linked(book, files)
    directory = book.root / 'data/state/verification'
    before = {p.name: p.read_bytes() for p in directory.glob('*.json')}
    with cache.verification_scope(book.root, persist=False):
        assert cache.rebind_verified_dependencies(book.root, batch.transitions())['state'] == 'SKIPPED'
        assert not cache.cached_validation(book.root, kind, key)
    assert {p.name: p.read_bytes() for p in directory.glob('*.json')} == before


@pytest.mark.parametrize('damage', ['same_size_mtime', 'replacement', 'missing', 'symlink', 'chmod'])
@pytest.mark.parametrize('when', ['before_link', 'after_link'])
def test_unknown_changes_never_receive_a_transition(object_book, damage, when):
    book = object_book
    kind, key, files = seed(book)
    path = book.root / files[0]['path']
    def change():
        raw, before = path.read_bytes(), path.stat()
        if damage == 'same_size_mtime':
            path.write_bytes(raw.replace(b'xxx', b'yxx', 1))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif damage == 'replacement':
            path.unlink()
            path.write_bytes(raw)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif damage == 'missing':
            path.unlink()
        elif damage == 'symlink':
            target = path.with_suffix('.target')
            target.write_bytes(raw)
            path.unlink()
            path.symlink_to(target)
        else:
            path.chmod(0o640 if before.st_mode & 0o777 != 0o640 else 0o600)
    if when == 'before_link':
        change()
        try:
            batch, _ = linked(book, files)
        except (ValueError, OSError):
            batch = VerifiedLinkBatch(book.root)
    else:
        batch, _ = linked(book, files)
        change()
    rebind(book, batch)
    clear_memory()
    assert not cache.cached_validation(book.root, kind, key)


def test_uncontrolled_second_link_and_corrupt_alias_fail_closed(object_book):
    book = object_book
    kind, key, files = seed(book)
    batch, destination = linked(book, files)
    source = book.root / files[0]['path']
    os.link(source, source.with_suffix('.outside'))
    assert batch.transitions() == []
    rebind(book, batch)
    assert not cache.cached_validation(book.root, kind, key)
    (destination / files[0]['path']).write_bytes(b'changed original through alias')
    assert not cache.cached_validation(book.root, kind, key)


def test_link_mode_change_fails_backup_copy_before_authorization(object_book, monkeypatch):
    book = object_book
    _, _, files = seed(book)
    link = maintenance.os.link
    def changed(source, destination):
        link(source, destination)
        source.chmod(0o640)
    monkeypatch.setattr(maintenance.os, 'link', changed)
    with pytest.raises(ValueError, match='备份期间证据变化'):
        linked(book, files)


def test_archive_root_symlink_is_rejected_before_any_link(object_book):
    book = object_book
    kind, key, files = seed(book)
    real = book.root / 'unexpected-copy-target'
    real.mkdir()
    archive = book.root / 'data/backups/ledger/manual-alias'
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.symlink_to(real, target_is_directory=True)
    source = book.root / files[0]['path']
    before = file_identity(source)
    with pytest.raises(ValueError, match='符号链接'):
        linked(book, files)
    assert list(real.iterdir()) == [] and file_identity(source) == before
    assert cache.cached_validation(book.root, kind, key)


def test_copy_fallback_does_not_authorize_ctime(object_book, monkeypatch):
    book = object_book
    kind, key, files = seed(book)
    monkeypatch.setattr(maintenance.os, 'link', lambda *a: (_ for _ in ()).throw(OSError('EXDEV')))
    batch, destination = linked(book, files)
    assert batch.transitions() == []
    assert rebind(book, batch)['state'] == 'SKIPPED'
    assert not (book.root / files[0]['path']).samefile(destination / files[0]['path'])
    assert cache.cached_validation(book.root, kind, key)


@pytest.mark.parametrize('damage', ['old_validator', 'bad_digest', 'unknown_kind', 'changed_dependency'])
def test_old_or_invalid_receipts_are_not_upgraded(object_book, damage):
    book = object_book
    kind, key, files = seed(book)
    identity = cache._validation_key(book.root, kind, key)
    path = cache._receipt_path(book.root, identity)
    envelope = json.loads(path.read_bytes())
    if damage == 'old_validator':
        envelope['verification']['validator'] = '0' * 64
    elif damage == 'unknown_kind':
        envelope['verification']['kind'] = 'unreviewed_verifier'
    elif damage == 'changed_dependency':
        envelope['verification']['dependencies'][0]['file_identity'][4] -= 1
    envelope['sha256'] = '0' * 64 if damage == 'bad_digest' else digest(envelope['verification'])
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    clear_memory()
    batch, _ = linked(book, files)
    assert rebind(book, batch)['rebound'] == 0
    assert path.read_bytes() == before
    assert not cache.cached_validation(book.root, kind, key)


@pytest.mark.parametrize('limits', [{'max_receipts': 0}, {'max_bytes': 0}])
def test_scan_budget_is_bounded_and_only_loses_optimization(object_book, limits):
    book = object_book
    kind, key, files = seed(book)
    batch, _ = linked(book, files)
    result = rebind(book, batch, **limits)
    assert result['truncated'] and result['rebound'] == 0 and result['read_bytes'] == 0
    clear_memory()
    assert not cache.cached_validation(book.root, kind, key)


def test_missing_authorization_after_interruption_stays_cold(object_book):
    book = object_book
    kind, key, files = seed(book)
    linked(book, files)  # Simulated process exit discards its in-memory transitions.
    clear_memory()
    assert not cache.cached_validation(book.root, kind, key)
    assert rebind(book, VerifiedLinkBatch(book.root))['state'] == 'SKIPPED'


def test_partial_receipt_publication_only_leaves_unwritten_proofs_cold(object_book, monkeypatch):
    book = object_book
    first = seed(book, label='first')
    second = seed(book, label='second')
    batch, _ = linked(book, first[2] + second[2])
    save = cache._save_receipt
    calls = []
    def interrupted(*args, **kwargs):
        if calls:
            return
        calls.append(1)
        save(*args, **kwargs)
    monkeypatch.setattr(cache, '_save_receipt', interrupted)
    result = rebind(book, batch)
    assert result['state'] == 'PARTIAL' and result['rebound'] == result['write_failures'] == 1
    clear_memory()
    states = [cache.cached_validation(book.root, kind, key) for kind, key, _ in [first, second]]
    assert states.count(True) == states.count(False) == 1


def test_unrelated_changed_dependency_cannot_be_blessed_by_linking_another(object_book):
    book = object_book
    first = seed(book, label='first')
    second = seed(book, label='second')
    key = digest('both')
    with cache.verification_scope(book.root, persist=True):
        cache.remember_validation(book.root, 'public_plan_summary', key, first[2] + second[2])
    batch, _ = linked(book, first[2])
    path = book.root / second[2][0]['path']
    path.write_bytes(path.read_bytes().replace(b'xxx', b'yxx', 1))
    rebind(book, batch)
    clear_memory()
    assert not cache.cached_validation(book.root, 'public_plan_summary', key)


def test_oversized_or_malformed_receipts_do_not_expand_scan(object_book):
    book = object_book
    kind, key, files = seed(book)
    batch, _ = linked(book, files)
    directory = book.root / 'data/state/verification'
    (directory / 'oversized.json').write_bytes(b'x' * 2_000_001)
    (directory / 'malformed.json').write_text('[]')
    result = rebind(book, batch)
    assert result['invalid'] == 2 and result['read_bytes'] < 10_000
    assert cache.cached_validation(book.root, kind, key)


def test_non_json_entries_also_consume_directory_scan_budget(object_book):
    book = object_book
    _, _, files = seed(book)
    batch, _ = linked(book, files)
    for number in range(4):
        (book.root / 'data/state/verification' / f'temporary-{number}').touch()
    result = rebind(book, batch, max_receipts=2)
    assert result['truncated'] and result['scanned'] == 2


def test_hot_proof_wins_before_oversized_history_and_survives_new_process(object_book):
    book = object_book
    directory = book.root / 'data/state/verification'
    directory.mkdir(parents=True, exist_ok=True)
    # Deliberately exceed the test budget with unrelated historical candidates.
    for number in range(25):
        (directory / f'old-document-{number:03}.json').write_text('{"old": true}')
    kind, key, files = seed(book)
    proof_id = cache._validation_key(book.root, kind, key)
    with cache.verification_scope(book.root, persist=True):
        before = {p.name: p.read_bytes() for p in directory.glob('*.json')}
        with cache.verification_scope(book.root, persist=False):
            assert cache.cached_validation(book.root, kind, key)
        assert {p.name: p.read_bytes() for p in directory.glob('*.json')} == before
        assert list(cache._SCOPE.get()['touched']) == [proof_id]
        cache._VALIDATIONS.clear()  # In-memory LRU eviction cannot lose the key.
        batch, _ = linked(book, files)
        result = cache.rebind_verified_dependencies(book.root, batch.transitions(), max_receipts=1)
    assert result['priority_rebound'] == result['rebound'] == 1
    assert result['scanned'] == 1 and result['truncated']
    child = '''
import sys
from pathlib import Path
from china_stock_sim.ledger import decision_evidence as cache
root=Path(sys.argv[1]);before={str(p):p.read_bytes() for p in (root/'data/state/verification').glob('*.json')}
with cache.verification_scope(root,persist=False):
    assert cache.cached_validation(root,sys.argv[2],sys.argv[3])
assert before=={str(p):p.read_bytes() for p in (root/'data/state/verification').glob('*.json')}
'''
    child_result = subprocess.run([sys.executable, '-c', child, str(book.root), kind, key],
                                  capture_output=True, text=True, timeout=15)
    assert child_result.returncode == 0, child_result.stderr


def test_nested_scopes_share_only_same_root_touch_keys(object_book):
    book = object_book
    other = SimpleNamespace(root=book.root / 'other-isolated-root')
    other.root.mkdir()
    kind, key, _ = seed(book)
    other_kind, other_key, _ = seed(other, label='other')
    with cache.verification_scope(book.root, persist=True):
        with cache.verification_scope(other.root, persist=False):
            assert cache.cached_validation(other.root, other_kind, other_key)
            assert len(cache._SCOPE.get()['touched']) == 1
        assert list(cache._SCOPE.get()['touched']) == []
        with cache.verification_scope(book.root, persist=False):
            assert cache.cached_validation(book.root, kind, key)
        assert list(cache._SCOPE.get()['touched']) == [cache._validation_key(book.root, kind, key)]


def test_first_touch_limit_does_not_evict_execution_keys(object_book):
    root = object_book.root
    with cache.verification_scope(root, persist=True):
        first = digest('first')
        cache._touch_fixed(root, 'frozen_plan_header', first)
        for number in range(4097):
            cache._touch_fixed(root, 'opaque_evidence_object', digest(number))
        cache._touch_fixed(root, 'statement_query', digest('not eligible'))
        touched = cache._SCOPE.get()['touched']
        assert len(touched) == 4096 and next(iter(touched)) == first
        assert digest('not eligible') not in touched


def test_priority_and_flat_share_byte_budget_and_do_not_parse_twice(object_book):
    book = object_book
    kind, key, files = seed(book)
    path = cache._receipt_path(book.root, cache._validation_key(book.root, kind, key))
    size = path.stat().st_size
    with cache.verification_scope(book.root, persist=True):
        assert cache.cached_validation(book.root, kind, key)
        batch, _ = linked(book, files)
        result = cache.rebind_verified_dependencies(book.root, batch.transitions(), max_bytes=size)
    assert result['priority_rebound'] == 1 and result['rebound'] == 1
    assert result['read_bytes'] == size and not result['truncated']


def test_cache_write_failure_does_not_invalidate_completed_backup(object_book, monkeypatch):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'stable': 'evidence'})
    add_record(book, {'proof': ref})
    monkeypatch.setattr(cache, '_save_receipt', lambda *a, **k: None)
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    clear_memory()
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'


def test_rebinding_exception_does_not_change_backup_result(object_book, monkeypatch):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'stable': 'evidence'})
    add_record(book, {'proof': ref})
    monkeypatch.setattr(maintenance, 'rebind_verified_dependencies',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('isolated cache fault')))
    backup = book.backup(label='manual')
    assert backup['state'] == 'COMPLETE'
    assert backup['validation_cache']['state'] == 'CACHE_UNAVAILABLE'
    assert book.verify_backup(backup['backup_id'])['state'] == 'READY'


def corrupt_preserving_size_mtime(path):
    before, raw = path.stat(), bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 1
    path.chmod(0o600)
    path.write_bytes(raw)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    path.chmod(before.st_mode & 0o777)


@pytest.mark.parametrize('target', ['evidence', 'database', 'configuration', 'manifest'])
def test_new_backup_proof_cannot_bless_mutation_after_copy_hash(object_book, monkeypatch, target):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'stable': 'evidence'})
    add_record(book, {'proof': ref})
    real_rebind = maintenance.rebind_verified_dependencies
    def changed(*args, **kwargs):
        result = real_rebind(*args, **kwargs)
        folder = next((book.root / 'data/backups/ledger').glob('manual-*'))
        path = {'evidence': folder / 'evidence' / ref['_evidence_ref']['path'],
                'database': folder / 'ledger.sqlite3',
                'configuration': folder / 'configuration/configs/default.toml',
                'manifest': folder / 'manifest.json'}[target]
        corrupt_preserving_size_mtime(path)
        return result
    monkeypatch.setattr(maintenance, 'rebind_verified_dependencies', changed)
    with pytest.raises(ValueError, match='核验后|非受控变化'):
        book.backup(label='manual')
    folder = next((book.root / 'data/backups/ledger').glob('manual-*'))
    assert (folder / 'failure.json').exists()
    clear_memory()
    assert book.verify_backup(folder.name)['state'] == 'INTEGRITY_ERROR'


@pytest.mark.parametrize('target', ['evidence', 'database', 'configuration', 'manifest'])
def test_full_backup_verify_does_not_bind_fresh_unverified_stamps(object_book, monkeypatch, target):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'stable': 'evidence'})
    add_record(book, {'proof': ref})
    saved = book.backup(label='manual')
    folder = book.root / 'data/backups/ledger' / saved['backup_id']
    proof_path = cache._receipt_path(book.root,
        cache._validation_key(book.root, maintenance.BACKUP_VALIDATION_KIND,
                              digest(json.loads((folder / 'manifest.json').read_bytes()))))
    proof_path.unlink()
    clear_memory()
    real_closure = maintenance._ledger_closure
    def changed(*args, **kwargs):
        result = real_closure(*args, **kwargs)
        path = {'evidence': folder / 'evidence' / ref['_evidence_ref']['path'],
                'database': folder / 'ledger.sqlite3',
                'configuration': folder / 'configuration/configs/default.toml',
                'manifest': folder / 'manifest.json'}[target]
        corrupt_preserving_size_mtime(path)
        return result
    monkeypatch.setattr(maintenance, '_ledger_closure', changed)
    assert book.verify_backup(saved['backup_id'])['state'] == 'INTEGRITY_ERROR'
    assert not proof_path.exists()
    clear_memory()
    assert book.verify_backup(saved['backup_id'])['state'] == 'INTEGRITY_ERROR'


def test_real_daily_rotation_preserves_surviving_backup_proofs(object_book, monkeypatch):
    book = object_book
    ref = evidence_objects.put_json(book.root, {'stable': 'small immutable object'})
    add_record(book, {'proof': ref})
    start = book._now()
    points = []
    for index in range(32):
        book.clock = lambda index=index: start + timedelta(minutes=index)
        point = book.backup(label='daily')
        assert point['state'] == 'COMPLETE'
        points.append(point)
    assert len(list((book.root / 'data/backups/ledger').glob('daily-*'))) == 30
    assert not (book.root / 'data/backups/ledger' / points[0]['backup_id']).exists()
    original = maintenance._ledger_closure
    monkeypatch.setattr(maintenance, '_ledger_closure', lambda *a: pytest.fail('retained backup went cold'))
    clear_memory()
    for point in points[-3:]:
        assert book.verify_backup(point['backup_id'])['state'] == 'READY'
    assert book.verify_backup(points[0]['backup_id'])['state'] == 'INTEGRITY_ERROR'
    monkeypatch.setattr(maintenance, '_ledger_closure', original)


def test_three_real_backups_cross_process_keep_terminal_and_summary_hot(execution_book, monkeypatch):
    book = execution_book
    original = completed(book)
    body = {k: v for k, v in original.items() if k not in {'id', 'document'}}
    body['large_original_fixture'] = 'x' * (18 * 1024 * 1024)
    reference = document(book.root, 'frozen', body)
    header = plan_header({**body, 'document': reference})
    with sqlite3.connect(book.settings.database) as db:
        db.execute('UPDATE plans SET data=? WHERE account_id=? AND id=?',
                   (canonical(header), 'LLM', original['plan_id']))
    with cache.verification_scope(book.root, persist=True):
        assert app(book).settle('LLM', original['plan_id'], operation_id='warm-terminal')['completion_status'] == 'COMPLETE'
    report = book.report_snapshot('LLM', FIRST, mode='recent')
    warm_summary(book, report)
    before = raw_hash(book.settings.database)
    child = r'''
import json,sys
from pathlib import Path
from china_stock_sim.ledger import LedgerService,decision_evidence as cache
from china_stock_sim.reporting import audit
root=Path(sys.argv[1]);book=LedgerService(root,test_mode=True)
reader=cache.read_document_bytes
def bounded(root,layout,**kwargs):
    size=(layout['wrapper']['object_ref']['_evidence_ref']['bytes'] if layout['wrapper'] else layout['file_identity'][2])
    assert size < 100_000, 'hot read expanded large original'
    return reader(root,layout,**kwargs)
cache.read_document_bytes=bounded
before={str(p):p.read_bytes() for p in (root/'data/state/verification').glob('*.json')}
with cache.verification_scope(root,persist=False) as metrics:
    result=book.terminal_execution_status('LLM',sys.argv[2])
    assert result['state']=='READY',result
    summary=audit.audit_account(root,book.report_snapshot('LLM',sys.argv[3],mode='recent'),materialize=False)
    assert summary['state']=='COMPLETE',summary
assert metrics['bytes_read'] < 10000,metrics
assert {str(p):p.read_bytes() for p in (root/'data/state/verification').glob('*.json')}==before
print(json.dumps(metrics))
'''
    for _ in range(3):
        backup = book.backup(label='manual')
        assert backup['state'] == 'COMPLETE', backup
        assert backup['validation_cache']['rebound'] > 0
        run = subprocess.run([sys.executable, '-c', child, str(book.root), original['plan_id'], FIRST],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr + run.stdout
        assert raw_hash(book.settings.database) == before
        with monkeypatch.context() as patch:
            patch.setattr(maintenance, '_ledger_closure', lambda *a: pytest.fail('just-created backup cold'))
            assert book.verify_backup(backup['backup_id'])['state'] == 'READY'
