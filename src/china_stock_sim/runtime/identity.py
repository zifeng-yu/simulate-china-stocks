"""Content-addressed source bytes and public TOML semantics; never load credentials."""

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import tomllib
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath

EVIDENCE = 'data/normalized/runtime-evidence'
DECLARATIONS = ('pyproject.toml', 'uv.lock')
LAUNCHERS = ('deploy/launchd/bin/A股模拟-业务调度',
             'deploy/launchd/bin/A股模拟-首页刷新',
             'deploy/launchd/bin/A股模拟-云端上传')
MAX_FILES = 4096
MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 64_000_000
MAX_MANIFEST_BYTES = 2_000_000
_HEX = re.compile(r'[0-9a-f]{64}')
_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,100}')
_PUBLIC_TOKEN_BUDGETS = frozenset({'max_output_tokens', 'max_tokens'})
_PRIVATE_KEY = re.compile(
    r'password|passwd|secret|token|api.?key|authorization|credential|endpoint|'
    r'url|host|user(?:name)?$|directory|path|proxy|private|certificate|cookie|header', re.I)
_PRIVATE_TEXT = re.compile(
    r'(?:https?://|ssh://|file://|Bearer\s+|sk-[A-Za-z0-9_.-]{12,}|'
    r'-----BEGIN .*PRIVATE KEY|(?:^|\s)(?:/|~/|[A-Za-z]:\\)|'
    r'\b(?:api[_-]?key|password|passwd|secret|token|authorization)\s*[:=]\s*\S+|'
    r'\b(?:\d{1,3}\.){3}\d{1,3}\b)', re.I)
_SOURCE_PRIVATE = re.compile(
    r'sk-[A-Za-z0-9_.-]{12,}|\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=-]{12,}|'
    r'[a-z][a-z0-9+.-]*://[^\s/@]+@|'
    r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[A-Za-z0-9+/=\r\n ]+'
    r'-----END (?:[A-Z]+ )?PRIVATE KEY-----', re.I)


class RuntimeEvidenceError(ValueError):
    """Messages are fixed codes, never raw operating system or configuration errors."""


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def owned(root, relative):
    root = Path(root)
    if root.is_symlink():
        raise RuntimeEvidenceError('UNSAFE_PATH')
    root = root.resolve()
    if not isinstance(relative, str):
        raise RuntimeEvidenceError('UNSAFE_PATH')
    name = PurePosixPath(relative)
    if (name.is_absolute() or '..' in name.parts
            or '\\' in relative or str(name) != relative):
        raise RuntimeEvidenceError('UNSAFE_PATH')
    path = root.joinpath(*name.parts)
    if any(item.is_symlink() for item in (path, *path.parents) if item != root.parent):
        raise RuntimeEvidenceError('UNSAFE_PATH')
    return path


def read_bytes(path, limit=MAX_FILE_BYTES, *, prefix=False):
    """Read a bounded regular file. A log prefix is a valid point-in-time snapshot."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeEvidenceError('UNSAFE_FILE')
            if before.st_size > limit:
                raise RuntimeEvidenceError('SIZE_LIMIT')
            raw = stream.read(before.st_size + (0 if prefix else 1))
            after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or (not prefix and (
                before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns)):
            raise RuntimeEvidenceError('INPUT_CHANGED')
        return raw
    except RuntimeEvidenceError:
        raise
    except OSError:
        raise RuntimeEvidenceError('READ_FAILED') from None


def save_immutable(root, relative, raw):
    path = owned(root, relative)
    if path.exists():
        if read_bytes(path, max(len(raw), MAX_MANIFEST_BYTES)) != raw:
            raise RuntimeEvidenceError('EVIDENCE_CONFLICT')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path = owned(root, relative)
    fd, temporary = tempfile.mkstemp(prefix='.runtime-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_bytes(path, max(len(raw), MAX_MANIFEST_BYTES)) != raw:
                raise RuntimeEvidenceError('EVIDENCE_CONFLICT') from None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def _public(value, depth=0):
    if depth > 30:
        raise RuntimeEvidenceError('CONFIG_LIMIT')
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise RuntimeEvidenceError('UNSAFE_CONFIG_KEY')
            if key in _PUBLIC_TOKEN_BUDGETS:
                # These existing model configuration/protocol limits are public integers.
                # A string/boolean/container cannot use their name to bypass redaction.
                result[key] = child if type(child) is int and 1 <= child <= 8192 else '[REDACTED]'
            elif _PRIVATE_KEY.search(key):
                result[key] = '[REDACTED]'
            elif key.lower().endswith('_env'):
                result[key] = '[ENVIRONMENT_NOT_READ]'
            else:
                result[key] = _public(child, depth + 1)
        return result
    if isinstance(value, list):
        return [_public(item, depth + 1) for item in value]
    if isinstance(value, str):
        return '[REDACTED]' if _PRIVATE_TEXT.search(value) else value
    if value is None or type(value) in {int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise RuntimeEvidenceError('CONFIG_VALUE_INVALID')


def _files(root):
    """Traverse only project src, rejecting links; include every Python source file."""
    source = owned(root, 'src')
    if not source.is_dir():
        raise RuntimeEvidenceError('SOURCE_MISSING')
    found, visited, pending = [], 0, [source]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.name == '__pycache__':
                    continue
                visited += 1
                if visited > MAX_FILES:
                    raise RuntimeEvidenceError('SCAN_LIMIT')
                if entry.is_symlink():
                    raise RuntimeEvidenceError('UNSAFE_PATH')
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.name.endswith('.py'):
                    found.append(str(Path(entry.path).relative_to(Path(root).resolve())))
    if not found:
        raise RuntimeEvidenceError('SOURCE_MISSING')
    return sorted(found)


def _object(root, path, raw):
    checksum = digest(raw)
    reference = f'{EVIDENCE}/objects/{checksum}.evidence'
    save_immutable(root, reference, raw)
    return {'path': path, 'sha256': checksum, 'size': len(raw), 'object_path': reference}


def capture_identity(root):
    """Capture source, named launchers/modes and public TOML without credentials."""
    try:
        root = Path(root)
        sources = _files(root)
        declarations, missing = [], []
        for name in (*DECLARATIONS, *LAUNCHERS):
            path = owned(root, name)
            if path.is_file():
                declarations.append(name)
            elif name in LAUNCHERS and path.exists():
                raise RuntimeEvidenceError('UNSAFE_FILE')
            else:
                missing.append(name)
        if 'pyproject.toml' in missing:
            raise RuntimeEvidenceError('DECLARATION_MISSING')
        configurations = []
        directory = owned(root, 'configs')
        if directory.exists():
            for index, path in enumerate(directory.iterdir(), 1):
                if index > MAX_FILES:
                    raise RuntimeEvidenceError('SCAN_LIMIT')
                if path.is_symlink():
                    raise RuntimeEvidenceError('UNSAFE_PATH')
                if path.suffix == '.toml':
                    configurations.append(str(path.relative_to(root.resolve())))
            configurations.sort()
        if len(sources) + len(declarations) + len(configurations) > MAX_FILES:
            raise RuntimeEvidenceError('SCAN_LIMIT')
        total, source_rows, config_rows = 0, [], []
        stamps = []
        for name in [*sources, *declarations, *configurations]:
            path = owned(root, name)
            before = path.stat()
            raw = read_bytes(path, MAX_FILE_BYTES)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise RuntimeEvidenceError('SIZE_LIMIT')
            stamps.append((name, (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode)))
            if name in configurations:
                public = _public(tomllib.loads(raw.decode('utf-8')))
                config_rows.append(_object(root, name, encode(public)))
            else:
                if _SOURCE_PRIVATE.search(raw.decode('utf-8')):
                    raise RuntimeEvidenceError('SOURCE_CONTAINS_CREDENTIAL')
                row = _object(root, name, raw)
                if name in LAUNCHERS:
                    row['mode'] = stat.S_IMODE(before.st_mode)
                source_rows.append(row)
        for name, expected in stamps:
            info = owned(root, name).stat()
            if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode) != expected:
                raise RuntimeEvidenceError('INPUT_CHANGED')
        body = {'schema_version': 1, 'scope': 'RUNTIME_AUDIT_ONLY',
                'source_files': source_rows, 'configuration_files': config_rows,
                'missing_declarations': missing, 'configuration_projection': 'public-toml-v1',
                'environment_values_read': False}
        raw = encode(body)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RuntimeEvidenceError('SIZE_LIMIT')
        identity = digest(raw)
        relative = f'{EVIDENCE}/identities/{identity}.json'
        save_immutable(root, relative, raw)
        return {'state': 'READY', 'identity_id': identity, 'manifest_path': relative,
                'source_file_count': len(source_rows), 'configuration_file_count': len(config_rows)}
    except RuntimeEvidenceError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise RuntimeEvidenceError('IDENTITY_CAPTURE_FAILED') from None


def validate_identity(raw, identity_id):
    """Validate references without reading any referred file; also used inside archives."""
    try:
        if (not isinstance(identity_id, str) or not _HEX.fullmatch(identity_id)
                or len(raw) > MAX_MANIFEST_BYTES or digest(raw) != identity_id):
            raise RuntimeEvidenceError('IDENTITY_INVALID')
        body = json.loads(raw)
        if (type(body['schema_version']) is not int or body['schema_version'] != 1
                or body['scope'] != 'RUNTIME_AUDIT_ONLY'
                or body['configuration_projection'] != 'public-toml-v1'
                or body['environment_values_read'] is not False):
            raise RuntimeEvidenceError('IDENTITY_INVALID')
        sources, configs = body['source_files'], body['configuration_files']
        if not isinstance(sources, list) or not sources or not isinstance(configs, list):
            raise RuntimeEvidenceError('IDENTITY_INVALID')
        rows = sources + configs
        if len(rows) > MAX_FILES:
            raise RuntimeEvidenceError('SCAN_LIMIT')
        seen = set()
        for row in rows:
            name, checksum = row['path'], row['sha256']
            normalized = PurePosixPath(name)
            if (not isinstance(name, str) or normalized.is_absolute() or '..' in normalized.parts
                    or '\\' in name or str(normalized) != name or name in seen
                    or not isinstance(checksum, str) or not _HEX.fullmatch(checksum)
                    or row['object_path'] != f'{EVIDENCE}/objects/{checksum}.evidence'
                    or type(row['size']) is not int or not 0 <= row['size'] <= MAX_FILE_BYTES):
                raise RuntimeEvidenceError('IDENTITY_INVALID')
            if row in sources:
                if name in LAUNCHERS:
                    if type(row.get('mode')) is not int or not 0 <= row['mode'] <= 0o7777:
                        raise RuntimeEvidenceError('IDENTITY_INVALID')
                elif name not in DECLARATIONS and not (name.startswith('src/') and name.endswith('.py')):
                    raise RuntimeEvidenceError('IDENTITY_INVALID')
            elif not (normalized.parent == PurePosixPath('configs') and name.endswith('.toml')):
                raise RuntimeEvidenceError('IDENTITY_INVALID')
            seen.add(name)
        return body
    except RuntimeEvidenceError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise RuntimeEvidenceError('IDENTITY_INVALID') from None
