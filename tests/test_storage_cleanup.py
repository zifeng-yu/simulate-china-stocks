import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from china_stock_sim.common.audit_archive import DIRECTORY, read_evidence

_SPEC = importlib.util.spec_from_file_location('storage_cleanup_tested',
    Path(__file__).resolve().parents[1] / 'scripts/storage_cleanup.py')
cleanup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cleanup)


def _fixture(root):
    raw = b'{"immutable":"' + b'evidence text ' * 1000 + b'"}\n'
    checksum = hashlib.sha256(raw).hexdigest()
    audit = root / DIRECTORY / f'{checksum}.evidence'
    audit.parent.mkdir(parents=True)
    audit.write_bytes(raw)
    unknown = audit.parent / 'unknown.bin'
    unknown.write_bytes(b'preserve unknown')
    backup_paths, manifests = [], []
    for name in ('daily-one', 'daily-two'):
        folder = root / 'data/backups/ledger' / name
        relative = f'data/plans/frozen/{checksum}.json'
        file = folder / 'evidence' / relative
        file.parent.mkdir(parents=True)
        file.write_bytes(raw)
        manifest = folder / 'manifest.json'
        manifest.write_text(json.dumps({'backup_id': name, 'state': 'COMPLETE',
            'evidence': {'files': [{'path': relative, 'sha256': checksum}]}}))
        backup_paths.append(file)
        manifests.append(manifest)
    cache = root / '.cache/uv'
    cache.mkdir(parents=True)
    (cache / 'rebuildable').write_bytes(b'cached wheel')
    venv = root / '.venv'
    venv.mkdir()
    (venv / 'python').write_bytes(b'keep environment')
    return raw, checksum, audit, unknown, backup_paths, manifests, cache


def test_plan_changes_nothing_and_reports_narrow_scope(tmp_path):
    raw, checksum, audit, unknown, paths, manifests, cache = _fixture(tmp_path)
    before = [p.read_bytes() for p in manifests]
    plan = cleanup.plan_cleanup(tmp_path)
    assert plan['read_only'] is True
    assert plan['state'] == 'PARTIAL'  # Unknown entries are preserved, not silently claimed covered.
    assert plan['summary']['audit_files'] == 1
    assert plan['summary']['backup_duplicate_groups'] == 1
    assert plan['summary']['backup_duplicate_logical_bytes'] == len(raw)
    assert plan['backup_duplicates'][0]['exclusive_physical_bytes'] is None
    assert not paths[0].samefile(paths[1])
    assert audit.read_bytes() == raw
    assert [p.read_bytes() for p in manifests] == before
    assert unknown.is_file() and cache.is_dir()
    assert not (tmp_path / 'data/normalized/evidence-objects').exists()


def test_explicit_apply_preserves_manifest_bytes_and_unknown_files(tmp_path):
    raw, checksum, _, unknown, paths, manifests, cache = _fixture(tmp_path)
    before = [p.read_bytes() for p in manifests]
    plan = cleanup.plan_cleanup(tmp_path)
    result = cleanup.apply_plan(tmp_path, plan, components=['audit', 'backups', 'cache'])
    assert result['state'] == 'COMPLETE'
    assert read_evidence(tmp_path, checksum) == raw
    assert paths[0].samefile(paths[1])
    assert paths[0].read_bytes() == raw
    assert [p.read_bytes() for p in manifests] == before
    assert unknown.read_bytes() == b'preserve unknown'
    assert not cache.exists()
    assert (tmp_path / '.venv/python').read_bytes() == b'keep environment'
    assert result['backups'][0]['exclusive_bytes_reclaimed'] is None
    assert cleanup.apply_plan(tmp_path, plan, components=['audit', 'backups', 'cache'])['state'] == 'COMPLETE'


def test_changed_backup_manifest_fails_before_any_action(tmp_path):
    raw, _, audit, _, _, manifests, _ = _fixture(tmp_path)
    plan = cleanup.plan_cleanup(tmp_path)
    manifests[0].write_text('{}')
    with pytest.raises(cleanup.CleanupError, match='MANIFEST_CHANGED'):
        cleanup.apply_plan(tmp_path, plan, components=['audit', 'backups'])
    assert audit.read_bytes() == raw


def test_changed_backup_bytes_rejected_without_linking(tmp_path):
    _, _, _, _, paths, _, _ = _fixture(tmp_path)
    plan = cleanup.plan_cleanup(tmp_path)
    paths[1].write_bytes(b'new different bytes')
    with pytest.raises(cleanup.CleanupError, match='EVIDENCE_CHANGED'):
        cleanup.apply_plan(tmp_path, plan, components=['backups'])
    assert not paths[0].samefile(paths[1])


def test_cache_changes_and_symlinks_are_not_deleted(tmp_path):
    _, _, _, _, _, _, cache = _fixture(tmp_path)
    plan = cleanup.plan_cleanup(tmp_path)
    (cache / 'new').write_bytes(b'new data after plan')
    with pytest.raises(cleanup.CleanupError, match='CACHE_CHANGED'):
        cleanup.apply_plan(tmp_path, plan, components=['cache'])
    assert cache.is_dir()
    (cache / 'link').symlink_to(tmp_path / '.venv')
    second = cleanup.plan_cleanup(tmp_path)
    assert second['cache'] == []
    assert any(x['reason'] == 'CACHE_UNKNOWN_ENTRY' for x in second['skipped'])


def test_unreviewed_plan_change_is_rejected(tmp_path):
    _fixture(tmp_path)
    plan = cleanup.plan_cleanup(tmp_path)
    plan['cache'][0]['path'] = '.venv'
    with pytest.raises(cleanup.CleanupError, match='PLAN_INVALID'):
        cleanup.apply_plan(tmp_path, plan, components=['cache'])


def test_backup_hardlink_to_unknown_path_is_rejected(tmp_path):
    _, _, _, _, paths, _, _ = _fixture(tmp_path)
    os.link(paths[0], tmp_path / 'unknown-link')
    plan = cleanup.plan_cleanup(tmp_path)
    with pytest.raises(cleanup.CleanupError, match='UNKNOWN_HARDLINK'):
        cleanup.apply_plan(tmp_path, plan, components=['backups'])


def test_already_shared_backup_is_reported_without_reclaim_claim(tmp_path):
    _, _, _, _, paths, _, _ = _fixture(tmp_path)
    paths[1].unlink()
    os.link(paths[0], paths[1])
    plan = cleanup.plan_cleanup(tmp_path)
    group = plan['backup_duplicates'][0]
    assert group['already_shared'] is True
    assert group['inode_unique_allocated_bytes'] == paths[0].stat().st_blocks * 512
    assert group['exclusive_physical_bytes'] is None


def test_internal_cache_alias_is_not_followed_outside_the_cache(tmp_path):
    _, _, _, _, _, _, cache = _fixture(tmp_path)
    (cache / 'internal-alias').symlink_to(cache / 'rebuildable')
    plan = cleanup.plan_cleanup(tmp_path)
    assert len(plan['cache']) == 1
    cleanup.apply_plan(tmp_path, plan, components=['cache'])
    assert not cache.exists()
    assert (tmp_path / '.venv/python').is_file()


def test_delta_plan_only_covers_new_audit_artifacts(tmp_path):
    _, _, _, _, _, _, _ = _fixture(tmp_path)
    prior = cleanup.plan_cleanup(tmp_path)
    raw = b'new independently preserved audit artifact'
    checksum = hashlib.sha256(raw).hexdigest()
    (tmp_path / DIRECTORY / f'{checksum}.evidence').write_bytes(raw)
    delta = cleanup.plan_cleanup(tmp_path, previous_plan=prior)
    assert delta['previous_plan_sha256'] == prior['plan_sha256']
    assert delta['deferred_audit_entries'] == 1
    assert len(delta['audit']) == 1 and delta['audit'][0]['sha256'] == checksum
    assert delta['backup_duplicates'] == [] and delta['cache'] == []
    cleanup.apply_plan(tmp_path, prior, components=['audit'])
    cleanup.apply_plan(tmp_path, delta, components=['audit'])
    assert read_evidence(tmp_path, checksum) == raw
