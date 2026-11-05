"""Small local observation adapters. Never create locks or call business APIs."""

import fcntl
import json
import os
import sqlite3
import stat
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from time import monotonic

from china_stock_sim.ledger.kernel import digest
from china_stock_sim.readiness.domain import TZ
from china_stock_sim.readiness.writer_owner import MAX_OWNER_BYTES, OWNER_PATH
from china_stock_sim.scheduler.process import probe_process
from china_stock_sim.scheduler.state import WorkflowJournal

MAX_JOURNAL = 8 * 1024 * 1024
SAFE_PHASES = {'report_plan', 'backup'}
SUCCESS = {'COMPLETE', 'READY', 'APPLIED', 'ALREADY_APPLIED', 'FROZEN'}
REUSED = {'FROZEN_PLAN_EXISTS', 'CURRENT_REPORT_ALREADY_PUBLISHED', 'COMPLETE_BACKUP_ALREADY_EXISTS'}


def file_stamp(path):
    try:
        item = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(item.st_mode):
        raise ValueError('unsafe local file')
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


def _instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('naive time')
    return result.astimezone(TZ)


def _successful(row):
    result = row.get('result', {})
    return (row.get('state') == 'COMPLETE' and result.get('state') in SUCCESS
            and result.get('completion') is not False or row.get('state') == 'SKIPPED'
            and result.get('reason') in REUSED)


def journal_facts(settings, day, target, now):
    path = settings.root / 'data/state/workflow/journal.json'
    stamp = file_stamp(path)
    if stamp is None or stamp[2] > MAX_JOURNAL:
        raise ValueError('journal unavailable')
    raw = WorkflowJournal(settings).read_current()
    if file_stamp(path) != stamp:
        raise ValueError('journal changed')
    reviews, phases, checks = {}, [], {}
    total = 0
    for run in raw['runs'].values():
        for attempt_index, attempt in enumerate(run.get('attempts', [])):
            starts = {}
            for phase_index, row in enumerate(attempt.get('phases', [])):
                total += 1
                if total > 20000:
                    raise ValueError('journal range')
                if row.get('session') != day or row.get('target_session') != target:
                    continue
                when = _instant(row['recorded_at'])
                if when > now:
                    raise ValueError('future journal')
                if row.get('phase') in {'input_review', 'plan'} and row.get('account_id'):
                    key = (row['account_id'], row['phase'])
                    order = (when, _instant(attempt['started_at']), attempt_index, phase_index)
                    if row.get('state') == 'RUNNING':
                        starts[key] = when
                    started = starts.get(key)
                    watermark = (started if started and started <= when and _successful(row) else None)
                    if key not in checks or order > checks[key][0]:
                        checks[key] = (order, watermark)
                    if row.get('state') != 'RUNNING':
                        starts.pop(key, None)
    for spec in settings.accounts:
        input_review = checks.get((spec.id, 'input_review'))
        plan_review = checks.get((spec.id, 'plan'))
        if input_review and plan_review and input_review[1] and plan_review[1]:
            reviews[spec.id] = min(input_review[1], plan_review[1])
    current = raw.get('runs', {}).get(raw.get('current'))
    attempt = current.get('attempts', [])[-1] if current and current.get('attempts') else None
    value = {'fingerprint': digest(raw), 'stamp': stamp, 'reviews': reviews,
             'state': 'IDLE', 'run_id': raw.get('current'), 'pid': None, 'birth': None, 'phase': None,
             'tail_safe': False}
    if attempt is None:
        return value
    if _instant(attempt['started_at']) > now:
        raise ValueError('future start')
    if attempt.get('state') != 'RUNNING':
        if not attempt.get('finished_at') or _instant(attempt['finished_at']) > now:
            raise ValueError('unfinished journal')
        return value
    pid = attempt.get('pid')
    birth = attempt.get('metadata', {}).get('process_birth')
    actual = probe_process(pid)
    value.update(pid=pid, birth=birth)
    if actual.get('state') != 'ALIVE' or not birth or actual.get('birth') != birth:
        return {**value, 'state': 'UNKNOWN'}
    for row in attempt.get('phases', []):
        if _instant(row['recorded_at']) > now:
            raise ValueError('future phase')
        phases.append(row)
    if not phases:
        return {**value, 'state': 'WORKING'}
    latest = max(enumerate(phases), key=lambda item: (_instant(item[1]['recorded_at']), item[0]))[1]
    # The newest observation must itself be the safe tail. Do not look backward
    # past a later financial/recovery phase, or across runs/PID generations.
    completed = {row.get('account_id') for row in phases if row.get('phase') == 'plan'
                 and row.get('session') == day and row.get('target_session') == target and _successful(row)}
    safe = (latest.get('phase') in SAFE_PHASES and latest.get('session') == day
            and latest.get('target_session') == target
            and latest.get('state') in {'RUNNING', 'COMPLETE', 'SKIPPED'}
            and completed >= {spec.id for spec in settings.accounts})
    return {**value, 'state': 'WORKING', 'phase': latest.get('phase'), 'tail_safe': safe}


def lock_owner(path, owner):
    """Confirm the published owner FD, not Darwin's unavailable flock type field."""
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-FpfDit', '--', str(path)],
                                capture_output=True, text=True, timeout=0.7, check=False,
                                env={'PATH': '/usr/bin:/bin:/usr/sbin', 'LC_ALL': 'C'})
        if result.returncode not in {0, 1} or len(result.stdout) > 32768:
            return None
        owners, records, current, pid = set(), [], {}, None
        for row in result.stdout.splitlines():
            if row.startswith('p'):
                if current:
                    records.append(current)
                    current = {}
                pid = int(row[1:])
            elif row.startswith('f'):
                if current:
                    records.append(current)
                current = {'pid': pid, 'fd': int(row[1:])}
            elif row.startswith('i'):
                current['ino'] = int(row[1:])
            elif row.startswith('D'):
                current['dev'] = int(row[1:], 16)
            elif row.startswith('t'):
                current['type'] = row[1:]
        if current:
            records.append(current)
        matching = [r for r in records if r.get('ino') == owner['lock_ino']
                    and r.get('dev') == owner['lock_dev'] and r.get('type') == 'REG']
        owners = {r['pid'] for r in matching}
        if owners == {owner['pid']} and any(r['fd'] == owner['fd'] for r in matching):
            return owner['pid']
        return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def lock_fact(path, *, owner=None):
    before = file_stamp(path)
    if before is None:
        return {'state': 'FREE', 'stamp': None, 'owner': None}
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != before[:2]:
            raise ValueError('lock replaced')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            value = {'state': 'HELD', 'owner': None}
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            value = {'state': 'FREE', 'owner': None}
    finally:
        os.close(descriptor)
    if value['state'] == 'HELD' and owner:
        value['owner'] = lock_owner(path, owner)
    if file_stamp(path) != before:
        raise ValueError('lock changed')
    return {**value, 'stamp': before}


def locks(settings):
    path = settings.root / OWNER_PATH
    stamp = file_stamp(path)
    owner = None
    if stamp and stamp[2] <= MAX_OWNER_BYTES:
        try:
            value = json.loads(path.read_bytes())
            if (set(value) == {'schema_version', 'pid', 'process_birth', 'token', 'fd', 'lock_dev', 'lock_ino'}
                    and value['schema_version'] == 1 and isinstance(value['process_birth'], str)
                    and value['process_birth'] and isinstance(value['token'], str) and len(value['token']) == 32
                    and all(type(value[k]) is int and value[k] >= 0
                            for k in ('pid', 'fd', 'lock_dev', 'lock_ino')) and value['pid'] > 0):
                owner = value
        except (OSError, ValueError, TypeError):
            owner = None
    result = {name: lock_fact(settings.root / ('data/state/' + name + '.lock'),
                             owner=owner if name == 'writer' else None)
              for name in ('writer', 'ledger-write', 'market')}
    if file_stamp(path) != stamp:
        raise ValueError('writer owner changed')
    actual = result['writer']['stamp']
    if not owner or not actual or actual[:2] != (owner['lock_dev'], owner['lock_ino']):
        result['writer']['owner'] = None
    result['writer'].update(owner_metadata=owner, owner_stamp=stamp)
    return result


def maintenance(settings):
    path = settings.root / 'data/state/dashboard-maintenance.json'
    stamp = file_stamp(path)
    if stamp is None:
        return {'active': False, 'stamp': None}
    if stamp[2] > 16384:
        raise ValueError('maintenance too large')
    body = json.loads(path.read_bytes())
    if type(body.get('active')) is not bool or file_stamp(path) != stamp:
        raise ValueError('maintenance invalid')
    return {'active': body['active'], 'stamp': stamp}


def market_version(settings, now):
    path = settings.root / 'data/state/market.sqlite3'
    if file_stamp(path) is None:
        raise ValueError('market missing')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
        deadline = monotonic() + 1.0
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        # No payload decoding. MAX(rowid) is the append-only generation; the
        # timestamp is a conservative global freshness barrier, not an economic
        # equivalence assertion. A later successful business review clears it.
        generation = db.execute('SELECT COALESCE(MAX(rowid),0) FROM observations').fetchone()[0]
        latest = db.execute('SELECT MAX(available_at) FROM observations '
                            'INDEXED BY observation_lookup WHERE available_at<=?',
                            (now.astimezone(timezone.utc).isoformat(),)).fetchone()[0]
        result = {'generation': generation, 'latest_at': _instant(latest) if latest else None}
        db.rollback()
        return result


def operational(journal, lock_states, maintained):
    if maintained['active']:
        return {'state': 'UNKNOWN', 'reasons': ['MAINTENANCE_ACTIVE']}
    if lock_states['ledger-write']['state'] != 'FREE':
        return {'state': 'WORKING', 'reasons': ['FINANCIAL_WRITER_ACTIVE']}
    if lock_states['market']['state'] != 'FREE':
        return {'state': 'WORKING', 'reasons': ['WORKFLOW_ACTIVE']}
    outer = lock_states['writer']
    metadata = outer.get('owner_metadata') or {}
    if outer['state'] != 'FREE' and not (journal['state'] == 'WORKING' and journal['tail_safe']
                                        and outer['owner'] == journal['pid']
                                        and metadata.get('pid') == journal['pid']
                                        and metadata.get('process_birth') == journal['birth']):
        return {'state': 'UNKNOWN', 'reasons': ['WRITER_IDENTITY_UNKNOWN']}
    if journal['state'] == 'UNKNOWN':
        return {'state': 'UNKNOWN', 'reasons': ['WRITER_IDENTITY_UNKNOWN']}
    if journal['state'] == 'WORKING' and not journal['tail_safe']:
        return {'state': 'WORKING', 'reasons': ['WORKFLOW_ACTIVE']}
    optional = []
    if journal['state'] == 'WORKING':
        optional.append('备份仍进行中' if journal['phase'] == 'backup' else '报告仍在更新')
    return {'state': 'READY', 'reasons': [], 'optional_work': optional}
