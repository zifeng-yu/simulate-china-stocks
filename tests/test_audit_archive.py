import hashlib
import json

import pytest

from china_stock_sim.common.audit_archive import (
    DIRECTORY,
    MAGIC,
    AuditArchiveError,
    archive_file,
    iter_evidence,
    layout,
    read_evidence,
    verify_evidence,
    verify_manifest,
)


def _original(root, raw):
    checksum = hashlib.sha256(raw).hexdigest()
    path = root / DIRECTORY / f'{checksum}.evidence'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return checksum, path


def test_preview_is_read_only_and_old_sha_resolves_after_archive(tmp_path):
    raw = b'\xff<html>' + b'original report bytes' * 1000 + b'</html>\n'
    checksum, path = _original(tmp_path, raw)
    manifest = {'file': {'path': 'reports/old.html', 'sha256': checksum,
                         'preserved_path': str(path.relative_to(tmp_path))}}
    old_manifest = json.dumps(manifest, sort_keys=True)
    preview = archive_file(tmp_path, checksum)
    assert preview['changed'] is False
    assert preview['estimated_compressed_bytes'] < len(raw) // 10
    assert path.read_bytes() == raw
    assert not (tmp_path / 'data/normalized/evidence-objects').exists()
    assert layout(tmp_path, checksum)['storage'] == 'RAW'
    assert verify_manifest(tmp_path, manifest)['verified_count'] == 1
    result = archive_file(tmp_path, checksum, apply=True)
    assert result['changed'] is True
    assert path.read_bytes().startswith(MAGIC)
    assert hashlib.sha256(path.read_bytes()).hexdigest() != checksum
    assert read_evidence(tmp_path, checksum) == raw
    assert read_evidence(tmp_path, manifest['file']) == raw
    assert b''.join(iter_evidence(tmp_path, str(path.relative_to(tmp_path)))) == raw
    assert verify_evidence(tmp_path, checksum)['sha256'] == checksum
    assert verify_manifest(tmp_path, manifest)['state'] == 'READY'
    assert json.dumps(manifest, sort_keys=True) == old_manifest
    assert archive_file(tmp_path, checksum, apply=True)['changed'] is False


def test_same_old_sha_requires_no_duplicate_archive_object(tmp_path):
    checksum, _ = _original(tmp_path, b'preserved source\n' * 1000)
    archive_file(tmp_path, checksum, apply=True)
    archive_file(tmp_path, checksum, apply=True)
    assert len(list((tmp_path / 'data/normalized/evidence-objects').rglob('*.bin.zlib'))) == 1


def test_wrong_original_hash_is_not_overwritten(tmp_path):
    checksum, path = _original(tmp_path, b'original')
    path.write_bytes(b'changed')
    for apply in (False, True):
        with pytest.raises(AuditArchiveError, match='HASH_MISMATCH'):
            archive_file(tmp_path, checksum, apply=apply)
        assert path.read_bytes() == b'changed'


def test_corrupt_compressed_object_does_not_fall_back_to_other_bytes(tmp_path):
    checksum, _ = _original(tmp_path, b'proof' * 1000)
    result = archive_file(tmp_path, checksum, apply=True)
    obj = tmp_path / result['object_ref']['_evidence_ref']['path']
    obj.chmod(0o644)
    obj.write_bytes(b'wrong')
    with pytest.raises(AuditArchiveError, match='CORRUPT'):
        verify_evidence(tmp_path, checksum)


def test_unknown_path_symlink_and_wrong_expected_sha_are_rejected(tmp_path):
    checksum, path = _original(tmp_path, b'original')
    with pytest.raises(AuditArchiveError):
        read_evidence(tmp_path, '../../anything')
    with pytest.raises(AuditArchiveError, match='HASH_MISMATCH'):
        read_evidence(tmp_path, checksum, '0' * 64)
    outside = tmp_path / 'other'
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(AuditArchiveError, match='UNSAFE_PATH'):
        verify_evidence(tmp_path, checksum)


def test_read_limit_and_corrupt_wrapper(tmp_path):
    checksum, path = _original(tmp_path, b'A' * 1000)
    with pytest.raises(AuditArchiveError, match='READ_LIMIT'):
        read_evidence(tmp_path, checksum, max_bytes=10)
    archive_file(tmp_path, checksum, apply=True)
    with pytest.raises(AuditArchiveError, match='READ_LIMIT'):
        read_evidence(tmp_path, checksum, max_bytes=10)
    path.chmod(0o644)
    body = json.loads(path.read_bytes()[len(MAGIC):])
    body['sha256'] = '0' * 64
    path.write_bytes(MAGIC + json.dumps(body).encode())
    with pytest.raises(AuditArchiveError, match='INVALID'):
        verify_evidence(tmp_path, checksum)


def test_empty_manifest_does_not_claim_full_receipt_verification(tmp_path):
    result = verify_manifest(tmp_path, {'sha256': '0' * 64, 'unrelated': 'not checked'})
    assert result['state'] == 'NOT_APPLICABLE'
    assert 'other receipt fields not verified' in result['coverage']


def test_arbitrary_original_magic_prefix_is_preserved(tmp_path):
    raw = MAGIC + b'this is original text rather than an archive wrapper\n'
    checksum, _ = _original(tmp_path, raw)
    assert read_evidence(tmp_path, checksum) == raw
    archive_file(tmp_path, checksum, apply=True)
    assert read_evidence(tmp_path, checksum) == raw
