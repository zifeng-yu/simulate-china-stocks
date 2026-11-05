"""Small, read-only dependency identities for already-published statement inputs.

This is an invalidation hint, never a replacement for the public fact query.
Changes force that query to verify its complete evidence before reuse.
"""

import hashlib
import json
import sqlite3
from collections import OrderedDict
from zoneinfo import ZoneInfo

from china_stock_sim.config import local_path
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.market.quality import normalize_code
from china_stock_sim.market.storage import digest

_SOURCES = OrderedDict()
_KINDS = {'calendar': ('calendar',), 'calendar_operation': ('calendar',),
          'prices': ('bar', 'calendar'), 'events': ('event', 'event_batch', 'coverage'),
          'event_coverage': ('coverage',)}


def _file_stamp(path):
    value = path.stat()
    if not path.is_file():
        raise ValueError('来源不是常规文件')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _sources(repository, references):
    result, active = {}, set()

    def visit(reference):
        if reference in result:
            return
        if reference in active or len(active) >= 64 or len(result) >= 4096:
            raise ValueError('来源依赖循环或超出范围')
        active.add(reference)
        path = local_path(repository.root, reference)
        if not path.is_relative_to(repository.objects):
            raise ValueError('来源路径越界')
        stamp = _file_stamp(path)
        key = str(path), tuple(stamp)
        children = _SOURCES.get(key)
        if children is None:
            if stamp[2] > 64 * 1024 * 1024:
                raise ValueError('来源对象超出有界范围')
            body = json.loads(path.read_bytes())
            if _file_stamp(path) != stamp or not isinstance(body, dict):
                raise ValueError('来源读取期间变化')
            children = body.get('dependencies', [])
            if not isinstance(children, list) or any(not isinstance(p, str) for p in children):
                raise ValueError('来源依赖格式无效')
            _SOURCES[key] = children
            _SOURCES.move_to_end(key)
            while len(_SOURCES) > 4096:
                _SOURCES.popitem(last=False)
        for child in children:
            visit(child)
        active.remove(reference)
        result[reference] = stamp

    for reference in sorted(references):
        visit(reference)
    return result


def fingerprint(repository, method, kwargs):
    """Bind only this query's visible facts, relevant calendar and source files.

    New observations outside the code/date scope and request/job writes do not
    invalidate the result. Crossing a knowledge boundary does. Index payload
    bytes are hashed too, so changing a row without updating its checksum cannot
    hide behind a cached check. Raw dependencies use complete file identities.
    """
    if method not in _KINDS or not isinstance(kwargs, dict):
        return {'state': 'UNAVAILABLE', 'reason': 'UNSUPPORTED_DEPENDENCY_QUERY'}
    try:
        cutoff = aware_time(kwargs['knowledge_cutoff'])
        code = normalize_code(kwargs['code']) if kwargs.get('code') else None
        start = session_date(kwargs['start']) if kwargs.get('start') else None
        end = session_date(kwargs['end']) if kwargs.get('end') else None
        rows = []
        with repository.connection() as connection:
            if connection is not None:
                for kind in _KINDS[method]:
                    clauses, args = ['kind=?', 'available_at<=?'], [
                        kind, cutoff.astimezone(ZoneInfo('UTC')).isoformat()]
                    if code and kind != 'calendar':
                        clauses.append('code=?')
                        args.append(code)
                    if method in {'calendar', 'prices'}:
                        for symbol, value in (('>=', start), ('<=', end)):
                            if value:
                                clauses.append('session' + symbol + '?')
                                args.append(value)
                    # Windowing occurs in SQL, before Python decodes payloads.
                    sql = ('SELECT * FROM (SELECT *, row_number() OVER '
                           '(PARTITION BY record_key ORDER BY available_at DESC,rowid DESC) AS rank '
                           'FROM observations WHERE ' + ' AND '.join(clauses) + ') WHERE rank=1 LIMIT 50001')
                    selected = connection.execute(sql, args).fetchall()
                    if len(selected) > 50000:
                        return {'state': 'UNAVAILABLE', 'reason': 'DEPENDENCY_SCOPE_TOO_LARGE'}
                    rows.extend(dict(row) for row in selected)
                # Current event batches can explicitly retain older event IDs.
                members = set()
                for row in rows:
                    if row['kind'] == 'event_batch':
                        members.update(json.loads(row['payload']).get('members', []))
                if len(members) > 50000:
                    return {'state': 'UNAVAILABLE', 'reason': 'DEPENDENCY_SCOPE_TOO_LARGE'}
                known = {row['id'] for row in rows}
                for identity in sorted(members - known):
                    row = connection.execute('SELECT * FROM observations WHERE id=?', (identity,)).fetchone()
                    if row is None:
                        raise ValueError('事件批次成员缺失')
                    rows.append(dict(row))
        facts = []
        for row in rows:
            facts.append({key: row[key] for key in ('id', 'kind', 'record_key', 'code', 'session',
                'available_at', 'fetched_at', 'provider', 'raw_ref', 'checksum')})
            facts[-1]['payload_sha256'] = hashlib.sha256(row['payload'].encode()).hexdigest()
        files = _sources(repository, {row['raw_ref'] for row in rows})
        # prices() distinguishes future requested days even before any facts exist.
        future = bool(method == 'prices' and end and end > cutoff.astimezone(
            ZoneInfo('Asia/Shanghai')).date().isoformat())
        body = {'schema_version': 1, 'method': method,
                'query': {k: v for k, v in kwargs.items() if k != 'knowledge_cutoff'},
                'future_price': future, 'facts': sorted(facts, key=lambda row: row['id']), 'files': files}
        return {'state': 'READY', 'fingerprint': digest(body),
                'observation_count': len(rows), 'source_count': len(files), 'read_only': True}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {'state': 'CORRUPT', 'reason': 'STATEMENT_DEPENDENCY_UNAVAILABLE', 'read_only': True}
