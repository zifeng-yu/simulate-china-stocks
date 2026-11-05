"""Explicit, fail-closed runtime retention; business evidence is never a deletion target."""

import fcntl
import gzip
import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.scheduler.process import probe_process

from .archive import read_verified_archive
from .identity import encode, save_immutable
from .recording import _day, _instant, owned

SHANGHAI = ZoneInfo('Asia/Shanghai')
BASE = 'data/state/runtime-retention'
ROTATED = 'logs/runtime-rotated'
DEFAULTS = {'raw_days': 30, 'archive_days': 180, 'stdout_max_bytes': 10 * 1024 * 1024,
            'stdout_keep_files': 5, 'max_scan_files': 100_000,
            'max_scan_bytes': 512 * 1024 * 1024}
STDIO = {'workflow': ('scheduler-launchd.stdout.log', 'scheduler-launchd.stderr.log'),
         'homepage': ('dashboard-refresh.stdout.log', 'dashboard-refresh.stderr.log'),
         'upload': ('publishing-launchd.stdout.log', 'publishing-launchd.stderr.log')}
SUCCESS = {'COMPLETE', 'READY', 'SKIPPED', 'UNCHANGED', 'DISABLED', 'BUSY',
           'WAITING_TIME', 'ALREADY_RUNNING', 'RETRY_WAIT'}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_LOG = re.compile(r'logs/runtime/\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.jsonl\Z')
_ARCHIVE = re.compile(r'data/backups/runtime/\d{4}-\d{2}-\d{2}/[0-9a-f]{64}\.tar\.gz\Z')


def _now(value=None):
    value = value or datetime.now(SHANGHAI)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('RETENTION_TIME_INVALID')
    return value.astimezone(SHANGHAI)


def load_retention_settings(root):
    path = owned(root, 'configs/runtime_retention.toml')
    supplied = tomllib.loads(path.read_text()) if path.exists() else {}
    if set(supplied) - set(DEFAULTS):
        raise ValueError('RETENTION_CONFIG_UNKNOWN')
    result = {**DEFAULTS, **supplied}
    if any(type(v) is not int or v <= 0 for v in result.values()):
        raise ValueError('RETENTION_CONFIG_INVALID')
    if (not 1 <= result['raw_days'] <= result['archive_days'] <= 36500
            or not 1024 <= result['stdout_max_bytes'] <= 1024 ** 3
            or not 1 <= result['stdout_keep_files'] <= 100
            or result['max_scan_files'] > 100_000
            or result['max_scan_bytes'] > 4 * 1024 ** 3):
        raise ValueError('RETENTION_CONFIG_INVALID')
    return result


class _Budget:
    def __init__(self, settings):
        self.files, self.bytes = settings['max_scan_files'], settings['max_scan_bytes']

    def consume(self, size=0):
        self.files -= 1
        self.bytes -= size
        if self.files < 0 or self.bytes < 0:
            raise ValueError('RETENTION_SCAN_LIMIT')


def _file(root, relative, budget=None):
    path = owned(root, relative)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('RETENTION_UNSAFE_FILE')
        if budget:
            budget.consume(before.st_size)
        hasher = hashlib.sha256()
        while raw := stream.read(1024 * 1024):
            hasher.update(raw)
        after = os.fstat(stream.fileno())
    def stamps(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if stamps(before) != stamps(after) or stamps(after) != stamps(path.stat()):
        raise ValueError('RETENTION_FILE_CHANGED')
    return {'path': relative, 'sha256': hasher.hexdigest(), 'size': before.st_size,
            'inode': before.st_ino, 'mtime_ns': before.st_mtime_ns}


def _json(root, relative, limit=8 * 1024 * 1024):
    path = owned(root, relative)
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limit:
        raise ValueError('RETENTION_RECORD_TOO_LARGE')
    return json.loads(path.read_bytes())


def _paths(root, base, suffix, budget):
    directory = owned(root, base)
    if not directory.exists():
        return []
    found = []
    for folder in sorted(directory.iterdir()):
        budget.consume()
        owned(root, str(folder.relative_to(root)))
        if not folder.is_dir() or not _day(folder.name):
            continue
        for path in sorted(folder.iterdir()):
            budget.consume()
            relative = str(path.relative_to(root))
            owned(root, relative)
            if path.is_file() and path.name.endswith(suffix):
                found.append(relative)
    return found


def _declarations(root, references, resolutions, budget):
    protected, resolved, evidence = {}, set(), []
    for item in references:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not _HASH.fullmatch(str(item.get('sha256', '')))):
            raise ValueError('RETENTION_REFERENCE_INVALID')
        owned(root, item['path'])
        protected[item['path']] = item['sha256']
    for item in resolutions:
        if (not isinstance(item, dict) or not re.fullmatch(r'[0-9a-f]{32}',
                str(item.get('invocation_id', '')))
                or not _HASH.fullmatch(str(item.get('sha256', '')))):
            raise ValueError('RETENTION_RESOLUTION_INVALID')
        row = _file(root, item['path'], budget)
        proof = _json(root, item['path'])
        if (row['sha256'] != item['sha256'] or proof.get('state') != 'RESOLVED'
                or proof.get('invocation_id') != item['invocation_id']):
            raise ValueError('RETENTION_RESOLUTION_INVALID')
        evidence.append(row)
        protected[item['path']] = item['sha256']
        resolved.add(item['invocation_id'])
    return protected, resolved, evidence


def _log_status(raw, resolved, probe):
    """Unclosed or uncertain logs always remain protected; a later success is not resolution."""
    if len(raw) > 64 * 1024 * 1024 or not raw.endswith(b'\n'):
        return 'CORRUPT_OR_OVERSIZED', None
    try:
        rows = [json.loads(line) for line in raw.splitlines()]
        if not rows or rows[0].get('event') != 'START' or rows[-1].get('event') != 'END':
            if rows:
                start = rows[0]
                if type(start.get('pid')) is int and start.get('process_birth'):
                    current = probe(start['pid'])
                    if current.get('state') == 'ALIVE' and current.get('birth') == start['process_birth']:
                        return 'ACTIVE_PROCESS', None
            return 'UNCLOSED_OR_UNKNOWN_PROCESS', None
        identity = rows[0]['invocation_id']
        if (any(r.get('invocation_id') != identity or r.get('sequence') != n
                or not _instant(r.get('recorded_at')) for n, r in enumerate(rows, 1))
                or sum(r.get('event') == 'START' for r in rows) != 1
                or sum(r.get('event') == 'END' for r in rows) != 1):
            return 'CORRUPT_LOG', None
        fault = (rows[-1].get('result_state') not in SUCCESS
                 or rows[-1].get('exit_code') not in (0, None)
                 or any(r.get('event') == 'STAGE_ERROR' or r.get('exception') for r in rows))
        if fault and identity not in resolved:
            return 'UNRESOLVED_FAILURE', None
        return None, max(_now(r['recorded_at']) for r in rows)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return 'CORRUPT_LOG', None


def _rotated(root, now, settings, protected, budget, body):
    directory = owned(root, ROTATED)
    if not directory.exists():
        return
    allowed = {name for names in STDIO.values() for name in names}
    for folder in sorted(directory.iterdir()):
        budget.consume()
        owned(root, str(folder.relative_to(root)))
        if folder.name not in allowed or not folder.is_dir():
            raise ValueError('ROTATED_PATH_INVALID')
        for path in sorted(folder.iterdir()):
            budget.consume()
            relative = str(path.relative_to(root))
            owned(root, relative)
            if not re.fullmatch(r'[0-9a-f]{64}\.log\.gz\.json', path.name):
                continue
            receipt = _json(root, relative)
            archive = receipt['archive']
            if (archive['path'] != relative.removesuffix('.json')
                    or receipt['path'] != 'logs/' + folder.name
                    or receipt['sha256'] != path.name[:64]):
                raise ValueError('ROTATED_RECEIPT_INVALID')
            original = archive['path'].removesuffix('.gz')
            observed = _file(root, archive['path'], budget)
            if observed != archive:
                raise ValueError('ROTATED_ARCHIVE_CHANGED')
            size, hasher = 0, hashlib.sha256()
            with gzip.open(owned(root, archive['path']), 'rb') as stream:
                while raw := stream.read(1024 * 1024):
                    size += len(raw)
                    if size > receipt['size']:
                        raise ValueError('ROTATED_ARCHIVE_SIZE_INVALID')
                    hasher.update(raw)
            if size != receipt['size'] or hasher.hexdigest() != receipt['sha256']:
                raise ValueError('ROTATED_ARCHIVE_INVALID')
            receipt_row = _file(root, relative, budget)
            body['evidence'].extend([observed, receipt_row])
            reasons = []
            if (any(name in protected for name in (relative, archive['path'], original))
                    or protected.get(receipt['path']) == receipt['sha256']):
                reasons.append('REFERENCED_EVIDENCE')
            # Unstructured stderr cannot prove resolution; preserve for explicit diagnostic review.
            if '.stderr.' in folder.name and receipt['size']:
                reasons.append('UNASSESSED_STDERR')
            if reasons:
                body['protected'].append({'path': archive['path'], 'reasons': reasons})
                continue
            age = (now.date() - _now(receipt['rotated_at']).date()).days
            if owned(root, original).exists():
                raw_row = _file(root, original, budget)
                if raw_row['sha256'] != receipt['sha256'] or raw_row['size'] != receipt['size']:
                    raise ValueError('ROTATED_SOURCE_CHANGED')
                body['evidence'].append(raw_row)
                if age >= settings['raw_days']:
                    body['candidates'].append({**raw_row, 'kind': 'STDIO_RAW',
                        'reason': 'RAW_RETENTION_EXPIRED', 'retained_archives': [archive['path']]})
            elif age >= settings['archive_days']:
                body['candidates'].extend([{**observed, 'kind': 'STDIO_ARCHIVE',
                    'reason': 'ARCHIVE_RETENTION_EXPIRED'}, {**receipt_row, 'kind': 'STDIO_MANIFEST',
                    'reason': 'ARCHIVE_RETENTION_EXPIRED'}])


def _plan(root, now, references, complete, resolutions, probe):
    settings = load_retention_settings(root)
    body = {'schema_version': 1, 'state': 'READY', 'as_of': now.date().isoformat(),
            'configuration': settings, 'scope': 'RUNTIME_LOGS_ONLY',
            'candidates': [], 'protected': [], 'evidence': [], 'bytes_reclaimable': 0}
    if complete is not True:
        return {**body, 'state': 'WAITING', 'reason': 'PROTECTION_INVENTORY_REQUIRED', 'plan_id': None}
    budget = _Budget(settings)
    protected, resolved, evidence = _declarations(root, references, resolutions, budget)
    body['evidence'].extend(evidence)
    body['protection_declaration'] = {'references': sorted(protected.items()),
                                    'resolved_invocations': sorted(resolved), 'complete': True}
    archive_members = {}
    archives = _paths(root, 'data/backups/runtime', '.tar.gz', budget)
    for relative in archives:
        if not _ARCHIVE.fullmatch(relative):
            raise ValueError('RETENTION_ARCHIVE_PATH_INVALID')
        row = _file(root, relative, budget)
        manifest_path = relative.removesuffix('.tar.gz') + '.manifest.json'
        manifest_row = _file(root, manifest_path, budget)
        metadata, logs = read_verified_archive(root, relative)
        body['evidence'].extend([row, manifest_row])
        reasons = []
        if metadata['state'] != 'COMPLETE':
            reasons.append('ARCHIVE_NOT_COMPLETE')
        if relative in protected or manifest_path in protected:
            reasons.append('REFERENCED_EVIDENCE')
        members = {item['path']: item for item in metadata['files']}
        for name, raw in logs.items():
            fault, _ = _log_status(raw, resolved, probe)
            if fault:
                reasons.append(fault)
            if name in protected:
                reasons.append('REFERENCED_EVIDENCE')
            if metadata['state'] == 'COMPLETE':
                archive_members.setdefault((name, members[name]['sha256']), []).append(relative)
        age = (now.date() - _now(metadata['captured_at']).date()).days
        # The source is removed in an earlier maintenance generation, never alongside its last archive.
        source_present = any(owned(root, name).exists() for name in logs)
        if age >= settings['archive_days'] and not reasons and not source_present:
            body['candidates'].extend([{**row, 'kind': 'ARCHIVE', 'reason': 'ARCHIVE_RETENTION_EXPIRED'},
                                      {**manifest_row, 'kind': 'ARCHIVE_MANIFEST',
                                       'reason': 'ARCHIVE_RETENTION_EXPIRED'}])
        elif reasons:
            body['protected'].append({'path': relative, 'reasons': sorted(set(reasons))})
    for relative in _paths(root, 'logs/runtime', '.jsonl', budget):
        if not _LOG.fullmatch(relative):
            raise ValueError('RETENTION_LOG_PATH_INVALID')
        row = _file(root, relative, budget)
        body['evidence'].append(row)
        fault, last = (('CORRUPT_OR_OVERSIZED', None) if row['size'] > 64 * 1024 * 1024 else
                       _log_status(owned(root, relative).read_bytes(), resolved, probe))
        reasons = ([fault] if fault else [])
        if relative in protected:
            reasons.append('REFERENCED_EVIDENCE')
        if last and last > now:
            reasons.append('FUTURE_LOG_TIME')
        if reasons:
            body['protected'].append({'path': relative, 'reasons': sorted(set(reasons))})
        elif (now.date() - last.date()).days >= settings['raw_days']:
            copies = archive_members.get((relative, row['sha256']), [])
            if copies:
                body['candidates'].append({**row, 'kind': 'RAW_LOG', 'reason': 'RAW_RETENTION_EXPIRED',
                                           'retained_archives': copies})
            else:
                body['protected'].append({'path': relative, 'reasons': ['VERIFIED_ARCHIVE_REQUIRED']})
    _rotated(root, now, settings, protected, budget, body)
    body['candidates'].sort(key=lambda item: item['path'])
    body['evidence'].sort(key=lambda item: item['path'])
    body['bytes_reclaimable'] = sum(item['size'] for item in body['candidates'])
    body['plan_id'] = hashlib.sha256(encode(body)).hexdigest()
    return body


def plan_retention(root, *, now=None, protected_references=(), reference_inventory_complete=False,
                   resolved_failures=(), process_probe=None):
    """Dry-run only: no locks, directories, configuration updates or deletion."""
    try:
        return _plan(Path(root).resolve(strict=True), _now(now), protected_references,
                     reference_inventory_complete, resolved_failures, process_probe or probe_process)
    except Exception:
        return {'state': 'WAITING', 'reason': 'RETENTION_INPUT_UNAVAILABLE_OR_UNSAFE',
                'plan_id': None, 'candidates': [], 'protected': [], 'bytes_reclaimable': 0}


@contextmanager
def _lock(root):
    path = owned(root, BASE + '/maintenance.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _atomic(root, relative, value):
    path = owned(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.retention-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply_locked(root, expected_plan, arguments, *, prepared_plan=None):
    receipt_path = BASE + '/receipts/' + expected_plan + '.json'
    if owned(root, receipt_path).exists():
        receipt = _json(root, receipt_path)
        if receipt.get('state') != 'COMPLETE' or receipt.get('plan_id') != expected_plan:
            raise ValueError('RETENTION_RECEIPT_INVALID')
        return {'state': 'UNCHANGED', 'plan_id': expected_plan, 'receipt': receipt_path,
                'deleted_count': len(receipt['deleted']), 'changed': False}
    progress_path = BASE + '/pending/' + expected_plan + '.json'
    progress = _json(root, progress_path) if owned(root, progress_path).exists() else None
    if progress is None:
        plan = prepared_plan or plan_retention(root, **arguments)
        if plan.get('state') != 'READY':
            return plan
        if plan['plan_id'] != expected_plan:
            return {'state': 'REJECTED', 'reason': 'STALE_RETENTION_PLAN', 'changed': False}
        progress = {'plan': plan, 'deleted': [], 'started_at': _now(arguments.get('now')).isoformat()}
        _atomic(root, progress_path, progress)
    plan = progress['plan']
    if (plan.get('plan_id') != expected_plan
            or hashlib.sha256(encode({k: v for k, v in plan.items() if k != 'plan_id'})).hexdigest() != expected_plan
            or plan.get('scope') != 'RUNTIME_LOGS_ONLY'):
        raise ValueError('RETENTION_PENDING_PLAN_INVALID')
    # Recheck all preserved dependencies and protection declarations after interruptions.
    declarations = {'references': sorted((r['path'], r['sha256']) for r in arguments['protected_references']),
                    'resolved_invocations': sorted(r['invocation_id'] for r in arguments['resolved_failures']),
                    'complete': arguments['reference_inventory_complete']}
    original = plan['protection_declaration']
    resolution_paths = {r['path'] for r in arguments['resolved_failures']}
    original_references = [tuple(x) for x in original['references'] if x[0] not in resolution_paths]
    if (declarations['complete'] is not True
            or declarations['references'] != original_references
            or declarations['resolved_invocations'] != original['resolved_invocations']
            or load_retention_settings(root) != plan['configuration']):
        return {'state': 'REJECTED', 'reason': 'PROTECTION_DECLARATION_CHANGED', 'changed': False}
    candidates = {row['path']: row for row in plan['candidates']}
    for row in plan['evidence']:
        if row['path'] in progress['deleted']:
            continue
        path = owned(root, row['path'])
        if not path.exists() and row['path'] in candidates:
            # An interrupted unlink is covered by the durable, hash-bound deletion intent.
            continue
        if _file(root, row['path']) != row:
            return {'state': 'REJECTED', 'reason': 'RETENTION_EVIDENCE_CHANGED', 'changed': False}
    for name, row in candidates.items():
        if name in progress['deleted']:
            continue
        path = owned(root, name)
        if path.exists():
            if _file(root, name) != {k: row[k] for k in ('path', 'sha256', 'size', 'inode', 'mtime_ns')}:
                return {'state': 'REJECTED', 'reason': 'RETENTION_FILE_CHANGED', 'changed': False}
            path.unlink()
        progress['deleted'].append(name)
        _atomic(root, progress_path, progress)
    receipt = {'schema_version': 1, 'state': 'COMPLETE', 'plan_id': expected_plan,
               'completed_at': _now(arguments.get('now')).isoformat(), 'plan': plan,
               'deleted': progress['deleted'], 'bytes_reclaimed': plan['bytes_reclaimable']}
    save_immutable(root, receipt_path, encode(receipt))
    owned(root, progress_path).unlink()
    return {'state': 'COMPLETE', 'plan_id': expected_plan, 'receipt': receipt_path,
            'deleted_count': len(progress['deleted']), 'bytes_reclaimed': plan['bytes_reclaimable'],
            'changed': bool(progress['deleted'])}


def apply_retention(root, *, expected_plan, now=None, protected_references=(),
                    reference_inventory_complete=False, resolved_failures=(), process_probe=None):
    if not isinstance(expected_plan, str) or not _HASH.fullmatch(expected_plan):
        return {'state': 'REJECTED', 'reason': 'EXPECTED_PLAN_REQUIRED', 'changed': False}
    arguments = {'now': now, 'protected_references': list(protected_references),
                 'reference_inventory_complete': reference_inventory_complete,
                 'resolved_failures': list(resolved_failures), 'process_probe': process_probe}
    try:
        with _lock(root):
            return _apply_locked(root, expected_plan, arguments)
    except BlockingIOError:
        return {'state': 'BUSY', 'reason': 'RETENTION_LOCKED', 'changed': False}
    except Exception:
        result = {'state': 'WAITING', 'reason': 'RETENTION_APPLY_INCOMPLETE',
                  'plan_id': expected_plan, 'changed': None, 'deleted_count': None,
                  'resume_same_plan': True}
        try:
            pending = owned(root, BASE + '/pending/' + expected_plan + '.json')
            receipt = owned(root, BASE + '/receipts/' + expected_plan + '.json')
            if pending.exists():
                progress = _json(root, str(pending.relative_to(Path(root))))
                deleted = [row['path'] for row in progress['plan']['candidates']
                           if not owned(root, row['path']).exists()]
                result.update(changed=bool(deleted), deleted_count=len(deleted))
            elif receipt.exists():
                saved = _json(root, str(receipt.relative_to(Path(root))))
                result.update(changed=bool(saved['deleted']), deleted_count=len(saved['deleted']))
            else:
                result.update(changed=False, deleted_count=0)
        except Exception:
            pass  # Unknown progress is explicit; never claim a partially applied cleanup was zero-write.
        return result


def maintenance_due(root, *, now=None, protected_references=(), reference_inventory_complete=False,
                    resolved_failures=()):
    """Optional homepage hook; one completed check per Shanghai day, never scan the ledger."""
    if reference_inventory_complete is not True:
        return {'state': 'WAITING', 'reason': 'PROTECTION_INVENTORY_REQUIRED', 'changed': False}
    today = _now(now).date().isoformat()
    state_path = BASE + '/daily.json'
    try:
        with _lock(root):
            if owned(root, state_path).exists() and _json(root, state_path).get('date') == today:
                return {'state': 'SKIPPED', 'reason': 'RETENTION_CHECKED_TODAY', 'changed': False}
            arguments = {'now': now, 'protected_references': list(protected_references),
                         'reference_inventory_complete': True, 'resolved_failures': list(resolved_failures),
                         'process_probe': None}
            plan = plan_retention(root, **arguments)
            if plan['state'] != 'READY':
                return plan
            result = (_apply_locked(root, plan['plan_id'], arguments, prepared_plan=plan) if plan['candidates'] else
                      {'state': 'UNCHANGED', 'reason': 'NO_EXPIRED_UNPROTECTED_LOGS', 'changed': False})
            if result['state'] in {'COMPLETE', 'UNCHANGED'}:
                _atomic(root, state_path, {'date': today, 'plan_id': plan['plan_id'], 'result': result})
            return result
    except BlockingIOError:
        return {'state': 'BUSY', 'reason': 'RETENTION_LOCKED', 'changed': False}
    except Exception:
        return {'state': 'WAITING', 'reason': 'RETENTION_MAINTENANCE_UNAVAILABLE', 'changed': False}


def _stdio_idle(root, tasks, settings):
    for name in _paths(root, 'logs/runtime', '.jsonl', _Budget(settings)):
        path = owned(root, name)
        with path.open('rb') as stream:
            first = stream.readline(16385)
            if len(first) > 16384:
                raise ValueError('STDIO_PROCESS_STATE_UNKNOWN')
            start = json.loads(first)
            if start.get('task') not in tasks:
                continue
            stream.seek(max(0, path.stat().st_size - 16384))
            lines = stream.read(16384).splitlines()
            try:
                end = json.loads(lines[-1])
            except (ValueError, IndexError):
                end = {}
            if end.get('event') == 'END' and end.get('invocation_id') == start.get('invocation_id'):
                continue
            if type(start.get('pid')) is not int or not start.get('process_birth'):
                raise ValueError('STDIO_PROCESS_STATE_UNKNOWN')
            process = probe_process(start['pid'])
            if process.get('state') == 'UNKNOWN' or (process.get('state') == 'ALIVE'
                    and process.get('birth') == start['process_birth']):
                raise ValueError('STDIO_PROCESS_NOT_STOPPED')


def rotate_stdio(root, *, stopped_tasks=(), now=None):
    """Explicit stopped-task maintenance; never truncate a process's open stdout descriptor."""
    if not stopped_tasks or any(task not in STDIO for task in stopped_tasks):
        return {'state': 'WAITING', 'reason': 'STOPPED_TASK_DECLARATION_REQUIRED', 'changed': False}
    try:
        root = Path(root).resolve(strict=True)
        settings, rotated = load_retention_settings(root), []
        with _lock(root):
            _stdio_idle(root, stopped_tasks, settings)
            for task in sorted(set(stopped_tasks)):
                for filename in STDIO[task]:
                    name = 'logs/' + filename
                    path = owned(root, name)
                    if not path.exists() or path.stat().st_size < settings['stdout_max_bytes']:
                        continue
                    before = _file(root, name)
                    target = f"{ROTATED}/{filename}/{before['sha256']}.log.gz"
                    destination = owned(root, target)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary = tempfile.mkstemp(prefix='.rotate-', dir=destination.parent)
                    try:
                        with os.fdopen(fd, 'wb') as output, path.open('rb') as source:
                            with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0) as compressed:
                                while raw := source.read(1024 * 1024):
                                    compressed.write(raw)
                            output.flush()
                            os.fsync(output.fileno())
                        if _file(root, name) != before:
                            raise ValueError('STDIO_CHANGED_DURING_ROTATION')
                        if destination.exists():
                            with gzip.open(destination, 'rb') as stream:
                                hasher = hashlib.sha256()
                                while raw := stream.read(1024 * 1024):
                                    hasher.update(raw)
                            if hasher.hexdigest() != before['sha256']:
                                raise ValueError('ROTATED_LOG_CONFLICT')
                        else:
                            os.link(temporary, destination)
                        manifest = {**before, 'schema_version': 1, 'task': task,
                                    'rotated_at': _now(now).isoformat(), 'archive': _file(root, target)}
                        receipt = target + '.json'
                        if not owned(root, receipt).exists():
                            save_immutable(root, receipt, encode(manifest))
                        # Rename to a content-addressed raw file before creating a fresh path.
                        preserved = owned(root, target.removesuffix('.gz'))
                        if preserved.exists() and _file(root, str(preserved.relative_to(Path(root))))['sha256'] != before['sha256']:
                            raise ValueError('ROTATED_LOG_CONFLICT')
                        os.replace(path, preserved)
                        path.touch(mode=0o600)
                        rotated.append({'path': name, 'archive': target, 'sha256': before['sha256']})
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
        return {'state': 'COMPLETE' if rotated else 'UNCHANGED', 'changed': bool(rotated),
                'rotated': rotated, 'soft_limit_files': settings['stdout_keep_files'],
                'retention_boundary': 'ROTATED_RAW_30_DAYS_COMPRESSED_180_DAYS_PROTECTIONS_OVERRIDE'}
    except BlockingIOError:
        return {'state': 'BUSY', 'reason': 'RETENTION_LOCKED', 'changed': False}
    except Exception:
        return {'state': 'WAITING', 'reason': 'STDIO_ROTATION_UNAVAILABLE', 'changed': False}
