"""Bounded SQL selection plus strict original calendar provenance."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from china_stock_sim.market import readiness_calendar as reader
from china_stock_sim.market.repository import CorruptData, MarketRepository


def put(repo, rows, day=1, **more):
    return repo.put('calendar', rows, {'provider': 'isolated-calendar', 'adapter_version': 'test',
                    'fetched_at': f'2026-10-{day:02d}T18:10:00+08:00', 'rows': rows, **more})


def read(root):
    return reader.read_calendar(root, start='2026-10-29', end='2026-11-03',
                                knowledge_cutoff='2026-10-30T20:00:00+08:00')


def test_calendar_reads_only_latest_sql_selected_versions_and_verifies_sources(project, monkeypatch):
    repo = MarketRepository(project)
    rows = [{'date': '2026-10-30', 'is_trading_day': 1}]
    for i in range(1, 21):
        put(repo, rows, day=i)
    before = repo.path.read_bytes()
    original, seen = MarketRepository._decode, []

    def tracked(self, row, **kwargs):
        seen.append(row['available_at'])
        return original(self, row, **kwargs)

    monkeypatch.setattr(MarketRepository, '_decode', tracked)
    monkeypatch.setattr(MarketRepository, 'select', lambda *a, **k: pytest.fail('unbounded old select'))
    value = read(project)
    assert value.ready and value.data == rows
    assert seen == ['2026-10-20T10:10:00+00:00']
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('damage', ['raw', 'payload', 'provider'])
def test_selected_source_and_index_tampering_are_rejected(project, damage):
    repo = MarketRepository(project)
    put(repo, [{'date': '2026-10-30', 'is_trading_day': 1}])
    with sqlite3.connect(repo.path) as db:
        if damage == 'raw':
            path = project / db.execute('SELECT raw_ref FROM observations').fetchone()[0]
            body = json.loads(path.read_bytes())
            body['rows'][0]['is_trading_day'] = 0
            path.write_text(json.dumps(body))
        elif damage == 'payload':
            db.execute("UPDATE observations SET payload=json_set(payload,'$.is_trading_day',0)")
        else:
            db.execute("UPDATE observations SET provider='forged'")
    with pytest.raises(CorruptData):
        read(project)


def test_raw_byte_budget_and_index_payload_budget_are_checked_before_decode(project, monkeypatch):
    repo = MarketRepository(project)
    put(repo, [{'date': '2026-10-30', 'is_trading_day': 1}], padding='x' * 100)
    monkeypatch.setattr(reader, 'MAX_RAW_BYTES', 64)
    monkeypatch.setattr(MarketRepository, '_decode', lambda *a, **k: pytest.fail('budget bypass'))
    with pytest.raises(CorruptData, match='SOURCE_LIMIT'):
        read(project)
    monkeypatch.setattr(reader, 'MAX_RAW_BYTES', 2 * 1024 * 1024)
    with sqlite3.connect(repo.path) as db:
        db.execute('UPDATE observations SET payload=?', ('x' * 4097,))
    with pytest.raises(CorruptData, match='INDEX_LIMIT'):
        read(project)


def test_raw_dependency_closure_has_size_and_count_bounds(project, monkeypatch):
    repo = MarketRepository(project)
    dependency = repo.save_raw({'provider': 'calendar-support', 'rows': [],
                               'fetched_at': '2026-10-01T18:00:00+08:00'})
    put(repo, [{'date': '2026-10-30', 'is_trading_day': 1}], dependencies=[dependency])
    assert read(project).ready
    monkeypatch.setattr(reader, 'MAX_SOURCES', 1)
    with pytest.raises(CorruptData, match='SOURCE_LIMIT'):
        read(project)


def test_missing_days_are_not_synthesized_and_future_versions_not_selected(project):
    repo = MarketRepository(project)
    put(repo, [{'date': '2026-10-30', 'is_trading_day': 1}])
    future = {'provider': 'isolated-calendar', 'adapter_version': 'test',
              'fetched_at': '2026-10-30T13:00:00+00:00',
              'rows': [{'date': '2026-10-30', 'is_trading_day': 0}]}
    repo.put('calendar', future['rows'], future)
    assert read(project).data == [{'date': '2026-10-30', 'is_trading_day': 1}]
    with pytest.raises(ValueError, match='RANGE'):
        reader.read_calendar(project, start='2020-01-01', end='2026-11-03',
                             knowledge_cutoff='2026-10-30T20:00:00+08:00')


def test_source_symlink_rejected_before_read(project, tmp_path):
    repo = MarketRepository(project)
    put(repo, [{'date': '2026-10-30', 'is_trading_day': 1}])
    with sqlite3.connect(repo.path) as db:
        path = project / db.execute('SELECT raw_ref FROM observations').fetchone()[0]
    outside = tmp_path / 'outside.json'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises((CorruptData, ValueError)):
        read(project)


def test_sql_query_plan_does_not_fetch_historical_payloads(project):
    repo = MarketRepository(project)
    rows = [{'date': (datetime(2026, 10, 29, tzinfo=timezone.utc) + timedelta(days=i)).date().isoformat(),
             'is_trading_day': 1} for i in range(6)]
    put(repo, rows)
    result = read(project)
    assert len(result.data) == 6 and len(result.observation_ids) == 6
