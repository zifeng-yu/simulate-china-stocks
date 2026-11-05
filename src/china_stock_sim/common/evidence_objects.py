"""Immutable compressed evidence; ordinary ledger reads never expand these objects."""

import hashlib
import json
import os
import re
import stat
import tempfile
import zlib
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

DIRECTORY = 'data/normalized/evidence-objects'
CHUNK_BYTES = 1024 * 1024
DEFAULT_READ_BYTES = 64 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = {'path', 'sha256', 'bytes', 'encoding', 'stored_sha256', 'stored_bytes'}
_SUFFIX = {'json+zlib': 'json.zlib', 'bytes+zlib': 'bin.zlib'}


class EvidenceObjectError(ValueError):
    def __init__(self, code):
        self.code = self.state = code
        super().__init__(code)


def _error(code='EVIDENCE_OBJECT_INVALID'):
    return EvidenceObjectError(code)


def _metadata(reference):
    if not isinstance(reference, dict) or set(reference) != {'_evidence_ref'}:
        raise _error()
    value = reference['_evidence_ref']
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise _error()
    if any(not isinstance(value[k], str) or not _HASH.fullmatch(value[k])
           for k in ('sha256', 'stored_sha256')):
        raise _error()
    if any(type(value[k]) is not int or value[k] < 0 for k in ('bytes', 'stored_bytes')):
        raise _error()
    if not isinstance(value['encoding'], str) or value['encoding'] not in _SUFFIX:
        raise _error()
    expected = f'{DIRECTORY}/{value["sha256"][:2]}/{value["sha256"]}.{_SUFFIX[value["encoding"]]}'
    if value['path'] != expected:
        raise _error()
    return value


def is_reference(value):
    """Recognize the complete, versioned reference; malformed markers are not accepted."""
    try:
        _metadata(value)
        return True
    except EvidenceObjectError:
        return False


def _path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise _error('EVIDENCE_OBJECT_UNSAFE_PATH')
    if not path.resolve().is_relative_to(root):
        raise _error('EVIDENCE_OBJECT_UNSAFE_PATH')
    return path


def _limit(value):
    if value is not None and (type(value) is not int or value < 0):
        raise _error()


def _source_chunks(source):
    if isinstance(source, (bytes, bytearray, memoryview)):
        source = memoryview(source)
        for offset in range(0, len(source), CHUNK_BYTES):
            yield source[offset:offset + CHUNK_BYTES]
    elif callable(getattr(source, 'read', None)):
        while True:
            value = source.read(CHUNK_BYTES)
            if value == b'':
                break
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise _error()
            yield value
    else:
        for value in source:
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise _error()
            for offset in range(0, len(value), CHUNK_BYTES):
                yield memoryview(value)[offset:offset + CHUNK_BYTES]


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stored_identity(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise _error('EVIDENCE_OBJECT_UNSAFE_PATH')
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def put_blob(root, source, *, max_bytes=None, encoding='json+zlib'):
    """Stream exact bytes (including whitespace) into one shared compressed object.

    For json+zlib the caller guarantees JSON provenance, e.g. SQLite json_valid.
    Arbitrary originals use bytes+zlib. This operation never parses a giant record.
    """
    _limit(max_bytes)
    if not isinstance(encoding, str) or encoding not in _SUFFIX:
        raise _error()
    temporary = None
    try:
        directory = _path(root, DIRECTORY)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.object-', dir=directory)
        raw_hash, stored_hash = hashlib.sha256(), hashlib.sha256()
        raw_size = stored_size = 0
        compressor = zlib.compressobj(level=6)
        with os.fdopen(descriptor, 'wb') as stream:
            for chunk in _source_chunks(source):
                raw_size += len(chunk)
                if max_bytes is not None and raw_size > max_bytes:
                    raise _error('EVIDENCE_OBJECT_LIMIT')
                raw_hash.update(chunk)
                compressed = compressor.compress(chunk)
                stream.write(compressed)
                stored_hash.update(compressed)
                stored_size += len(compressed)
            compressed = compressor.flush()
            stream.write(compressed)
            stored_hash.update(compressed)
            stored_size += len(compressed)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
        identity = raw_hash.hexdigest()
        relative = f'{DIRECTORY}/{identity[:2]}/{identity}.{_SUFFIX[encoding]}'
        destination = _path(root, relative)
        destination.parent.mkdir(exist_ok=True, mode=0o700)
        reference = {'_evidence_ref': {'path': relative, 'sha256': identity,
            'bytes': raw_size, 'encoding': encoding,
            'stored_sha256': stored_hash.hexdigest(), 'stored_bytes': stored_size}}
        try:
            # Atomic create-if-absent. Never replace an existing immutable object.
            os.link(temporary, destination)
            _sync_directory(destination.parent)
        except FileExistsError:
            saved_hash, saved_size = _stored_identity(destination)
            reference['_evidence_ref'].update(stored_sha256=saved_hash, stored_bytes=saved_size)
        verify(root, reference)
        return reference
    except EvidenceObjectError:
        raise
    except (OSError, TypeError, ValueError, RecursionError, zlib.error):
        raise _error('EVIDENCE_OBJECT_WRITE_FAILED') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _json_parts(value, active):
    """Ledger-compatible canonical JSON without recursive whole-object copies."""
    if isinstance(value, float):
        raise _error('EVIDENCE_OBJECT_JSON_INVALID')
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _error('EVIDENCE_OBJECT_JSON_INVALID')
        value = format(value, 'f')
    elif isinstance(value, datetime):
        if value.utcoffset() is None:
            raise _error('EVIDENCE_OBJECT_JSON_INVALID')
        value = value.astimezone(timezone.utc).isoformat()
    elif isinstance(value, date):
        value = value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        identity = id(value)
        if identity in active:
            raise _error('EVIDENCE_OBJECT_JSON_INVALID')
        active.add(identity)
        try:
            yield from _json_parts({field.name: getattr(value, field.name)
                                    for field in fields(value)}, active)
        finally:
            active.remove(identity)
        return
    if isinstance(value, (Mapping, list, tuple)):
        identity = id(value)
        if identity in active:
            raise _error('EVIDENCE_OBJECT_JSON_INVALID')
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                if any(not isinstance(key, str) for key in value):
                    raise _error('EVIDENCE_OBJECT_JSON_INVALID')
                yield '{'
                for index, key in enumerate(sorted(value)):
                    if index:
                        yield ','
                    yield json.dumps(key, ensure_ascii=False)
                    yield ':'
                    yield from _json_parts(value[key], active)
                yield '}'
            else:
                yield '['
                for index, item in enumerate(value):
                    if index:
                        yield ','
                    yield from _json_parts(item, active)
                yield ']'
        finally:
            active.remove(identity)
    elif value is None or isinstance(value, (str, int, bool)):
        yield json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    else:
        raise _error('EVIDENCE_OBJECT_JSON_INVALID')


def put_json(root, value, *, max_bytes=None):
    """Store canonical JSON once; identical subobjects get an identical reference."""
    def encoded():
        buffer = bytearray()
        for part in _json_parts(value, set()):
            chunk = part.encode('utf-8')
            if len(buffer) + len(chunk) > CHUNK_BYTES and buffer:
                yield bytes(buffer)
                buffer.clear()
            if len(chunk) >= CHUNK_BYTES:
                yield chunk
            else:
                buffer.extend(chunk)
        if buffer:
            yield bytes(buffer)
    return put_blob(root, encoded(), max_bytes=max_bytes)


def iter_bytes(root, reference, *, max_bytes=None):
    """Restore original bytes in bounded chunks; consume fully to verify both hashes."""
    _limit(max_bytes)
    metadata = _metadata(reference)
    if max_bytes is not None and metadata['bytes'] > max_bytes:
        raise _error('EVIDENCE_OBJECT_LIMIT')
    try:
        path = _path(root, metadata['path'])
        if not stat.S_ISREG(path.lstat().st_mode):
            raise _error('EVIDENCE_OBJECT_UNSAFE_PATH')
        if path.stat().st_size != metadata['stored_bytes']:
            raise _error('EVIDENCE_OBJECT_CORRUPT')
        decompressor = zlib.decompressobj()
        raw_hash, stored_hash = hashlib.sha256(), hashlib.sha256()
        raw_size = stored_size = 0
        with path.open('rb') as stream:
            for compressed in iter(lambda: stream.read(CHUNK_BYTES), b''):
                stored_hash.update(compressed)
                stored_size += len(compressed)
                pending = compressed
                while pending:
                    chunk = decompressor.decompress(pending, CHUNK_BYTES)
                    pending = decompressor.unconsumed_tail
                    raw_size += len(chunk)
                    if raw_size > metadata['bytes'] or decompressor.unused_data:
                        raise _error('EVIDENCE_OBJECT_CORRUPT')
                    raw_hash.update(chunk)
                    if chunk:
                        yield chunk
        if (not decompressor.eof or decompressor.unused_data or
                raw_size != metadata['bytes'] or stored_size != metadata['stored_bytes'] or
                raw_hash.hexdigest() != metadata['sha256'] or
                stored_hash.hexdigest() != metadata['stored_sha256']):
            raise _error('EVIDENCE_OBJECT_CORRUPT')
    except EvidenceObjectError:
        raise
    except (OSError, ValueError, zlib.error):
        raise _error('EVIDENCE_OBJECT_UNAVAILABLE') from None


def verify(root, reference, *, max_bytes=None):
    for _ in iter_bytes(root, reference, max_bytes=max_bytes):
        pass
    metadata = _metadata(reference)
    return {'state': 'READY', 'sha256': metadata['sha256'], 'bytes': metadata['bytes'],
            'stored_sha256': metadata['stored_sha256'], 'stored_bytes': metadata['stored_bytes']}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _error('EVIDENCE_OBJECT_JSON_INVALID')
        result[key] = value
    return result


def _invalid_number(value):
    raise _error('EVIDENCE_OBJECT_JSON_INVALID')


def read_json(root, reference, *, max_bytes=DEFAULT_READ_BYTES):
    """Explicit bounded expansion for evidence inspection, never an implicit row read."""
    try:
        if _metadata(reference)['encoding'] != 'json+zlib':
            raise _error('EVIDENCE_OBJECT_ENCODING_MISMATCH')
        raw = b''.join(iter_bytes(root, reference, max_bytes=max_bytes))
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_float=_invalid_number, parse_constant=_invalid_number)
    except EvidenceObjectError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise _error('EVIDENCE_OBJECT_JSON_INVALID') from None
