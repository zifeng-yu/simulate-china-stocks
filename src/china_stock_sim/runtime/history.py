"""Bounded, read-only runtime history; missing or corrupt evidence never means success."""

import heapq
import json
import os
import re
import stat
from pathlib import Path

from china_stock_sim.scheduler.process import probe_process

from .recording import (
    _SENSITIVE,
    MAX_EVENT_BYTES,
    TASKS,
    _code,
    _day,
    _field_identity,
    _instant,
    _number,
    _safe_metrics,
    owned,
    safe_exception,
    safe_fields,
)

MAX_SCAN_FILES = 500
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 5000
MAX_EVENTS_PER_FILE = 10_000
MAX_RETURN_EVENTS = 50
MAX_PROCESS_PROBES = 5
_EVENTS = {'START', 'END', 'EVENT', 'STAGE_START', 'STAGE_END', 'STAGE_ERROR'}


def _inventory(root, selected_date, invocation_id):
    details = {'entries_seen': 0, 'files_seen': 0, 'known_bytes': 0,
               'unsafe_entries': 0, 'unrecognized_entries': 0, 'truncated': False}
    base = owned(root, 'logs/runtime')
    if not base.exists():
        return [], details
    if not base.is_dir():
        raise ValueError('RUNTIME_STORAGE_INVALID')
    days = []
    with os.scandir(base) as entries:
        for entry in entries:
            details['entries_seen'] += 1
            if details['entries_seen'] > MAX_DIRECTORY_ENTRIES:
                details['truncated'] = True
                break
            if entry.is_symlink():
                details['unsafe_entries'] += 1
            elif _day(entry.name) and entry.is_dir(follow_symlinks=False):
                if selected_date is None or entry.name == selected_date:
                    days.append(entry.name)
            else:
                details['unrecognized_entries'] += 1
    candidates = []
    for day in sorted(days, reverse=True):
        folder = owned(root, 'logs/runtime/' + day)
        with os.scandir(folder) as entries:
            for entry in entries:
                details['entries_seen'] += 1
                if details['entries_seen'] > MAX_DIRECTORY_ENTRIES:
                    details['truncated'] = True
                    break
                if entry.is_symlink():
                    details['unsafe_entries'] += 1
                    continue
                if not re.fullmatch(r'[0-9a-f]{32}\.jsonl', entry.name):
                    details['unrecognized_entries'] += 1
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    details['unsafe_entries'] += 1
                    continue
                details['files_seen'] += 1
                details['known_bytes'] += info.st_size
                if invocation_id is not None and entry.name != invocation_id + '.jsonl':
                    continue
                row = (day, info.st_mtime_ns, f'logs/runtime/{day}/{entry.name}', info.st_size)
                heapq.heappush(candidates, row)
                if len(candidates) > MAX_SCAN_FILES:
                    heapq.heappop(candidates)
                    details['truncated'] = True
        if details['entries_seen'] > MAX_DIRECTORY_ENTRIES:
            break
    return sorted(candidates, reverse=True), details


def _identity(root, raw):
    if not isinstance(raw, dict) or raw.get('state') != 'READY':
        return {'state': 'UNAVAILABLE'}, False
    identity, manifest = raw.get('identity_id'), raw.get('manifest_path')
    if (not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity)
            or manifest != f'data/normalized/runtime-evidence/identities/{identity}.json'):
        return {'state': 'INVALID'}, False
    try:
        exists = owned(root, manifest).is_file()
    except (OSError, ValueError):
        exists = False
    return {'state': 'READY' if exists else 'MISSING', 'identity_id': identity,
            'manifest_path': manifest}, exists


def _event(raw, root):
    result = {key: raw[key] for key in ('event', 'recorded_at', 'sequence')}
    result.update(safe_fields(raw, root))
    for key in ('stage', 'kind'):
        value = raw.get(key)
        if (isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_.]{0,63}', value)
                and not _SENSITIVE.search(value)):
            result[key] = value
    if isinstance(raw.get('stage_id'), str) and re.fullmatch(r'[0-9a-f]{32}', raw['stage_id']):
        result['stage_id'] = raw['stage_id']
    if isinstance(raw.get('result'), dict):
        result['result'] = safe_fields(raw['result'], root)
    if isinstance(raw.get('metrics'), dict):
        result['metrics'] = _safe_metrics(raw['metrics'])
    if isinstance(raw.get('exception'), dict):
        result['exception'] = safe_exception(raw['exception'])
    return result


def _open_state(start, issues, probe):
    if issues:
        return 'UNKNOWN'
    pid, birth = start.get('pid'), start.get('process_birth')
    if type(pid) is not int or not 0 < pid <= 2**31 - 1 or not isinstance(birth, str) or not birth:
        return 'UNKNOWN'
    try:
        observed = probe(pid)
    except Exception:
        return 'UNKNOWN'
    if observed.get('state') == 'DEAD':
        return 'INTERRUPTED'
    if observed.get('state') == 'ALIVE':
        return 'RUNNING' if observed.get('birth') == birth else 'INTERRUPTED'
    return 'UNKNOWN'


def _read(root, relative, budget, probe):
    path = owned(root, relative)
    expected_id = path.stem
    issues, values, used = [], [], 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('RUNTIME_UNSAFE_FILE')
        allowed = min(MAX_FILE_BYTES, budget)
        raw = stream.read(allowed + 1)
    if len(raw) > allowed:
        return None, allowed, ['FILE_BUDGET_EXCEEDED']
    used = len(raw)
    if raw and not raw.endswith(b'\n'):
        issues.append('TRUNCATED_TAIL')
    previous_sequence = 0
    task = None
    for index, line in enumerate(raw.splitlines(keepends=True)):
        if index >= MAX_EVENTS_PER_FILE:
            issues.append('EVENT_BUDGET_EXCEEDED')
            break
        if len(line) > MAX_EVENT_BYTES:
            issues.append('OVERSIZED_EVENT')
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            issues.append('INVALID_EVENT_JSON')
            continue
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value.get('schema_version') != 1
                or not isinstance(value.get('event'), str) or value.get('event') not in _EVENTS
                or value.get('invocation_id') != expected_id
                or not isinstance(value.get('task'), str) or value.get('task') not in TASKS
                or not _instant(value.get('recorded_at'))
                or type(value.get('sequence')) is not int or value['sequence'] < 1):
            issues.append('INVALID_EVENT_SCHEMA')
            continue
        if task is not None and task != value['task']:
            issues.append('TASK_MISMATCH')
            continue
        task = value['task']
        if value['sequence'] != previous_sequence + 1:
            issues.append('EVENT_SEQUENCE_GAP')
        metrics = value.get('metrics')
        if metrics is not None:
            known = {'elapsed_seconds', 'cpu_user_seconds', 'cpu_system_seconds',
                     'child_cpu_user_seconds', 'child_cpu_system_seconds',
                     'cpu_percent_one_core', 'peak_rss_bytes', 'disk_free_bytes'}
            if (not isinstance(metrics, dict) or any(item is not None and not _number(item)
                    for key, item in metrics.items() if key in known)):
                issues.append('INVALID_METRIC_FIELDS')
        previous_sequence = value['sequence']
        values.append(value)
    starts = [v for v in values if v['event'] == 'START']
    ends = [v for v in values if v['event'] == 'END']
    if len(starts) != 1 or not values or values[0]['event'] != 'START':
        issues.append('START_MISSING_OR_DUPLICATED')
    if len(ends) > 1 or ends and values[-1]['event'] != 'END':
        issues.append('END_DUPLICATED_OR_NOT_LAST')
    start, end = starts[0] if starts else {}, ends[-1] if ends else {}
    if start and start['recorded_at'][:10] != path.parent.name:
        issues.append('START_DATE_MISMATCH')
    identity, identity_present = _identity(root, start.get('identity'))
    identity_issue = not identity_present
    event_summaries = [_event(v, root) for v in values if v['event'] not in {'START', 'END'}]
    state = ('CORRUPT' if issues else 'FINISHED' if end else _open_state(start, issues, probe))
    summary = safe_fields(end.get('result', {}), root) if isinstance(end.get('result'), dict) else {}
    exit_code = end.get('exit_code') if type(end.get('exit_code')) is int else None
    result_state = end.get('result_state') if _code(end.get('result_state')) else None
    record = {'invocation_id': expected_id, 'task': task, 'state': state,
              'started_at': start.get('recorded_at'), 'finished_at': end.get('recorded_at'),
              'pid': start.get('pid') if type(start.get('pid')) is int else None,
              'scheduled': start.get('scheduled') if type(start.get('scheduled')) is bool else None,
              'identity': identity, 'exit_code': exit_code, 'result_state': result_state,
              'result': summary, 'metrics': _safe_metrics(end.get('metrics')),
              'events': event_summaries[-MAX_RETURN_EVENTS:],
              'event_count': len(values), 'events_truncated': len(event_summaries) > MAX_RETURN_EVENTS,
              'issues': sorted(set(issues)), 'identity_missing': identity_issue,
              'closed': bool(end), 'record_path': relative, 'bytes': used,
              'exception': safe_exception(end.get('exception')),
              'dropped_events': end.get('dropped_events') if type(end.get('dropped_events')) is int
              and end['dropped_events'] >= 0 else None}
    record['omitted_field_count'] = summary.get('omitted_field_count', 0) + sum(
        row.get('omitted_field_count', 0) + row.get('result', {}).get('omitted_field_count', 0)
        for row in event_summaries)
    runtime_info = start.get('runtime_info')
    if isinstance(runtime_info, dict):
        record['runtime_info'] = {key: value for key, value in runtime_info.items()
            if key in {'python', 'os', 'os_release', 'architecture'} and isinstance(value, str)
            and re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', value) and not _SENSITIVE.search(value)}
        cores = runtime_info.get('cpu_count')
        record['runtime_info']['cpu_count'] = cores if type(cores) is int and 0 < cores < 100_000 else None
    # Match on all parsed summaries, even when the returned event tail is bounded.
    identities = {key: set() for key in ('run_id', 'account_id', 'decision_id')}
    for row in [summary, *event_summaries]:
        for fields in (row, row.get('result', {})):
            for key in identities:
                if _field_identity(key, fields.get(key)):
                    identities[key].add(fields[key])
    return (record, identities), used, []


def _storage(root, inventory):
    result = {'runtime_files_observed': inventory['files_seen'],
              'runtime_bytes_observed': inventory['known_bytes'],
              'inventory_truncated': inventory['truncated'], 'supplementary_logs': [],
              'workflow_journal_bytes': None, 'workflow_journal_limit_bytes': 100_000_000,
              'workflow_journal_warning': 'UNKNOWN'}
    for name in ('scheduler-launchd.stdout.log', 'scheduler-launchd.stderr.log',
                 'dashboard-refresh.stdout.log', 'dashboard-refresh.stderr.log',
                 'publishing-launchd.stdout.log', 'publishing-launchd.stderr.log'):
        try:
            path = owned(root, 'logs/' + name)
            value = path.stat().st_size if path.is_file() else None
            state = 'PRESENT' if value is not None else 'MISSING'
        except (OSError, ValueError):
            value, state = None, 'UNAVAILABLE'
        result['supplementary_logs'].append({'path': 'logs/' + name, 'bytes': value, 'state': state})
    try:
        path = owned(root, 'data/state/workflow/journal.json')
        if path.is_file():
            size = path.stat().st_size
            result.update(workflow_journal_bytes=size, workflow_journal_warning=
                          'LIMIT_EXCEEDED' if size > 100_000_000 else 'NEAR_LIMIT'
                          if size >= 80_000_000 else 'BELOW_LIMIT')
        else:
            result['workflow_journal_warning'] = 'NOT_PRESENT'
    except (OSError, ValueError):
        pass
    return result


def _archive_catalog(root, selected_date, invocation_id):
    """Bounded catalog only; never inflate archives or pretend catalog presence verifies bytes."""
    result = {'state': 'NONE', 'entries': [], 'truncated': False,
              'scope': 'ARCHIVE_CATALOG_ONLY', 'verification': 'NOT_RUN'}
    try:
        base = owned(root, 'data/backups/runtime')
        if not base.is_dir():
            return result
        seen, used = 0, 0
        for folder in sorted(base.iterdir(), reverse=True):
            seen += 1
            if seen > MAX_SCAN_FILES:
                result['truncated'] = True
                break
            owned(root, str(folder.relative_to(root)))
            if not _day(folder.name) or not folder.is_dir() or selected_date and folder.name != selected_date:
                continue
            for path in sorted(folder.iterdir(), reverse=True):
                seen += 1
                if seen > MAX_SCAN_FILES or used > MAX_SCAN_BYTES:
                    result['truncated'] = True
                    break
                owned(root, str(path.relative_to(root)))
                if not re.fullmatch(r'[0-9a-f]{64}\.manifest\.json', path.name):
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    result['truncated'] = True
                    continue
                raw = path.read_bytes()
                used += len(raw)
                receipt = json.loads(raw)
                archive = str(path.relative_to(root)).removesuffix('.manifest.json') + '.tar.gz'
                if receipt.get('archive_path') != archive or not owned(root, archive).is_file():
                    continue
                manifest = receipt.get('manifest', {})
                boundaries = manifest.get('boundaries', [])
                if not isinstance(boundaries, list):
                    continue
                if invocation_id and not any(isinstance(row, dict)
                        and row.get('invocation_id') == invocation_id for row in boundaries):
                    continue
                result['entries'].append({'archive_path': archive, 'date': folder.name,
                    'manifest_path': str(path.relative_to(root)), 'log_count': len(boundaries),
                    'captured_at': manifest.get('captured_at') if _instant(manifest.get('captured_at')) else None,
                    'claimed_state': manifest.get('state') if manifest.get('state') in {'COMPLETE', 'PARTIAL'} else 'UNKNOWN'})
                if len(result['entries']) >= 50:
                    result['truncated'] = True
                    break
            if result['truncated']:
                break
        result['state'] = 'ARCHIVED_AVAILABLE' if result['entries'] else 'NONE'
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        result['state'] = 'UNAVAILABLE'
    return result


def query_history(root, *, date=None, task=None, invocation_id=None, run_id=None,
                  account_id=None, decision_id=None, limit=20):
    """Inspect only local evidence with finite budgets; no directories or locks are created."""
    if date is not None and not _day(date):
        raise ValueError('RUNTIME_INVALID_DATE')
    if task is not None and task not in TASKS:
        raise ValueError('RUNTIME_INVALID_TASK')
    if invocation_id is not None and (not isinstance(invocation_id, str)
                                     or not re.fullmatch(r'[0-9a-f]{32}', invocation_id)):
        raise ValueError('RUNTIME_INVALID_INVOCATION_ID')
    identities = {'run_id': run_id, 'account_id': account_id, 'decision_id': decision_id}
    if any(v is not None and not _field_identity(k, v) for k, v in identities.items()):
        raise ValueError('RUNTIME_INVALID_FILTER')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('RUNTIME_INVALID_LIMIT')
    root = Path(root).resolve(strict=True)
    rows, inventory = _inventory(root, date, invocation_id)
    records, skipped, scanned, used = [], [], 0, 0
    processes = {}

    def probe(pid):
        if pid not in processes:
            if len(processes) >= MAX_PROCESS_PROBES:
                return {'state': 'UNKNOWN'}
            try:
                processes[pid] = probe_process(pid)
            except Exception:
                processes[pid] = {'state': 'UNKNOWN'}
        return processes[pid]

    counts = {'corrupt': 0, 'unclosed': 0, 'interrupted': 0, 'unknown': 0,
              'identity_missing': 0, 'recording_gaps': 0, 'omitted_fields': 0}
    scanned_counts = dict(counts)
    for _, _, relative, size in rows:
        if used >= MAX_SCAN_BYTES:
            skipped.append({'reason': 'SCAN_BYTE_BUDGET_EXCEEDED'})
            break
        scanned += 1
        try:
            parsed, read_bytes, issues = _read(root, relative, MAX_SCAN_BYTES - used, probe)
            used += read_bytes
        except (OSError, ValueError):
            skipped.append({'record_path': relative, 'reason': 'RECORD_UNREADABLE'})
            continue
        if issues:
            skipped.append({'record_path': relative, 'reason': issues[0]})
        if parsed is None:
            continue
        row, found = parsed
        observed = {'corrupt': bool(row['issues']), 'unclosed': not row['closed'],
                    'interrupted': row['state'] == 'INTERRUPTED',
                    'unknown': row['state'] == 'UNKNOWN',
                    'identity_missing': row['identity_missing'],
                    'recording_gaps': bool(row['dropped_events']),
                    'omitted_fields': row['omitted_field_count']}
        for key, value in observed.items():
            scanned_counts[key] += value
        if task is not None and row['task'] != task:
            continue
        if any(value is not None and value not in found[key] for key, value in identities.items()):
            continue
        for key, value in observed.items():
            counts[key] += value
        records.append(row)
    records.sort(key=lambda row: (row['started_at'] or '', row['invocation_id']), reverse=True)
    matched, returned = len(records), records[:limit]
    timestamps = [row['started_at'] for row in records if row['started_at']]
    scan_truncated = inventory['truncated'] or bool(skipped)
    results_truncated = matched > limit
    truncated = scan_truncated or results_truncated
    partial_reasons = []
    if any(counts[key] for key in counts if key != 'unclosed'):
        partial_reasons.append('MATCHING_RECORD_QUALITY')
    if scanned_counts['corrupt']:
        partial_reasons.append('SCANNED_RECORD_CORRUPTION')
    if inventory['unsafe_entries']:
        partial_reasons.append('UNSAFE_STORAGE_ENTRIES')
    if scan_truncated:
        partial_reasons.append('SCAN_INCOMPLETE')
    archives = _archive_catalog(root, date, invocation_id)
    return {'state': 'PARTIAL' if partial_reasons else 'READY',
            'schema_version': 1, 'records': returned,
            'archives': archives,
            'filters': {'date': date, 'task': task, 'invocation_id': invocation_id,
                        **identities, 'limit': limit},
            'coverage': {'scope': 'SCANNED_LOCAL_INVOCATIONS_ONLY', 'scanned_files': scanned,
                         'scanned_bytes': used, 'matched': matched, 'returned': len(returned),
                         'truncated': truncated, 'counts': counts, 'inventory': inventory,
                         'counts_scope': 'MATCHING_RECORDS', 'scanned_counts': scanned_counts,
                         'partial_reasons': partial_reasons,
                         'scan_truncated': scan_truncated,
                         'results_truncated': results_truncated,
                         'earliest_matching_start': min(timestamps) if timestamps else None,
                         'latest_matching_start': max(timestamps) if timestamps else None,
                         'skipped': skipped[:100],
                         'budgets': {'files': MAX_SCAN_FILES, 'bytes': MAX_SCAN_BYTES,
                                     'file_bytes': MAX_FILE_BYTES,
                                     'directory_entries': MAX_DIRECTORY_ENTRIES,
                                     'process_probes': MAX_PROCESS_PROBES,
                                     'events_per_record': MAX_RETURN_EVENTS}},
            'storage': _storage(root, inventory),
            'limits': ['Earlier executions without these records cannot be reconstructed.',
                       'Resource values are observations, not financial correctness assertions.',
                       'Source identity references are checked for presence; archive verification '
                       'provides full identity/object digest validation.',
                       'Archive catalog presence is not content verification; raw-only filters and counts '
                       'do not cover archived invocation bodies. Use runtime-archive-verify for integrity.',
                       'No END means live/interrupted/unknown by PID and birth identity; '
                       'no finished timestamp is invented.']}
