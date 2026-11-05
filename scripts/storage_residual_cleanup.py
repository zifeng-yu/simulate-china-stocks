"""Explicit, reviewed legacy-backup wrappers and redundant staging-link cleanup."""

import argparse
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from china_stock_sim.common.evidence_objects import is_reference, iter_bytes
from china_stock_sim.ledger.document_objects import FORMAT
from china_stock_sim.ledger.kernel import ledger_lock
from china_stock_sim.ledger.storage_promotion import _digest, _hash, _json, _path, _sync, _write
from china_stock_sim.scheduler.workspace import writer_lock


class ResidualCleanupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise ResidualCleanupError(code)


def stamp(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        fail('RESIDUAL_NOT_REGULAR')
    return {'device': info.st_dev, 'inode': info.st_ino, 'bytes': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'mode': stat.S_IMODE(info.st_mode)}


def canonical_raw_hashes(chunks):
    """Original document writer hashes canonical bytes, then stores one final LF."""
    raw, logical, pending = hashlib.sha256(), hashlib.sha256(), b''
    for chunk in chunks:
        if not chunk:
            continue
        raw.update(chunk)
        if pending:
            logical.update(pending)
        logical.update(chunk[:-1])
        pending = chunk[-1:]
    if pending != b'\n':
        logical.update(pending)
    return raw.hexdigest(), logical.hexdigest()


def _file_hashes(path):
    with path.open('rb') as stream:
        return canonical_raw_hashes(iter(lambda: stream.read(1024 * 1024), b''))


def _proof(root, item):
    path = _path(root, item['path'])
    if _hash(path) != item['sha256']:
        fail('RESIDUAL_PROOF_CHANGED')
    return _json(path)


def prepare_backups(root, *, review_path, compaction_receipt):
    root = Path(root).resolve()
    review, compaction = _json(_path(root, review_path)), _json(_path(root, compaction_receipt))
    if compaction.get('state') != 'COMPLETE':
        fail('RESIDUAL_COMPACTION_NOT_COMPLETE')
    originals = {r['original_sha256']: r for r in compaction['results']}
    manifests = []
    members = []
    for backup in review['old_daily_backups']:
        relative = f'data/backups/ledger/{backup["backup_id"]}/manifest.json'
        if relative != backup['manifest_path']:
            fail('RESIDUAL_BACKUP_PATH_INVALID')
        path = _path(root, relative)
        manifest = _proof(root, {'path': relative, 'sha256': backup['manifest_sha256']})
        if manifest['state'] != 'COMPLETE' or manifest['backup_id'] != backup['backup_id']:
            fail('RESIDUAL_BACKUP_INCOMPLETE')
        manifests.append({'path': relative, 'sha256': _hash(path)})
        for item in manifest['evidence']['files']:
            if not item['path'].startswith('data/plans/'):
                continue
            original = originals.get(item['sha256'])
            if original is None or original['document']['path'] != item['path']:
                fail('RESIDUAL_UNMAPPED_BACKUP_DOCUMENT')
            expected = original['document']['content_hash']
            if item.get('content_hash', Path(item['path']).stem) != expected:
                fail('RESIDUAL_LOGICAL_HASH_MISMATCH')
            target = _path(root, str(Path(relative).parent / 'evidence' / item['path']))
            members.append({'backup_id': backup['backup_id'], 'item': item,
                'path': str(target.relative_to(root)), 'expected_stat': stamp(target),
                'document': original['document'], 'object_ref': original['object_ref'],
                'root_wrapper_sha256': original['wrapper_sha256']})
    result = {'schema_version': 1, 'state': 'PLANNED_READ_ONLY',
        'created_at': datetime.now().astimezone().isoformat(),
        'review': {'path': review_path, 'sha256': _hash(_path(root, review_path))},
        'compaction': {'path': compaction_receipt, 'sha256': _hash(_path(root, compaction_receipt))},
        'manifests': manifests, 'members': members,
        'database_scope': 'Excluded: retain both distinct original SQLite restorepoints',
        'new_CAS_bodies': False, 'manifest_bytes_changed': False}
    result['plan_sha256'] = _digest(result)
    return result


def _check_digest(plan):
    if plan.get('plan_sha256') != _digest({k: v for k, v in plan.items() if k != 'plan_sha256'}):
        fail('RESIDUAL_PLAN_CHANGED')


def _backup_plan(root, plan):
    _check_digest(plan)
    review = _proof(root, plan['review'])
    compaction = _proof(root, plan['compaction'])
    if compaction.get('state') != 'COMPLETE':
        fail('RESIDUAL_COMPACTION_NOT_COMPLETE')
    originals = {r['original_sha256']: r for r in compaction['results']}
    allowed_ids = {row['backup_id'] for row in review['old_daily_backups']}
    allowed_proofs = {row['manifest_path']: row['manifest_sha256']
                      for row in review['old_daily_backups']}
    expected = {}
    if (len(plan['manifests']) != len(allowed_ids)
            or {row['path']: row['sha256'] for row in plan['manifests']} != allowed_proofs):
        fail('RESIDUAL_MANIFEST_SET_INVALID')
    for proof in plan['manifests']:
        manifest = _proof(root, proof)
        identity = manifest['backup_id']
        if (manifest.get('state') != 'COMPLETE' or identity not in allowed_ids
                or proof['path'] != f'data/backups/ledger/{identity}/manifest.json'):
            fail('RESIDUAL_MANIFEST_SET_INVALID')
        for item in manifest['evidence']['files']:
            if item['path'].startswith('data/plans/'):
                original = originals[item['sha256']]
                key = f'data/backups/ledger/{identity}/evidence/{item["path"]}'
                expected[key] = {'backup_id': identity, 'item': item, 'path': key,
                    'document': original['document'], 'object_ref': original['object_ref'],
                    'root_wrapper_sha256': original['wrapper_sha256']}
    if len(plan['members']) != len(expected) or {r['path'] for r in plan['members']} != set(expected):
        fail('RESIDUAL_MEMBER_SET_INVALID')
    for row in plan['members']:
        if any(row.get(key) != value for key, value in expected[row['path']].items()):
            fail('RESIDUAL_MEMBER_MAPPING_CHANGED')


def _receipt(root, plan, kind):
    path = _path(root, f'data/maintenance/residual-{kind}-{plan["plan_sha256"]}.json')
    result = _json(path) if path.exists() else {'schema_version': 1,
        'plan_sha256': plan['plan_sha256'], 'phase': 'PREFLIGHT', 'completed': [], 'pending': None}
    if result.get('plan_sha256') != plan['plan_sha256']:
        fail('RESIDUAL_RECEIPT_CONFLICT')
    return path, result


def _publish(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.legacy-wrapper-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_backups(root, plan, *, maintenance_confirmed=False, checkpoint=None):
    from china_stock_sim.ledger.maintenance import backup_member_layout
    if maintenance_confirmed is not True:
        fail('RESIDUAL_MAINTENANCE_REQUIRED')
    root = Path(root).resolve()
    with writer_lock(SimpleNamespace(root=root)), ledger_lock(root):
        _backup_plan(root, plan)
        receipt_path, receipt = _receipt(root, plan, 'backups')
        allowed = {row['path'] for row in plan['members']}
        if (not set(receipt['completed']) <= allowed
                or receipt['pending'] not in allowed | {None}):
            fail('RESIDUAL_RECEIPT_INVALID')
        preflight, source_hashes, cas_hashes = [], {}, {}
        for row in plan['members']:
            reference = row['object_ref']
            if not is_reference(reference):
                fail('RESIDUAL_REFERENCE_INVALID')
            metadata = reference['_evidence_ref']
            expected = row['document']['content_hash']
            if (metadata['sha256'] != row['item']['sha256']
                    or _hash(_path(root, row['document']['path'])) != row['root_wrapper_sha256']):
                fail('RESIDUAL_ROOT_DOCUMENT_CHANGED')
            root_object = _path(root, metadata['path'])
            root_stamp = stamp(root_object)
            if root_stamp['mode'] & 0o222 or root_stamp['bytes'] != metadata['stored_bytes']:
                fail('RESIDUAL_OBJECT_NOT_IMMUTABLE')
            key = _digest(reference)
            if key not in cas_hashes:
                cas_hashes[key] = canonical_raw_hashes(iter_bytes(root, reference))
            if cas_hashes[key] != (metadata['sha256'], expected):
                fail('RESIDUAL_OBJECT_LOGICAL_HASH_MISMATCH')
            path = _path(root, row['path'])
            evidence_root = path.parents[len(Path(row['item']['path']).parts) - 1]
            destination = _path(evidence_root, metadata['path'])
            if destination.exists():
                destination_stamp = stamp(destination)
                if (destination_stamp['mode'] & 0o222
                        or destination_stamp['bytes'] != metadata['stored_bytes']
                        or not destination.samefile(root_object)):
                    fail('RESIDUAL_BACKUP_CAS_CONFLICT')
            wrapper = {'storage_format': FORMAT, 'logical_content_hash': expected, 'object_ref': reference}
            encoded_wrapper = (json.dumps(wrapper, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')) + '\n').encode()
            if path.stat().st_size <= 8192 and path.read_bytes() == encoded_wrapper:
                layout = backup_member_layout(evidence_root, row['item'], verify=True)
                if not layout['legacy_compressed']:
                    fail('RESIDUAL_REENTRY_LAYOUT_INVALID')
                preflight.append((row, root_object, root_stamp, evidence_root, path, None, encoded_wrapper))
                continue
            current = stamp(path)
            if current != row['expected_stat']:
                fail('RESIDUAL_ORIGINAL_CHANGED')
            inode_key = current['device'], current['inode'], current['bytes'], current['mtime_ns']
            if inode_key not in source_hashes:
                source_hashes[inode_key] = _file_hashes(path)
            if source_hashes[inode_key] != (metadata['sha256'], expected):
                fail('RESIDUAL_ORIGINAL_HASH_MISMATCH')
            preflight.append((row, root_object, root_stamp, evidence_root, path, current, encoded_wrapper))
        receipt.update(phase='APPLYING', preflight_members=len(preflight))
        _write(receipt_path, receipt)
        for row, source, source_stamp, evidence_root, path, previous, encoded_wrapper in preflight:
            if previous is not None:
                if stamp(source) != source_stamp or stamp(path) != previous:
                    fail('RESIDUAL_PREPUBLICATION_CHANGED')
                destination = _path(evidence_root, row['object_ref']['_evidence_ref']['path'])
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    os.link(source, destination)
                    _sync(destination.parent)
                elif (not destination.samefile(source)
                      or stamp(destination) != source_stamp):
                    fail('RESIDUAL_BACKUP_CAS_CONFLICT')
                receipt['pending'] = row['path']
                _write(receipt_path, receipt)
                _publish(path, encoded_wrapper)
                if checkpoint:
                    checkpoint('WRAPPER_PUBLISHED', row['path'])
            layout = backup_member_layout(evidence_root, row['item'], verify=False)
            if not layout['legacy_compressed']:
                fail('RESIDUAL_PUBLISHED_LAYOUT_INVALID')
            if row['path'] not in receipt['completed']:
                receipt['completed'].append(row['path'])
            receipt['pending'] = None
            _write(receipt_path, receipt)
        for manifest in plan['manifests']:
            _proof(root, manifest)
        receipt.update(phase='COMPLETE', finished_at=datetime.now().astimezone().isoformat(),
            manifest_bytes_changed=False, database_bytes_changed=False,
            compressed_existing_CAS_again=False,
            required_next_verification='Verify both original backup IDs and one real isolated restore with original raw SHA checks.')
        _write(receipt_path, receipt)
        return receipt


def unlink_stage(root, plan, *, maintenance_confirmed=False, checkpoint=None):
    if maintenance_confirmed is not True:
        fail('RESIDUAL_MAINTENANCE_REQUIRED')
    root = Path(root).resolve()
    with writer_lock(SimpleNamespace(root=root)), ledger_lock(root):
        _check_digest(plan)
        promotion = _proof(root, plan['promotion_proof'])
        staging = Path(promotion['staging_root'])
        if len(staging.parts) < 3 or staging.parts[:2] != ('data', 'maintenance'):
            fail('RESIDUAL_STAGE_ROOT_INVALID')
        if any(not is_reference(ref) for ref in promotion['references']):
            fail('RESIDUAL_REFERENCE_INVALID')
        if len({r['_evidence_ref']['path'] for r in promotion['references']}) != len(promotion['references']):
            fail('RESIDUAL_STAGE_SET_INVALID')
        expected = {str(Path(promotion['staging_root']) / r['_evidence_ref']['path']): r
                    for r in promotion['references']}
        if len(plan['files']) != len(expected) or {r['path'] for r in plan['files']} != set(expected):
            fail('RESIDUAL_STAGE_SET_INVALID')
        receipt_path, receipt = _receipt(root, plan, 'stage')
        completed = set(receipt['completed'])
        if not completed <= set(expected) or receipt['pending'] not in set(expected) | {None}:
            fail('RESIDUAL_RECEIPT_INVALID')
        # Every retained body and every remaining source is checked before any unlink.
        for row in plan['files']:
            if row['reference'] != expected[row['path']]:
                fail('RESIDUAL_STAGE_REFERENCE_CHANGED')
            reference = row['reference']['_evidence_ref']
            if row['retained_root_path'] != reference['path']:
                fail('RESIDUAL_STAGE_TARGET_CHANGED')
            source = _path(root, row['path'])
            retained = _path(root, row['retained_root_path'])
            if stamp(retained) != row['expected_stat'] or _hash(retained) != reference['stored_sha256']:
                fail('RESIDUAL_RETAINED_OBJECT_CHANGED')
            if row['path'] in completed:
                if source.exists():
                    fail('RESIDUAL_DELETED_PATH_REAPPEARED')
            elif source.exists():
                if not source.samefile(retained) or stamp(source) != row['expected_stat']:
                    fail('RESIDUAL_STAGE_LINK_CHANGED')
            elif receipt['pending'] != row['path']:
                fail('RESIDUAL_STAGE_UNEXPECTED_MISSING')
        receipt['phase'] = 'UNLINKING'
        _write(receipt_path, receipt)
        for row in plan['files']:
            if row['path'] in completed:
                continue
            source = _path(root, row['path'])
            retained = _path(root, row['retained_root_path'])
            receipt['pending'] = row['path']
            _write(receipt_path, receipt)
            if source.exists():
                if not source.samefile(retained) or stamp(source) != row['expected_stat']:
                    fail('RESIDUAL_STAGE_LINK_CHANGED')
                source.unlink()
                _sync(source.parent)
            if checkpoint:
                checkpoint('STAGE_UNLINKED', row['path'])
            receipt['completed'].append(row['path'])
            receipt['pending'] = None
            _write(receipt_path, receipt)
        receipt.update(phase='COMPLETE', finished_at=datetime.now().astimezone().isoformat(),
            removed_logical_bytes=sum(r['reference']['_evidence_ref']['stored_bytes'] for r in plan['files']),
            data_blocks_reclaimed_bytes=0, production_objects_deleted=False,
            boundary='Only redundant staging names removed; retained root/backup inodes persist.')
        _write(receipt_path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare-backups', 'archive-backups', 'unlink-stage'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--review')
    parser.add_argument('--compaction-receipt')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--maintenance-confirmed', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare-backups':
        if not args.review or not args.compaction_receipt:
            parser.error('prepare-backups requires review and compaction receipt')
        result = prepare_backups(args.root, review_path=args.review,
                                 compaction_receipt=args.compaction_receipt)
    else:
        if not args.plan or not args.maintenance_confirmed:
            parser.error('mutation requires reviewed plan and maintenance confirmation')
        method = archive_backups if args.action == 'archive-backups' else unlink_stage
        result = method(args.root, _json(args.plan), maintenance_confirmed=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
