"""Bounded, strictly verified calendar projection for frequent local status reads."""

import json
from dataclasses import dataclass
from datetime import date, timezone
from time import monotonic

from china_stock_sim.config import local_path
from china_stock_sim.market.contracts import DataResult, aware_time, session_date
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.storage import digest

MAX_ROWS = 742
MAX_SOURCES = 32
MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 4096


@dataclass(frozen=True)
class CalendarResult(DataResult):
    dependencies: tuple = ()


def dependencies_current(root, result):
    try:
        return all(_stamp(local_path(root, path)) == stamp for path, stamp in result.dependencies)
    except (OSError, ValueError):
        return False


def _stamp(path):
    if path.is_symlink() or not path.is_file():
        raise CorruptData('CALENDAR_SOURCE_UNAVAILABLE')
    item = path.stat()
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


def read_calendar(root, *, start, end, knowledge_cutoff):
    """Read selected calendar rows and at most 2MiB of immutable source closure.

    Historical versions are ranked in SQL, not decoded in Python. Missing facts
    are never filled with weekdays. No repository writes or derived caches.
    """
    start, end = session_date(start), session_date(end)
    if not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days < MAX_ROWS:
        raise ValueError('CALENDAR_RANGE_EXCEEDED')
    repo = MarketRepository(root)
    with repo.connection() as db:
        if db is None:
            return DataResult('MISSING', [])
        deadline = monotonic() + 1.0
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows = db.execute('''WITH ranked AS (
            SELECT rowid AS rid, ROW_NUMBER() OVER (
                PARTITION BY record_key ORDER BY available_at DESC,rowid DESC) AS n
            FROM observations WHERE kind='calendar' AND session>=? AND session<=?
                AND available_at<=?)
            SELECT o.id,o.kind,o.record_key,o.code,o.session,o.available_at,o.fetched_at,
                o.provider,o.raw_ref,o.checksum,
                CASE WHEN length(CAST(o.payload AS BLOB))<=? THEN o.payload ELSE NULL END AS payload
            FROM ranked r JOIN observations o ON o.rowid=r.rid WHERE r.n=1 LIMIT ?''',
            (start, end, aware_time(knowledge_cutoff).astimezone(timezone.utc).isoformat(),
             MAX_PAYLOAD_BYTES, MAX_ROWS + 1)).fetchall()
        db.rollback()
    if len(rows) > MAX_ROWS or any(row['payload'] is None for row in rows):
        raise CorruptData('CALENDAR_INDEX_LIMIT_EXCEEDED')
    queue, stamps, verified, total = list({row['raw_ref'] for row in rows}), {}, {}, 0
    while queue:
        reference = queue.pop()
        if reference in stamps:
            continue
        path = local_path(root, reference)
        if not path.is_relative_to(repo.objects) or (root / reference).is_symlink():
            raise CorruptData('CALENDAR_SOURCE_UNAVAILABLE')
        stamp = _stamp(path)
        total += stamp[2]
        if len(stamps) >= MAX_SOURCES or total > MAX_RAW_BYTES:
            raise CorruptData('CALENDAR_SOURCE_LIMIT_EXCEEDED')
        with path.open('rb') as stream:
            raw = stream.read(stamp[2] + 1)
        if len(raw) != stamp[2]:
            raise CorruptData('CALENDAR_SOURCE_CHANGED')
        body = json.loads(raw)
        if _stamp(path) != stamp or not isinstance(body, dict):
            raise CorruptData('CALENDAR_SOURCE_CHANGED')
        if digest(body) != path.stem:
            raise CorruptData('CALENDAR_SOURCE_HASH_MISMATCH')
        dependencies = body.get('dependencies', [])
        if not isinstance(dependencies, list) or any(not isinstance(ref, str) for ref in dependencies):
            raise CorruptData('CALENDAR_SOURCE_DEPENDENCIES_INVALID')
        stamps[reference] = stamp
        # This is the same content-hash and recursive dependency contract as
        # validate_raw, bounded before reading. _decode reuses these verified
        # bodies, preserving provider/time/index checks without a second IO race.
        verified[reference] = body
        queue.extend(dependencies)
    decoded = [repo._decode(row, verify=True, verified=verified) for row in rows]
    if any(_stamp(local_path(root, ref)) != stamp for ref, stamp in stamps.items()):
        raise CorruptData('CALENDAR_SOURCE_CHANGED')
    decoded.sort(key=lambda row: (row['code'], row['session'], row['id']))
    return CalendarResult('READY' if decoded else 'MISSING', [row['payload'] for row in decoded],
                          observation_ids=tuple(row['id'] for row in decoded),
                          dependencies=tuple(sorted(stamps.items())))
