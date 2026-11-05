"""Incremental, fail-closed inventory of sealed references to runtime evidence.

Only derived protection metadata is written. This module never scans the ledger,
collects data, rotates active stderr, or deletes a file by itself.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from china_stock_sim.common import audit_archive

from .recording import owned, record_exception
from .retention import maintenance_due

BASE = 'data/state/runtime-retention'
CACHE = BASE + '/protection.json'
SCHEMA = 1
ROOTS = ('logs/*.json', 'docs/**', audit_archive.DIRECTORY + '/*.evidence')
_REFERENCE = re.compile(rb'(?:logs/runtime/\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.jsonl|'
                        rb'data/backups/runtime/\d{4}-\d{2}-\d{2}/[0-9a-f]{64}'
                        rb'(?:\.tar\.gz|\.manifest\.json))')
_MUTABLE_LOGS = {'logs/project-current-state.json'}


class ProtectionScanError(ValueError):
    """Fixed diagnostics may identify a project source without its content."""

    def __init__(self, reason, source):
        self.reason = reason
        self.source = source
        super().__init__(reason)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _stamp(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('PROTECTION_UNSAFE_SOURCE')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _sources(root, limit):
    result, count = [], 0

    def include(path):
        nonlocal count
        count += 1
        if count > limit:
            raise ValueError('PROTECTION_SCAN_LIMIT')
        relative = str(path.relative_to(root))
        owned(root, relative)
        if path.is_symlink():
            raise ValueError('PROTECTION_UNSAFE_SOURCE')
        if path.is_file():
            result.append(relative)

    logs = owned(root, 'logs')
    if logs.exists():
        for path in sorted(logs.iterdir()):
            if path.suffix == '.json' and str(path.relative_to(root)) not in _MUTABLE_LOGS:
                include(path)
    docs = owned(root, 'docs')
    if docs.exists():
        pending = [docs]
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                count += 1
                if count > limit:
                    raise ValueError('PROTECTION_SCAN_LIMIT')
                owned(root, str(path.relative_to(root)))
                if path.is_symlink():
                    raise ValueError('PROTECTION_UNSAFE_SOURCE')
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    result.append(str(path.relative_to(root)))
    evidence = owned(root, audit_archive.DIRECTORY)
    if evidence.exists():
        for path in sorted(evidence.iterdir()):
            if path.suffix == '.evidence':
                include(path)
    return sorted(result)


def _identity(root, relative, previous=None):
    path = owned(root, relative)
    identity = {'file': _stamp(path)}
    if previous and previous.get('file') == identity['file']:
        if previous.get('object'):
            obj = previous['object']
            return {**previous, 'object': {**obj, 'file': _stamp(owned(root, obj['path']))}}
        return previous
    if relative.startswith(audit_archive.DIRECTORY + '/'):
        layout = audit_archive.layout(root, relative)
        identity.update(logical_sha256=layout['sha256'], logical_bytes=layout['bytes'])
        if layout.get('object_ref'):
            obj = layout['object_ref']['_evidence_ref']
            identity['object'] = {'path': obj['path'], 'sha256': obj['stored_sha256'],
                                  'file': _stamp(owned(root, obj['path']))}
    return identity


def _scan(root, relative, identity, maximum, max_json_nodes=1_000_000):
    size = identity.get('logical_bytes', identity['file'][2])
    if size > maximum:
        raise ProtectionScanError('PROTECTION_SOURCE_TOO_LARGE', relative)
    path = owned(root, relative)
    if relative.startswith(audit_archive.DIRECTORY + '/'):
        chunks = audit_archive.iter_evidence(root, relative, max_bytes=maximum)
    else:
        def stream():
            with path.open('rb') as handle:
                yield from iter(lambda: handle.read(1024 * 1024), b'')
        chunks = stream()
    checksum, references, previous, read_bytes = hashlib.sha256(), set(), b'', 0
    json_parts = [] if relative.startswith('logs/') else None
    for chunk in chunks:
        read_bytes += len(chunk)
        if read_bytes > maximum:
            raise ProtectionScanError('PROTECTION_SOURCE_TOO_LARGE', relative)
        checksum.update(chunk)
        if json_parts is not None:
            if read_bytes > 64 * 1024 * 1024:
                raise ProtectionScanError('PROTECTION_JSON_TOO_LARGE', relative)
            json_parts.append(chunk)
        text = (previous + chunk).replace(b'\\/', b'/').replace(b'\\u002f', b'/')
        references.update(match.group().decode('ascii') for match in _REFERENCE.finditer(text))
        previous = text[-512:]
    nodes = 0
    if json_parts is not None and read_bytes:
        pending = [json.loads(b''.join(json_parts))]
        while pending:
            item = pending.pop()
            nodes += 1
            if nodes > max_json_nodes:
                raise ProtectionScanError('PROTECTION_JSON_TOO_COMPLEX', relative)
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str):
                references.update(m.group().decode('ascii') for m in _REFERENCE.finditer(item.encode()))
    if _identity(root, relative, identity) != identity:
        raise ProtectionScanError('PROTECTION_SOURCE_CHANGED', relative)
    source_type = ('EMPTY_ARTIFACT' if read_bytes == 0 else
                   'JSON' if json_parts is not None else 'TEXT_OR_ARCHIVED_BYTES')
    return {'identity': identity, 'sha256': checksum.hexdigest(), 'bytes': read_bytes,
            'source_type': source_type, 'json_nodes': nodes,
            'references': sorted(references)}


def _read_cache(root):
    path = owned(root, CACHE)
    if not path.exists():
        return {'schema_version': SCHEMA, 'roots': list(ROOTS), 'sources': {}, 'targets': {}}
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('PROTECTION_CACHE_TOO_LARGE')
    value = json.loads(path.read_bytes())
    body = value['body']
    if (value.get('sha256') != hashlib.sha256(_encoded(body)).hexdigest()
            or body.get('schema_version') != SCHEMA or body.get('roots') != list(ROOTS)
            or not isinstance(body.get('sources'), dict) or not isinstance(body.get('targets'), dict)):
        raise ValueError('PROTECTION_CACHE_INVALID')
    return body


def _save_cache(root, value):
    path = owned(root, CACHE)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _encoded({'body': value, 'sha256': hashlib.sha256(_encoded(value)).hexdigest()}) + b'\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.protection-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(root):
    path = owned(root, BASE + '/protection.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _save_progress(root, cache):
    # Per-file validation is reusable; a partial cache is not deletion authority.
    cache.pop('manifest_hash', None)
    _save_cache(root, cache)


def _inventory(root, *, max_scan_files, max_scan_bytes, max_json_nodes=1_000_000):
    cache = _read_cache(root)
    progress = {'changed': False}
    try:
        return _advance_inventory(root, cache, progress, max_scan_files=max_scan_files,
                                  max_scan_bytes=max_scan_bytes, max_json_nodes=max_json_nodes)
    except Exception:
        if progress['changed'] or 'manifest_hash' in cache:
            _save_progress(root, cache)
        raise


def _advance_inventory(root, cache, progress, *, max_scan_files, max_scan_bytes, max_json_nodes):
    names = _sources(root, max_scan_files)
    if set(cache['sources']) - set(names):
        raise ValueError('PROTECTION_PREVIOUS_SOURCE_MISSING')
    identities = {name: _identity(root, name, cache['sources'].get(name, {}).get('identity')) for name in names}
    used, reused, pending, changed = 0, 0, [], False
    for name in names:
        known = cache['sources'].get(name)
        if known and known.get('identity') == identities[name]:
            reused += 1
            continue
        size = identities[name].get('logical_bytes', identities[name]['file'][2])
        if used + size > max_scan_bytes:
            pending.append(name)
            continue
        cache['sources'][name] = _scan(root, name, identities[name], max_scan_bytes - used, max_json_nodes)
        used += size
        changed = True
        progress['changed'] = True
    base = {'source_count': len(names), 'sources_reused': reused, 'bytes_scanned': used,
            'scope': list(ROOTS), 'financial_database_scanned': False}
    if pending:
        if changed or 'manifest_hash' in cache:
            _save_progress(root, cache)
        return {**base, 'state': 'WAITING', 'reason': 'PROTECTION_SCAN_PENDING',
                'pending_sources': len(pending), 'reference_inventory_complete': False}
    references = sorted({ref for row in cache['sources'].values() for ref in row['references']})
    protected = []
    for name in references:
        path = owned(root, name)
        stamp = _stamp(path)
        target = cache['targets'].get(name)
        if not target or target.get('identity') != stamp:
            if used + stamp[2] > max_scan_bytes:
                if changed or 'manifest_hash' in cache:
                    _save_progress(root, cache)
                return {**base, 'state': 'WAITING', 'reason': 'PROTECTION_TARGET_SCAN_PENDING',
                        'reference_inventory_complete': False}
            checksum = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(chunk)
            if _stamp(path) != stamp:
                raise ValueError('PROTECTION_TARGET_CHANGED')
            target = {'identity': stamp, 'sha256': checksum.hexdigest()}
            cache['targets'][name] = target
            used += stamp[2]
            changed = True
            progress['changed'] = True
        protected.append({'path': name, 'sha256': target['sha256']})
    # No deletion authority is returned for a moving source inventory.
    if names != _sources(root, max_scan_files) or any(
            _identity(root, name, identity) != identity for name, identity in identities.items()):
        raise ValueError('PROTECTION_SOURCE_CHANGED')
    manifest_hash = hashlib.sha256(_encoded({'sources': identities, 'references': protected})).hexdigest()
    if changed or cache.get('manifest_hash') != manifest_hash:
        cache['manifest_hash'] = manifest_hash
        _save_cache(root, cache)
    return {**base, 'bytes_scanned': used, 'state': 'READY',
            'reference_inventory_complete': True, 'protected_references': protected,
            'manifest_path': CACHE, 'manifest_hash': manifest_hash}


def protection_inventory(root, *, max_scan_files=20_000, max_scan_bytes=512 * 1024 * 1024,
                         max_json_nodes=1_000_000):
    """Collect/cache references only. A limit, missing source or bad archive fails closed."""
    try:
        if (any(type(v) is not int or v < 1 for v in (max_scan_files, max_scan_bytes, max_json_nodes))
                or max_json_nodes > 10_000_000):
            raise ValueError('PROTECTION_LIMIT_INVALID')
        root = Path(root).resolve(strict=True)
        with _lock(root):
            return _inventory(root, max_scan_files=max_scan_files, max_scan_bytes=max_scan_bytes,
                              max_json_nodes=max_json_nodes)
    except BlockingIOError:
        return {'state': 'BUSY', 'reason': 'PROTECTION_LOCKED', 'reference_inventory_complete': False}
    except Exception as exc:
        record_exception(exc, component='runtime.protection')
        result = {'state': 'WAITING', 'reason': 'PROTECTION_INVENTORY_UNAVAILABLE',
                  'reference_inventory_complete': False}
        if isinstance(exc, ProtectionScanError):
            result.update(reason=exc.reason, source=exc.source)
        return result


def maintain_runtime(root, *, now=None):
    """Homepage hook: cache changed references, then at most one daily maintenance."""
    proof = protection_inventory(root)
    if proof.get('reference_inventory_complete') is not True:
        return proof
    result = maintenance_due(root, now=now, protected_references=proof['protected_references'],
                             reference_inventory_complete=True)
    return {**result, 'protection_manifest': proof['manifest_path'],
            'protection_hash': proof['manifest_hash'],
            'protected_reference_count': len(proof['protected_references'])}
