import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from china_stock_sim.common.evidence_objects import iter_bytes, put_blob
from china_stock_sim.ledger.maintenance import backup_member_layout
from china_stock_sim.ledger.storage_promotion import _digest
from china_stock_sim.scheduler.workspace import writer_lock

_SPEC = importlib.util.spec_from_file_location('residual_cleanup_tested',
    Path(__file__).resolve().parents[1] / 'scripts/storage_residual_cleanup.py')
cleanup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cleanup)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
    return path


def seal(plan):
    plan['plan_sha256'] = _digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
    return plan


def fixture(root):
    originals, compacted, rows = [], [], []
    for number in range(2):
        raw = json.dumps({'number': number, 'evidence': 'test frozen evidence' * 1000},
                         sort_keys=True, separators=(',', ':')).encode() + b'\n'
        logical = sha(raw[:-1])
        relative = f'data/plans/frozen/{logical}.json'
        reference = put_blob(root, raw)
        wrapper = write(root, relative, {'storage_format': 'cas-json-v1',
                          'logical_content_hash': logical, 'object_ref': reference})
        document = {'path': relative, 'content_hash': logical}
        compacted.append({'state': 'APPLIED', 'document': document, 'object_ref': reference,
            'original_sha256': sha(raw), 'wrapper_sha256': sha(wrapper.read_bytes())})
        originals.append(raw)
    for number in range(2):
        backup_id = f'daily-test-{number}'
        parent = root / 'data/backups/ledger' / backup_id
        parent.mkdir(parents=True)
        (parent / 'ledger.sqlite3').write_bytes(b'unchanged database ' + bytes([number]))
        members = []
        for index, row in enumerate(compacted):
            path = parent / 'evidence' / row['document']['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            if number:
                os.link(root / rows[0]['evidence_root'] / row['document']['path'], path)
            else:
                path.write_bytes(originals[index])
            members.append({'path': row['document']['path'], 'sha256': row['original_sha256'],
                            'content_hash': row['document']['content_hash']})
        manifest = write(root, str((parent / 'manifest.json').relative_to(root)),
                         {'backup_id': backup_id, 'state': 'COMPLETE', 'evidence': {'files': members}})
        rows.append({'backup_id': backup_id, 'manifest_path': str(manifest.relative_to(root)),
                     'manifest_sha256': sha(manifest.read_bytes()),
                     'evidence_root': str((parent / 'evidence').relative_to(root))})
    write(root, 'logs/review.json', {'old_daily_backups': rows})
    write(root, 'logs/compaction.json', {'state': 'COMPLETE', 'results': compacted})
    plan = cleanup.prepare_backups(root, review_path='logs/review.json',
                                   compaction_receipt='logs/compaction.json')
    return plan, originals, compacted, rows


def stage_fixture(root):
    stage = 'data/maintenance/test-stage'
    refs, files = [], []
    for number in range(2):
        ref = put_blob(root, json.dumps({'row': number, 'body': 'content' * 1000}).encode())
        retained = root / ref['_evidence_ref']['path']
        relative = str(Path(stage) / ref['_evidence_ref']['path'])
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(retained, target)
        refs.append(ref)
        files.append({'path': relative, 'retained_root_path': ref['_evidence_ref']['path'],
                      'reference': ref, 'expected_stat': cleanup.stamp(target)})
    promotion = write(root, 'logs/promotion.json', {'staging_root': stage, 'references': refs})
    compact = root / stage / 'data/state/ledger.compact.sqlite3'
    compact.parent.mkdir(parents=True)
    compact.write_bytes(b'keep small index baseline')
    plan = seal({'schema_version': 1, 'files': files,
                 'promotion_proof': {'path': 'logs/promotion.json', 'sha256': sha(promotion.read_bytes())}})
    return plan, compact


def test_prepare_is_read_only_archive_preserves_raw_manifest_and_database(tmp_path, monkeypatch):
    plan, originals, compacted, backups = fixture(tmp_path)
    assert len(plan['members']) == 4
    assert not (tmp_path / 'data/maintenance').exists()
    before = {str(p.relative_to(tmp_path)): p.read_bytes()
              for p in (tmp_path / 'data/backups/ledger').glob('*/*') if p.is_file()}
    initial_objects = list((tmp_path / 'data/normalized/evidence-objects').rglob('*.zlib'))
    calls, original_hash = [], cleanup._file_hashes
    monkeypatch.setattr(cleanup, '_file_hashes', lambda path: (calls.append(path), original_hash(path))[1])
    result = cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True)
    assert result['phase'] == 'COMPLETE' and len(result['completed']) == 4
    assert len(calls) == 2  # Shared raw inode is streamed exactly once, not once per alias.
    assert initial_objects == list((tmp_path / 'data/normalized/evidence-objects').rglob('*.zlib'))
    for path, raw in before.items():
        assert (tmp_path / path).read_bytes() == raw
    for backup in backups:
        evidence = tmp_path / backup['evidence_root']
        for index, row in enumerate(compacted):
            item = {'path': row['document']['path'], 'content_hash': row['document']['content_hash'],
                    'sha256': row['original_sha256']}
            layout = backup_member_layout(evidence, item)
            assert layout['legacy_compressed']
            assert b''.join(iter_bytes(evidence, layout['object_ref'])) == originals[index]
            relative = row['object_ref']['_evidence_ref']['path']
            assert (evidence / relative).samefile(tmp_path / relative)
    assert cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'COMPLETE'


def test_archive_interrupted_after_wrapper_can_resume(tmp_path):
    plan, _, _, _ = fixture(tmp_path)
    def crash(event, path):
        assert event == 'WRAPPER_PUBLISHED'
        raise RuntimeError('simulated crash')
    with pytest.raises(RuntimeError, match='simulated crash'):
        cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True, checkpoint=crash)
    wrappers = [json.loads((tmp_path / row['path']).read_bytes()).get('storage_format') for row in plan['members']]
    assert wrappers.count('cas-json-v1') == 1
    assert cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'COMPLETE'


@pytest.mark.parametrize('damage', ['raw', 'root_wrapper', 'object', 'mutable_object', 'manifest',
                                  'review', 'compaction', 'destination', 'symlink'])
def test_archive_all_preflight_failures_preserve_first_raw(tmp_path, damage):
    plan, originals, compacted, backups = fixture(tmp_path)
    row = plan['members'][-1]
    target = tmp_path / row['path']
    if damage == 'raw':
        target.write_bytes(b'changed old raw')
    elif damage == 'root_wrapper':
        (tmp_path / row['document']['path']).write_bytes(b'changed wrapper')
    elif damage in {'object', 'mutable_object'}:
        target = tmp_path / row['object_ref']['_evidence_ref']['path']
        target.chmod(0o644)
        if damage == 'object':
            target.write_bytes(b'broken zlib body')
            target.chmod(0o444)
    elif damage == 'manifest':
        (tmp_path / backups[-1]['manifest_path']).write_text('{}')
    elif damage in {'review', 'compaction'}:
        (tmp_path / f'logs/{damage}.json').write_text('{}')
    elif damage == 'destination':
        target = tmp_path / backups[-1]['evidence_root'] / compacted[-1]['object_ref']['_evidence_ref']['path']
        target.parent.mkdir(parents=True)
        target.write_bytes((tmp_path / compacted[-1]['object_ref']['_evidence_ref']['path']).read_bytes())
        target.chmod(0o444)  # Same bytes are insufficient: this operation promises a shared inode.
    else:
        target.unlink()
        target.symlink_to(tmp_path / row['document']['path'])
    with pytest.raises((ValueError, OSError)):
        cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True)
    assert (tmp_path / plan['members'][0]['path']).read_bytes() == originals[0]
    assert not (tmp_path / 'data/maintenance').exists()


def test_manifest_set_and_member_set_are_exact(tmp_path):
    plan, _, _, _ = fixture(tmp_path)
    plan['manifests'][1] = dict(plan['manifests'][0])
    seal(plan)
    with pytest.raises(cleanup.ResidualCleanupError, match='MANIFEST_SET'):
        cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True)


def test_changed_plan_without_reseal_is_rejected(tmp_path):
    plan, _, _, _ = fixture(tmp_path)
    plan['members'].pop()
    with pytest.raises(cleanup.ResidualCleanupError, match='PLAN_CHANGED'):
        cleanup.archive_backups(tmp_path, plan, maintenance_confirmed=True)


def test_unmapped_content_hash_is_rejected_before_plan(tmp_path):
    _, _, _, backups = fixture(tmp_path)
    path = tmp_path / backups[-1]['manifest_path']
    manifest = json.loads(path.read_bytes())
    manifest['evidence']['files'][-1]['content_hash'] = '0' * 64
    write(tmp_path, backups[-1]['manifest_path'], manifest)
    backups[-1]['manifest_sha256'] = sha(path.read_bytes())
    write(tmp_path, 'logs/review.json', {'old_daily_backups': backups})
    with pytest.raises(cleanup.ResidualCleanupError, match='LOGICAL_HASH'):
        cleanup.prepare_backups(tmp_path, review_path='logs/review.json', compaction_receipt='logs/compaction.json')


@pytest.mark.parametrize('chunks', [[b'one\n'], [b'o', b'n', b'e', b'\n'], [b'one'], [b''],
                                   [b'a\nb\n'], [b'a', b'', b'\n', b'']])
def test_raw_logical_hash_stream_boundary(chunks):
    raw = b''.join(chunks)
    assert cleanup.canonical_raw_hashes(chunks) == (sha(raw), sha(raw.removesuffix(b'\n')))


def test_stage_unlinks_only_fixed_shared_names_and_keeps_bodies(tmp_path):
    plan, compact = stage_fixture(tmp_path)
    result = cleanup.unlink_stage(tmp_path, plan, maintenance_confirmed=True)
    assert result['phase'] == 'COMPLETE' and result['data_blocks_reclaimed_bytes'] == 0
    assert compact.read_bytes() == b'keep small index baseline'
    assert not any((tmp_path / row['path']).exists() for row in plan['files'])
    assert all((tmp_path / row['retained_root_path']).exists() for row in plan['files'])
    assert cleanup.unlink_stage(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'COMPLETE'


@pytest.mark.parametrize('damage', ['same_bytes_new_inode', 'raw_body', 'missing', 'proof',
                                  'set', 'reference', 'target', 'symlink'])
def test_stage_preflights_all_before_first_unlink(tmp_path, damage):
    plan, _ = stage_fixture(tmp_path)
    row = plan['files'][-1]
    target = tmp_path / row['path']
    if damage == 'same_bytes_new_inode':
        raw = target.read_bytes()
        target.unlink()
        target.write_bytes(raw)
        target.chmod(0o444)
    elif damage == 'raw_body':
        target.chmod(0o644)
        target.write_bytes(b'changed shared body')
    elif damage == 'missing':
        target.unlink()
    elif damage == 'proof':
        (tmp_path / 'logs/promotion.json').write_text('{}')
    elif damage == 'set':
        plan['files'].pop()
        seal(plan)
    elif damage == 'reference':
        row['reference']['_evidence_ref']['stored_sha256'] = '0' * 64
        seal(plan)
    elif damage == 'target':
        row['retained_root_path'] = 'data/state/ledger.sqlite3'
        seal(plan)
    else:
        target.unlink()
        target.symlink_to(tmp_path / row['retained_root_path'])
    with pytest.raises((ValueError, OSError)):
        cleanup.unlink_stage(tmp_path, plan, maintenance_confirmed=True)
    assert (tmp_path / plan['files'][0]['path']).exists()


def test_stage_pending_unlink_resumes_after_crash(tmp_path):
    plan, _ = stage_fixture(tmp_path)
    def crash(event, path):
        assert event == 'STAGE_UNLINKED'
        raise RuntimeError('simulated crash')
    with pytest.raises(RuntimeError, match='simulated crash'):
        cleanup.unlink_stage(tmp_path, plan, maintenance_confirmed=True, checkpoint=crash)
    assert not (tmp_path / plan['files'][0]['path']).exists()
    assert (tmp_path / plan['files'][1]['path']).exists()
    assert cleanup.unlink_stage(tmp_path, plan, maintenance_confirmed=True)['phase'] == 'COMPLETE'


@pytest.mark.parametrize('kind', ['archive', 'stage'])
def test_explicit_maintenance_and_writer_lock_required(tmp_path, kind):
    plan = fixture(tmp_path)[0] if kind == 'archive' else stage_fixture(tmp_path)[0]
    method = cleanup.archive_backups if kind == 'archive' else cleanup.unlink_stage
    with pytest.raises(cleanup.ResidualCleanupError, match='MAINTENANCE_REQUIRED'):
        method(tmp_path, plan)
    with writer_lock(SimpleNamespace(root=tmp_path)), pytest.raises(RuntimeError, match='已有进程'):
        method(tmp_path, plan, maintenance_confirmed=True)
