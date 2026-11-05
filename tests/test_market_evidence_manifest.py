"""Public recursive evidence manifests must be complete, bounded and read-only."""

import hashlib
import socket

import pytest

from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService


def source(**extra):
    return {'provider': 'fixture', 'fetched_at': '2026-10-28T18:30:00+08:00', **extra}


@pytest.fixture
def facts(tmp_path):
    repo = MarketRepository(tmp_path/'facts')
    leaf = repo.save_raw(source(rows=[]))
    middle = repo.save_raw(source(dependencies=[leaf], rows=[]))
    ids = repo.put('calendar', [{'date': '2026-10-28', 'is_trading_day': True}],
                   source(dependencies=[middle]))
    snapshot = repo.freeze({'as_of': '2026-10-28'}, ids)
    return repo, leaf, middle, snapshot['snapshot_id']


def test_manifest_lists_snapshot_and_all_recursive_original_bytes_without_writes(facts, monkeypatch):
    repo, leaf, middle, identity = facts
    def no_network(*args, **kwargs):
        raise AssertionError('no network')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    before = {str(p): p.read_bytes() for p in repo.root.rglob('*') if p.is_file()}
    result = MarketDataService(repo.root).evidence_manifest([identity, identity])
    assert result['state'] == 'READY' and result['snapshot_ids'] == [identity]
    assert len(result['files']) == 4 and len(result['observation_ids']) == 1
    paths = {item['path'] for item in result['files']}
    assert leaf in paths and middle in paths
    for item in result['files']:
        assert hashlib.sha256((repo.root/item['path']).read_bytes()).hexdigest() == item['sha256']
    assert before == {str(p): p.read_bytes() for p in repo.root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('ids', ['a'*64, ['../escape'], [None], ['x'*64], ['a'*64]*1001])
def test_invalid_snapshot_ids_rejected_without_creating_root(tmp_path, ids):
    root = tmp_path/'absent'
    with pytest.raises(ValueError):
        MarketDataService(root).evidence_manifest(ids)
    assert not root.exists()


def test_empty_manifest_does_not_require_or_create_database(tmp_path):
    root = tmp_path/'empty'
    assert MarketDataService(root).evidence_manifest([])['files'] == []
    assert not root.exists()


def test_missing_or_tampered_recursive_source_refuses_manifest(facts):
    repo, leaf, _, identity = facts
    (repo.root/leaf).write_text('{"fake":true}')
    with pytest.raises(CorruptData):
        MarketDataService(repo.root).evidence_manifest([identity])


def test_symlink_escape_is_rejected(facts, tmp_path):
    repo, leaf, _, identity = facts
    outside = tmp_path/'outside.json'
    outside.write_bytes((repo.root/leaf).read_bytes())
    (repo.root/leaf).unlink()
    (repo.root/leaf).symlink_to(outside)
    with pytest.raises(ValueError):
        MarketDataService(repo.root).evidence_manifest([identity])


def test_mutated_index_is_rejected_before_export(facts):
    repo, _, _, identity = facts
    with repo.connection(write=True) as db:
        db.execute("UPDATE observations SET provider='tampered'")
    with pytest.raises(CorruptData):
        MarketDataService(repo.root).evidence_manifest([identity])


def test_independent_query_observations_export_without_snapshot(facts):
    repo, leaf, middle, identity = facts
    observation_ids = repo.read_snapshot(identity)['body']['observation_ids']
    result = MarketDataService(repo.root).evidence_manifest([], observation_ids=observation_ids)
    assert result['snapshot_ids'] == []
    assert result['observation_ids'] == observation_ids
    assert len(result['files']) == 3
    assert {leaf, middle} <= {item['path'] for item in result['files']}
    assert all(item['kind'] == 'source_object' for item in result['files'])


@pytest.mark.parametrize('ids', ['a'*64, ['../escape'], [None], ['x'*64]])
def test_query_observation_ids_are_validated_without_writes(tmp_path, ids):
    root = tmp_path/'missing'
    with pytest.raises(ValueError):
        MarketDataService(root).evidence_manifest([], observation_ids=ids)
    assert not root.exists()


def test_missing_query_observation_is_not_exported_as_empty_success(facts):
    repo, _, _, _ = facts
    with pytest.raises(ValueError):
        MarketDataService(repo.root).evidence_manifest([], observation_ids=['a'*64])
