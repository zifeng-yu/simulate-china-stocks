"""Bounded, local runtime-only archives. Verification never extracts members."""

import fcntl
import gzip
import io
import json
import os
import re
import tarfile
import tempfile
import zlib
from contextlib import contextmanager
from datetime import date as calendar_date
from datetime import datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from .identity import (
    EVIDENCE,
    MAX_MANIFEST_BYTES,
    RuntimeEvidenceError,
    digest,
    encode,
    owned,
    read_bytes,
    save_immutable,
    validate_identity,
)

BASE = 'data/backups/runtime'
MAX_FILES = 10000
MAX_FILE_BYTES = 64_000_000
MAX_TOTAL_BYTES = 256_000_000
MAX_ARCHIVE_BYTES = 256_000_000
MAX_LINE_BYTES = 100_000
AUTO_SCAN_FILES = 4000
AUTO_TOTAL_BYTES = 32_000_000
AUTO_SECONDS = 5
_LOG = re.compile(r'([0-9a-f]{32})\.jsonl')
_OBJECT = re.compile(re.escape(EVIDENCE) + r'/objects/([0-9a-f]{64})\.evidence')
_IDENTITY = re.compile(re.escape(EVIDENCE) + r'/identities/([0-9a-f]{64})\.json')
_EVENTS = {'START', 'END', 'STAGE_START', 'STAGE_END', 'STAGE_ERROR', 'EVENT'}
_SHANGHAI = ZoneInfo('Asia/Shanghai')


class _Budget:
    def __init__(self, *, automatic=False):
        self.remaining = AUTO_TOTAL_BYTES if automatic else MAX_TOTAL_BYTES
        self.files = AUTO_SCAN_FILES if automatic else MAX_FILES
        self.deadline = monotonic() + (AUTO_SECONDS if automatic else 60)

    def check(self, *, size=0, files=0):
        self.remaining -= size
        self.files -= files
        if self.remaining < 0 or self.files < 0 or monotonic() > self.deadline:
            raise RuntimeEvidenceError('ARCHIVE_BUDGET_EXCEEDED')


def _now():
    return datetime.now(_SHANGHAI)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise RuntimeEvidenceError('INVALID_DATE')
    try:
        calendar_date.fromisoformat(value)
    except ValueError:
        raise RuntimeEvidenceError('INVALID_DATE') from None
    if value > _now().date().isoformat():
        raise RuntimeEvidenceError('FUTURE_DATE')
    return value


@contextmanager
def _lock(root):
    path = owned(root, BASE + '/archive.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    path = owned(root, BASE + '/archive.lock')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeEvidenceError('ARCHIVE_BUSY') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _log_info(name, raw):
    """Inspect recorded boundaries; neither infer an END nor repair malformed bytes."""
    identity, seen_start, seen_end, issues = None, False, False, set()
    if not raw or not raw.endswith(b'\n'):
        issues.add('TRUNCATED_OR_EMPTY_LOG')
    invocation = Path(name).stem
    for line in raw.splitlines():
        if len(line) > MAX_LINE_BYTES:
            issues.add('EVENT_SIZE_LIMIT')
            continue
        try:
            event = json.loads(line)
            if (not isinstance(event, dict) or type(event.get('schema_version')) is not int
                    or event.get('schema_version') != 1
                    or event.get('invocation_id') != invocation or event.get('event') not in _EVENTS):
                raise ValueError
            if event['event'] == 'START':
                if seen_start or seen_end:
                    raise ValueError
                seen_start, identity = True, event.get('identity')
            elif not seen_start or seen_end:
                raise ValueError
            if event['event'] == 'END':
                seen_end = True
        except (ValueError, TypeError, UnicodeError):
            issues.add('MALFORMED_EVENT')
    if not seen_start:
        issues.add('MISSING_START')
    if not seen_end:
        issues.add('MISSING_END')
    if not isinstance(identity, dict) or identity.get('state') != 'READY':
        issues.add('IDENTITY_UNAVAILABLE')
        identity = None
    elif (not isinstance(identity.get('identity_id'), str)
            or identity.get('manifest_path') != f"{EVIDENCE}/identities/{identity['identity_id']}.json"
            or not _IDENTITY.fullmatch(identity.get('manifest_path', ''))):
        issues.add('IDENTITY_REFERENCE_INVALID')
        identity = None
    boundary = {'path': name, 'invocation_id': invocation, 'has_start': seen_start,
                'has_end': seen_end, 'issues': sorted(issues)}
    return boundary, identity


def _snapshot(root, day, budget):
    directory = owned(root, 'logs/runtime/' + day)
    if not directory.is_dir():
        raise RuntimeEvidenceError('NO_RUNTIME_LOGS')
    files, boundaries, identities, issues = {}, [], {}, []
    paths = []
    for path in directory.iterdir():
        budget.check(files=1)
        paths.append(path)
    for path in sorted(paths):
        name = 'logs/runtime/' + day + '/' + path.name
        path = owned(root, name)
        if not _LOG.fullmatch(path.name) or not path.is_file():
            raise RuntimeEvidenceError('UNEXPECTED_LOG_PATH')
        size = path.stat().st_size
        budget.check(size=size)
        raw = read_bytes(path, MAX_FILE_BYTES, prefix=True)
        files[name] = raw
        boundary, identity = _log_info(name, raw)
        boundaries.append(boundary)
        if identity:
            identities[identity['identity_id']] = identity['manifest_path']
    if not files:
        raise RuntimeEvidenceError('NO_RUNTIME_LOGS')
    for identity, name in sorted(identities.items()):
        try:
            budget.check(files=1)
            raw = read_bytes(owned(root, name), MAX_MANIFEST_BYTES)
            budget.check(size=len(raw))
            manifest = validate_identity(raw, identity)
            files[name] = raw
            for row in manifest['source_files'] + manifest['configuration_files']:
                ref = row['object_path']
                if ref in files:
                    continue
                budget.check(files=1, size=row['size'])
                body = read_bytes(owned(root, ref), row['size'])
                if len(body) != row['size'] or digest(body) != row['sha256']:
                    raise RuntimeEvidenceError('OBJECT_INVALID')
                files[ref] = body
        except RuntimeEvidenceError as exc:
            if str(exc) in {'ARCHIVE_BUDGET_EXCEEDED', 'UNSAFE_PATH'}:
                raise
            issues.append({'identity_id': identity, 'reason': 'IDENTITY_CLOSURE_UNAVAILABLE'})
    metadata = {'schema_version': 1, 'scope': 'RUNTIME_ONLY_SAME_DISK_NOT_DISASTER_RECOVERY',
                'date': day, 'files': [{'path': name, 'size': len(raw), 'sha256': digest(raw)}
                    for name, raw in sorted(files.items())],
                'boundaries': boundaries, 'issues': issues}
    metadata['state'] = ('PARTIAL' if issues or any(b['issues'] for b in boundaries) else 'COMPLETE')
    return metadata, files


def _fingerprint(metadata):
    return digest(encode({k: v for k, v in metadata.items() if k not in {'captured_at', 'fingerprint'}}))


def _paths(day, fingerprint):
    stem = f'{BASE}/{day}/{fingerprint}'
    return stem + '.tar.gz', stem + '.manifest.json'


def _allowed(name, day):
    return (bool(_OBJECT.fullmatch(name) or _IDENTITY.fullmatch(name))
            or name.startswith('logs/runtime/' + day + '/')
            and bool(_LOG.fullmatch(name.removeprefix('logs/runtime/' + day + '/'))))


def _check_payload(manifest, files):
    """Reconstruct closure and boundary facts solely from the archive's own bytes."""
    try:
        day = manifest['date']
        if (manifest['schema_version'] != 1
                or manifest['scope'] != 'RUNTIME_ONLY_SAME_DISK_NOT_DISASTER_RECOVERY'
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
                or _fingerprint(manifest) != manifest['fingerprint']):
            raise ValueError
        calendar_date.fromisoformat(day)
        captured = datetime.fromisoformat(manifest['captured_at'])
        if captured.tzinfo is None:
            raise ValueError
        rows = manifest['files']
        if not isinstance(rows, list) or len(rows) != len(files) or not rows:
            raise ValueError
        names = [r['path'] for r in rows]
        if len(set(names)) != len(names) or set(names) != set(files):
            raise ValueError
        for row in rows:
            raw = files[row['path']]
            if (not _allowed(row['path'], day) or type(row['size']) is not int
                    or row['size'] != len(raw) or digest(raw) != row['sha256']):
                raise ValueError
        boundaries, identities, issues, required = [], {}, [], set()
        for name, raw in sorted(files.items()):
            if name.startswith('logs/runtime/'):
                boundary, identity = _log_info(name, raw)
                boundaries.append(boundary)
                required.add(name)
                if identity:
                    identities[identity['identity_id']] = identity['manifest_path']
        if not boundaries:
            raise ValueError
        for identity, name in sorted(identities.items()):
            required.add(name)
            try:
                body = validate_identity(files[name], identity)
                for row in body['source_files'] + body['configuration_files']:
                    required.add(row['object_path'])
                    raw = files[row['object_path']]
                    if len(raw) != row['size'] or digest(raw) != row['sha256']:
                        raise ValueError
            except (KeyError, ValueError):
                issues.append({'identity_id': identity, 'reason': 'IDENTITY_CLOSURE_UNAVAILABLE'})
        expected_state = 'PARTIAL' if issues or any(b['issues'] for b in boundaries) else 'COMPLETE'
        if (manifest['boundaries'] != boundaries or manifest['issues'] != issues
                or manifest['state'] != expected_state or set(files) - required):
            raise ValueError
        return manifest
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR') from None


def _read_archive(root, relative, *, include_logs=False):
    path = owned(root, relative)
    budget = _Budget()
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeEvidenceError('SIZE_LIMIT')
    class LimitedReader:
        def __init__(self, stream):
            self.stream = stream
            self.remaining = MAX_TOTAL_BYTES + MAX_FILES * 1024 + 10240

        def read(self, size):
            budget.check()
            raw = self.stream.read(min(size, self.remaining + 1))
            self.remaining -= len(raw)
            if self.remaining < 0:
                raise RuntimeEvidenceError('ARCHIVE_BUDGET_EXCEEDED')
            return raw

    # Stream members, including trailing zero blocks, with a bound on inflated padding too.
    files, manifest = {}, None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source, gzip.GzipFile(fileobj=source) as inflated:
        with tarfile.open(fileobj=LimitedReader(inflated), mode='r|', ignore_zeros=True) as archive:
            for member in archive:
                budget.check(files=1, size=member.size)
                if (not member.isfile() or member.pax_headers or member.size < 0
                        or member.size > MAX_FILE_BYTES or member.name in files
                        or member.name == 'manifest.json' and member.size > MAX_MANIFEST_BYTES
                        or member.name == 'manifest.json' and manifest is not None):
                    raise RuntimeEvidenceError('UNSAFE_ARCHIVE_MEMBER')
                if member.name != 'manifest.json' and not (
                        _OBJECT.fullmatch(member.name) or _IDENTITY.fullmatch(member.name)
                        or re.fullmatch(r'logs/runtime/\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.jsonl', member.name)):
                    raise RuntimeEvidenceError('UNSAFE_ARCHIVE_MEMBER')
                stream = archive.extractfile(member)
                raw = stream.read(member.size + 1)
                if len(raw) != member.size:
                    raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
                if member.name == 'manifest.json':
                    manifest = json.loads(raw)
                else:
                    files[member.name] = raw
    if manifest is None:
        raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
    checked = _check_payload(manifest, files)
    if include_logs:
        return checked, {name: raw for name, raw in files.items()
                         if name.startswith('logs/runtime/')}
    return checked


def read_verified_archive(root, relative):
    """Read one verified archive and log members, without extraction or mutation."""
    match = re.fullmatch(re.escape(BASE) + r'/(\d{4}-\d{2}-\d{2})/([0-9a-f]{64})\.tar\.gz', relative)
    if match is None:
        raise RuntimeEvidenceError('UNSAFE_ARCHIVE_PATH')
    metadata, logs = _read_archive(root, relative, include_logs=True)
    if metadata['date'] != match[1] or metadata['fingerprint'] != match[2]:
        raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
    _, manifest_name = _paths(match[1], match[2])
    saved = json.loads(read_bytes(owned(root, manifest_name), MAX_MANIFEST_BYTES))
    if saved != _receipt(root, relative, metadata):
        raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
    return metadata, logs


def _result(metadata, archive_path, manifest_path, *, changed):
    return {'state': metadata['state'], 'date': metadata['date'],
            'fingerprint': metadata['fingerprint'], 'archive_path': archive_path,
            'manifest_path': manifest_path, 'captured_at': metadata['captured_at'],
            'file_count': len(metadata['files']), 'log_count': len(metadata['boundaries']),
            'unclosed_count': sum(not b['has_end'] for b in metadata['boundaries']),
            'issue_count': len(metadata['issues']) + sum(len(b['issues']) for b in metadata['boundaries']),
            'changed': changed, 'scope': metadata['scope']}


def _receipt(root, relative, metadata):
    path = owned(root, relative)
    raw = read_bytes(path, MAX_ARCHIVE_BYTES)
    return {'schema_version': 1, 'archive_path': relative, 'archive_sha256': digest(raw),
            'archive_size': len(raw), 'manifest': metadata}


def _archive_locked(root, day, budget, snapshot=None):
    metadata, files = snapshot or _snapshot(root, day, budget)
    fingerprint = _fingerprint(metadata)
    archive_name, manifest_name = _paths(day, fingerprint)
    archive_path, manifest_path = owned(root, archive_name), owned(root, manifest_name)
    if archive_path.exists():
        existing = _read_archive(root, archive_name)
        if existing['fingerprint'] != fingerprint:
            raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
        expected = _receipt(root, archive_name, existing)
        if manifest_path.exists():
            if json.loads(read_bytes(manifest_path, MAX_MANIFEST_BYTES)) != expected:
                raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
        else:
            # Crash after tar commit: recreate only the missing receipt from verified archived bytes.
            save_immutable(root, manifest_name, encode(expected))
        return _result(existing, archive_name, manifest_name, changed=False)
    if manifest_path.exists():
        raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
    metadata.update(fingerprint=fingerprint, captured_at=_now().isoformat())
    if len(encode(metadata)) > MAX_MANIFEST_BYTES - 1000:
        raise RuntimeEvidenceError('SIZE_LIMIT')
    _check_payload(metadata, files)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.archive-', dir=archive_path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                    for name, raw in [('manifest.json', encode(metadata)), *sorted(files.items())]:
                        budget.check()
                        member = tarfile.TarInfo(name)
                        member.size, member.mode, member.mtime = len(raw), 0o600, 0
                        archive.addfile(member, io.BytesIO(raw))
            output.flush()
            os.fsync(output.fileno())
        if Path(temporary).stat().st_size > MAX_ARCHIVE_BYTES:
            raise RuntimeEvidenceError('SIZE_LIMIT')
        owned(root, archive_name)
        os.link(temporary, archive_path)
    finally:
        os.unlink(temporary)
    save_immutable(root, manifest_name, encode(_receipt(root, archive_name, metadata)))
    return _result(metadata, archive_name, manifest_name, changed=True)


def _failure(exc):
    reason = str(exc) if isinstance(exc, RuntimeEvidenceError) else 'ARCHIVE_FAILED'
    state = ('BUSY' if reason == 'ARCHIVE_BUSY' else 'WAITING_MANUAL' if reason in {
        'ARCHIVE_BUDGET_EXCEEDED', 'SIZE_LIMIT', 'SCAN_LIMIT'} else 'FAILED')
    return {'state': state, 'reason': reason, 'changed': False}


def _auto_state(root):
    path = owned(root, BASE + '/auto-state.json')
    if not path.exists():
        return {'schema_version': 1, 'last_checked_date': None}
    try:
        body = json.loads(read_bytes(path, 100_000))
        if (not isinstance(body, dict) or type(body.get('schema_version')) is not int
                or body['schema_version'] != 1
                or set(body) != {'schema_version', 'last_checked_date'}):
            raise ValueError
        if body['last_checked_date'] is not None:
            _date(body['last_checked_date'])
        return body
    except (ValueError, TypeError):
        raise RuntimeEvidenceError('AUTO_STATE_INVALID') from None


def _save_auto_state(root, day):
    relative = BASE + '/auto-state.json'
    path = owned(root, relative)
    fd, temporary = tempfile.mkstemp(prefix='.auto-state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(encode({'schema_version': 1, 'last_checked_date': day}))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, owned(root, relative))
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_runtime(root, *, date):
    """Explicitly archive one date; incomplete source evidence remains visibly PARTIAL."""
    try:
        day = _date(date)
        with _lock(root):
            return _archive_locked(root, day, _Budget())
    except (OSError, ValueError, TypeError, tarfile.TarError, EOFError, zlib.error) as exc:
        return _failure(exc)


def verify_archive(root, path):
    """Verify archived bytes and the receipt only; never create locks or extract files."""
    try:
        given = Path(path)
        if given.is_absolute():
            given = given.relative_to(Path(root).resolve())
        relative = str(given)
        match = re.fullmatch(re.escape(BASE) + r'/(\d{4}-\d{2}-\d{2})/([0-9a-f]{64})\.tar\.gz', relative)
        if match is None:
            raise RuntimeEvidenceError('UNSAFE_ARCHIVE_PATH')
        metadata = _read_archive(root, relative)
        if metadata['date'] != match[1] or metadata['fingerprint'] != match[2]:
            raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
        _, manifest_name = _paths(match[1], match[2])
        saved = json.loads(read_bytes(owned(root, manifest_name), MAX_MANIFEST_BYTES))
        if saved != _receipt(root, relative, metadata):
            raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
        result = _result(metadata, relative, manifest_name, changed=False)
        result.update(state='READY' if metadata['state'] == 'COMPLETE' else 'PARTIAL', read_only=True)
        return result
    except (OSError, ValueError, TypeError, tarfile.TarError, EOFError, zlib.error):
        return {'state': 'INTEGRITY_ERROR', 'reason': 'ARCHIVE_VERIFICATION_FAILED', 'read_only': True}


def archive_due(root):
    """One previous changed date per successful homepage; no daily permanent skip."""
    try:
        directory = owned(root, 'logs/runtime')
        if not directory.is_dir():
            return {'state': 'SKIPPED', 'reason': 'NO_PREVIOUS_RUNTIME_LOGS', 'changed': False}
        budget = _Budget(automatic=True)
        with _lock(root):
            state = _auto_state(root)
            days = []
            for path in directory.iterdir():
                budget.check(files=1)
                owned(root, 'logs/runtime/' + path.name)
                if not path.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.name):
                    raise RuntimeEvidenceError('UNEXPECTED_LOG_PATH')
                if path.name < _now().date().isoformat():
                    days.append(_date(path.name))
            if not days:
                return {'state': 'SKIPPED', 'reason': 'NO_PREVIOUS_RUNTIME_LOGS', 'changed': False}
            days.sort()
            unarchived = [day for day in days if not owned(root, BASE + '/' + day).exists()]
            candidates = unarchived or days
            later = [day for day in candidates if day > (state['last_checked_date'] or '')]
            day = (later or candidates)[0]
            following = days[(days.index(day) + 1) % len(days)]
            snapshot = _snapshot(root, day, budget)
            archive_name, manifest_name = _paths(day, _fingerprint(snapshot[0]))
            if owned(root, archive_name).exists() and owned(root, manifest_name).exists():
                receipt = json.loads(read_bytes(owned(root, manifest_name), MAX_MANIFEST_BYTES))
                budget.check(size=len(encode(receipt)))
                if not isinstance(receipt, dict) or not isinstance(receipt.get('manifest'), dict):
                    raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
                saved = receipt['manifest']
                if (saved.get('fingerprint') != _fingerprint(snapshot[0])
                        or _fingerprint(saved) != _fingerprint(snapshot[0])
                        or receipt.get('archive_path') != archive_name
                        or owned(root, archive_name).stat().st_size != receipt.get('archive_size')):
                    raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
                budget.check(size=receipt['archive_size'])
                if digest(read_bytes(owned(root, archive_name), MAX_ARCHIVE_BYTES)) != receipt.get('archive_sha256'):
                    raise RuntimeEvidenceError('ARCHIVE_INTEGRITY_ERROR')
                result = {'state': 'SKIPPED', 'reason': 'UNCHANGED_PREVIOUS_RUNTIME_LOGS',
                          'date': day, 'changed': False}
            else:
                result = _archive_locked(root, day, budget, snapshot)
            _save_auto_state(root, day)
            return {**result, 'checked_date': day, 'next_check_date': following,
                    'candidate_date_count': len(days), 'unarchived_date_count': len(unarchived)}
    except (OSError, ValueError, TypeError, tarfile.TarError, EOFError, zlib.error) as exc:
        return _failure(exc)
