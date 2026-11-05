"""Resolve historical audit bytes by their original SHA, raw or losslessly archived."""

import hashlib
import json
import os
import re
import stat
import tempfile
import zlib
from pathlib import Path

from china_stock_sim.common.evidence_objects import (
    CHUNK_BYTES,
    DEFAULT_READ_BYTES,
    EvidenceObjectError,
    is_reference,
    iter_bytes,
    put_blob,
)

DIRECTORY = 'data/normalized/project-audit-evidence'
MAGIC = b'CHINA_STOCK_AUDIT_ARCHIVE_V1\n'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_HEADER_LIMIT = 16 * 1024


class AuditArchiveError(ValueError):
    def __init__(self, code):
        self.code = self.state = code
        super().__init__(code)


def _error(code='AUDIT_REFERENCE_INVALID'):
    return AuditArchiveError(code)


def _identity(source, expected_sha=None):
    if isinstance(source, dict):
        expected_sha = expected_sha or source.get('sha256')
        source = source.get('preserved_path', source.get('path'))
    if isinstance(source, Path):
        source = str(source)
    if not isinstance(source, str):
        raise _error()
    if _HASH.fullmatch(source):
        source = f'{DIRECTORY}/{source}.evidence'
    prefix = DIRECTORY + '/'
    if not source.startswith(prefix) or '/' in source[len(prefix):]:
        raise _error()
    name = source[len(prefix):]
    checksum = name.removesuffix('.evidence')
    if not name.endswith('.evidence') or not _HASH.fullmatch(checksum):
        raise _error()
    if expected_sha is not None and expected_sha != checksum:
        raise _error('AUDIT_REFERENCE_HASH_MISMATCH')
    return source, checksum


def _path(root, relative):
    root = Path(root).resolve()
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise _error('AUDIT_UNSAFE_PATH')
    if not current.resolve().is_relative_to(root):
        raise _error('AUDIT_UNSAFE_PATH')
    return current


def layout(root, source, expected_sha=None):
    relative, checksum = _identity(source, expected_sha)
    try:
        path = _path(root, relative)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise _error('AUDIT_UNSAFE_PATH')
        with path.open('rb') as stream:
            header = stream.read(len(MAGIC))
            if header != MAGIC:
                return {'storage': 'RAW', 'path': relative, 'sha256': checksum,
                        'bytes': info.st_size, 'allocated_bytes': info.st_blocks * 512}
            raw = stream.read(_HEADER_LIMIT + 1)
        # The original can itself start with any byte sequence, including MAGIC.
        # Filename-bound original bytes take precedence over wrapper interpretation.
        original_digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
                original_digest.update(chunk)
        if original_digest.hexdigest() == checksum:
            return {'storage': 'RAW', 'path': relative, 'sha256': checksum,
                    'bytes': info.st_size, 'allocated_bytes': info.st_blocks * 512}
        if len(raw) > _HEADER_LIMIT:
            raise _error('AUDIT_ARCHIVE_INVALID')
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'format', 'sha256', 'bytes', 'object_ref'}
                or type(value['format']) is not int or value['format'] != 1
                or type(value['bytes']) is not int or value['bytes'] < 0
                or value['sha256'] != checksum or not is_reference(value['object_ref'])):
            raise _error('AUDIT_ARCHIVE_INVALID')
        metadata = value['object_ref']['_evidence_ref']
        if (metadata['encoding'] != 'bytes+zlib' or metadata['sha256'] != checksum
                or metadata['bytes'] != value['bytes']):
            raise _error('AUDIT_ARCHIVE_INVALID')
        return {'storage': 'COMPRESSED', 'path': relative, 'sha256': checksum,
                'bytes': value['bytes'], 'allocated_bytes': info.st_blocks * 512,
                'object_ref': value['object_ref']}
    except AuditArchiveError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise _error('AUDIT_ARCHIVE_UNAVAILABLE') from None


def iter_evidence(root, source, expected_sha=None, *, max_bytes=None):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise _error()
    current = layout(root, source, expected_sha)
    if max_bytes is not None and current['bytes'] > max_bytes:
        raise _error('AUDIT_READ_LIMIT')
    if current['storage'] == 'COMPRESSED':
        try:
            yield from iter_bytes(root, current['object_ref'], max_bytes=max_bytes)
        except EvidenceObjectError:
            raise _error('AUDIT_ARCHIVE_CORRUPT') from None
        return
    digest, size = hashlib.sha256(), 0
    try:
        with _path(root, current['path']).open('rb') as stream:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
                size += len(chunk)
                if size > current['bytes']:
                    raise _error('AUDIT_ORIGINAL_CHANGED')
                digest.update(chunk)
                yield chunk
        if size != current['bytes'] or digest.hexdigest() != current['sha256']:
            raise _error('AUDIT_ORIGINAL_HASH_MISMATCH')
    except OSError:
        raise _error('AUDIT_ARCHIVE_UNAVAILABLE') from None


def read_evidence(root, source, expected_sha=None, *, max_bytes=DEFAULT_READ_BYTES):
    return b''.join(iter_evidence(root, source, expected_sha, max_bytes=max_bytes))


def verify_evidence(root, source, expected_sha=None):
    current = layout(root, source, expected_sha)
    for _ in iter_evidence(root, source, expected_sha):
        pass
    return {'state': 'READY', **current}


def archive_file(root, source, expected_sha=None, *, apply=False):
    """Preview by default; explicit apply replaces only this original with a verified wrapper."""
    current = layout(root, source, expected_sha)
    if current['storage'] == 'COMPRESSED':
        verify_evidence(root, source, expected_sha)
        return {'state': 'READY', 'changed': False, 'action': 'ALREADY_ARCHIVED', **current}
    path = _path(root, current['path'])
    before = path.stat()
    if not apply:
        compressor, size = zlib.compressobj(level=6), 0
        for chunk in iter_evidence(root, source, expected_sha):
            size += len(compressor.compress(chunk))
        size += len(compressor.flush())
        return {'state': 'READY', 'changed': False, 'action': 'ARCHIVE', **current,
                'estimated_compressed_bytes': size,
                'mtime_ns': before.st_mtime_ns, 'inode': before.st_ino,
                'device': before.st_dev}
    with path.open('rb') as stream:
        reference = put_blob(root, stream, encoding='bytes+zlib')
    metadata = reference['_evidence_ref']
    if metadata['sha256'] != current['sha256'] or metadata['bytes'] != current['bytes']:
        raise _error('AUDIT_ORIGINAL_HASH_MISMATCH')
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise _error('AUDIT_ORIGINAL_CHANGED')
    wrapper = MAGIC + json.dumps({'format': 1, 'sha256': current['sha256'],
        'bytes': current['bytes'], 'object_ref': reference}, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.audit-', dir=path.parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(wrapper)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
        latest = path.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise _error('AUDIT_ORIGINAL_CHANGED')
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    verified = verify_evidence(root, source, expected_sha)
    return {'state': 'READY', 'changed': True, 'action': 'ARCHIVED', **verified,
            'wrapper_bytes': len(wrapper), 'compressed_bytes': metadata['stored_bytes']}


def verify_manifest(root, manifest):
    """Verify existing preserved_path mappings without rewriting sealed receipt JSON."""
    found = {}

    def visit(value):
        if isinstance(value, dict):
            preserved = value.get('preserved_path')
            if isinstance(preserved, str) and preserved.startswith(DIRECTORY + '/'):
                _, checksum = _identity(preserved, value.get('sha256'))
                found[checksum] = preserved
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(manifest)
    checks = [verify_evidence(root, path, checksum) for checksum, path in sorted(found.items())]
    return {'state': 'READY' if checks else 'NOT_APPLICABLE', 'verified_count': len(checks),
            'checks': checks, 'coverage': 'preserved_path mappings only; other receipt fields not verified'}
