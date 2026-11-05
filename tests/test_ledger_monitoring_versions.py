"""The lightweight monitor supports both the legacy and compact ledger layouts."""

import sqlite3

import pytest

from china_stock_sim.ledger.monitoring import read_runtime_facts
from china_stock_sim.ledger.store import initialize


@pytest.mark.parametrize('version', [3, 4])
def test_monitor_supports_schema3_and_schema4_without_writing(settings, version):
    initialize(settings, recorded_at='2026-11-03T12:00:00+08:00')
    with sqlite3.connect(settings.database) as db:
        db.execute(f'PRAGMA user_version={version}')
    before = settings.database.read_bytes()
    result = read_runtime_facts(settings, ['2026-11-02'])
    assert result['state'] == 'READY' and result['schema_version'] == version
    assert len(result['accounts']) == 3
    assert settings.database.read_bytes() == before
