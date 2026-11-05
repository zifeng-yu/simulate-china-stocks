"""Plan first; explicitly apply only verified archive/dedup/cache actions in this project."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from china_stock_sim.common.audit_archive import DIRECTORY, archive_file, verify_evidence

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_CACHE = ('.cache/uv', '.pytest_cache', '.ruff_cache')


class CleanupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _path(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise CleanupError('CLEANUP_UNSAFE_PATH')
    current = Path(root).resolve()
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise CleanupError('CLEANUP_UNSAFE_PATH')
    if not current.resolve().is_relative_to(Path(root).resolve()):
        raise CleanupError('CLEANUP_UNSAFE_PATH')
    return current


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _stat(path):
    saved = path.lstat()
    return {'bytes': saved.st_size, 'allocated_bytes': saved.st_blocks * 512,
            'device': saved.st_dev, 'inode': saved.st_ino, 'mtime_ns': saved.st_mtime_ns,
            'links': saved.st_nlink, 'mode': stat.S_IMODE(saved.st_mode)}


def _cache_inventory(root, relative):
    directory = _path(root, relative)
    if not directory.is_dir():
        return None
    entries, logical, allocated = [], 0, 0
    for folder, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise CleanupError('CACHE_UNKNOWN_ENTRY')
            elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise CleanupError('CACHE_UNKNOWN_ENTRY')
            entries.append([str(path.relative_to(directory)), info.st_mode,
                            info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino])
            logical += info.st_size if stat.S_ISREG(info.st_mode) else 0
            allocated += info.st_blocks * 512
    encoded = json.dumps(sorted(entries), separators=(',', ':')).encode()
    return {'path': relative, 'entry_count': len(entries), 'bytes': logical,
            'allocated_bytes': allocated, 'tree_fingerprint': hashlib.sha256(encoded).hexdigest(),
            'root_stat': _stat(directory), 'rebuildable': True}


def _backup_candidates(root):
    groups, manifests, skipped = defaultdict(list), [], []
    folder = _path(root, 'data/backups/ledger')
    if not folder.is_dir():
        return [], [], []
    for manifest in sorted(folder.glob('daily-*/manifest.json')):
        relative = str(manifest.relative_to(root))
        try:
            manifest = _path(root, relative)
            if manifest.stat().st_size > 8 * 1024 * 1024:
                raise CleanupError('BACKUP_MANIFEST_TOO_LARGE')
            value = json.loads(manifest.read_bytes())
            if value.get('state') != 'COMPLETE':
                continue
            if value.get('backup_id') != manifest.parent.name:
                raise CleanupError('BACKUP_IDENTITY_INVALID')
            pending = []
            for item in value.get('evidence', {}).get('files', []):
                original, expected = item.get('path'), item.get('sha256')
                if not isinstance(original, str) or not original.startswith('data/plans/'):
                    continue
                if not isinstance(expected, str) or not _HASH.fullmatch(expected):
                    raise CleanupError('BACKUP_REFERENCE_INVALID')
                path = _path(root, str((manifest.parent / 'evidence' / original).relative_to(root)))
                if not stat.S_ISREG(path.lstat().st_mode) or _hash(path) != expected:
                    raise CleanupError('BACKUP_EVIDENCE_HASH_MISMATCH')
                pending.append((expected, {'path': str(path.relative_to(root)), **_stat(path)}))
            manifests.append({'path': relative, 'sha256': _hash(manifest)})
            for expected, item in pending:
                groups[expected].append(item)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            skipped.append({'path': relative, 'reason': getattr(exc, 'code', 'BACKUP_UNAVAILABLE')})
    result = []
    for checksum, files in sorted(groups.items()):
        unique = {r['path']: r for r in files}
        files = sorted(unique.values(), key=lambda r: r['path'])
        if len(files) < 2:
            continue
        if len({r['bytes'] for r in files}) != 1:
            skipped.append({'sha256': checksum, 'reason': 'BACKUP_SIZE_CONFLICT'})
            continue
        inodes = {(r['device'], r['inode']): r for r in files}
        result.append({'sha256': checksum, 'files': files,
            'logical_path_bytes': sum(r['bytes'] for r in files),
            'inode_unique_allocated_bytes': sum(r['allocated_bytes'] for r in inodes.values()),
            'duplicate_logical_bytes': files[0]['bytes'] * (len(files) - 1),
            'already_shared': len(inodes) == 1, 'exclusive_physical_bytes': None})
    return result, manifests, skipped


def plan_cleanup(root, *, previous_plan=None):
    root = Path(root).resolve()
    audit, skipped = [], []
    prior_paths = set()
    if previous_plan is not None:
        _check_manifest(root, previous_plan)
        prior_paths = {item['path'] for item in previous_plan['audit']}
    folder = _path(root, DIRECTORY)
    if folder.is_dir():
        for file in sorted(folder.iterdir()):
            relative = str(file.relative_to(root))
            if relative in prior_paths:
                # The old reviewed plan remains authoritative for these bytes;
                # its apply revalidates them. This delta neither deletes nor
                # claims a second verification of unchanged historical entries.
                continue
            if (not file.name.endswith('.evidence') or
                    not _HASH.fullmatch(file.name.removesuffix('.evidence'))):
                skipped.append({'path': relative, 'reason': 'UNKNOWN_AUDIT_FILE'})
                continue
            try:
                audit.append(archive_file(root, relative, apply=False))
            except (OSError, ValueError) as exc:
                skipped.append({'path': relative, 'reason': getattr(exc, 'code', 'AUDIT_UNAVAILABLE')})
    backups, manifests, backup_skipped = ([], [], []) if previous_plan is not None else _backup_candidates(root)
    skipped.extend(backup_skipped)
    cache = []
    for relative in (() if previous_plan is not None else _CACHE):
        try:
            item = _cache_inventory(root, relative)
            if item is not None:
                cache.append(item)
        except (OSError, ValueError) as exc:
            skipped.append({'path': relative, 'reason': getattr(exc, 'code', 'CACHE_UNAVAILABLE')})
    plan = {'schema_version': 1, 'state': 'READY' if not skipped else 'PARTIAL',
        'read_only': True, 'created_at': datetime.now().astimezone().isoformat(),
        'audit': audit, 'backup_duplicates': backups, 'backup_manifests': manifests,
        'cache': cache, 'skipped': skipped,
        'previous_plan_sha256': None if previous_plan is None else previous_plan['plan_sha256'],
        'deferred_audit_entries': len(prior_paths),
        'scope': {'archive': DIRECTORY, 'backup': 'COMPLETE daily evidence/data/plans only',
                  'cache': list(_CACHE), 'runtime_evidence': 'EXCLUDED',
                  'database': 'EXCLUDED', 'venv': 'EXCLUDED'},
        'measurement': 'Logical bytes and st_blocks*512; hardlinks counted per inode where stated; '
                       'APFS clone sharing/exclusive/reclaimable bytes are unknown.',
        'summary': {'audit_files': len(audit), 'audit_original_bytes': sum(r['bytes'] for r in audit),
            'audit_estimated_compressed_bytes': sum(r.get('estimated_compressed_bytes',
                r.get('object_ref', {}).get('_evidence_ref', {}).get('stored_bytes', 0)) for r in audit),
            'backup_duplicate_groups': len(backups),
            'backup_duplicate_logical_bytes': sum(r['duplicate_logical_bytes'] for r in backups),
            'cache_allocated_bytes': sum(r['allocated_bytes'] for r in cache)}}
    plan['plan_sha256'] = hashlib.sha256(json.dumps(plan, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return plan


def _check_manifest(root, plan):
    expected = plan.get('plan_sha256')
    body = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    actual = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    if expected != actual or plan.get('schema_version') != 1:
        raise CleanupError('CLEANUP_PLAN_INVALID')
    allowed = {}
    for item in plan['backup_manifests']:
        path = _path(root, item['path'])
        if _hash(path) != item['sha256'] or path.stat().st_size > 8 * 1024 * 1024:
            raise CleanupError('BACKUP_MANIFEST_CHANGED')
        manifest = json.loads(path.read_bytes())
        for entry in manifest.get('evidence', {}).get('files', []):
            original = entry.get('path')
            if isinstance(original, str) and original.startswith('data/plans/'):
                saved = str((path.parent / 'evidence' / original).relative_to(Path(root).resolve()))
                allowed[saved] = entry.get('sha256')
    for group in plan['backup_duplicates']:
        for item in group['files']:
            if allowed.get(item['path']) != group['sha256']:
                raise CleanupError('BACKUP_REFERENCE_UNREVIEWED')


def _deduplicate(root, group):
    files = group['files']
    checked = []
    for item in files:
        path = _path(root, item['path'])
        relative = path.relative_to(Path(root).resolve())
        parts = relative.parts
        if (len(parts) < 8 or parts[:3] != ('data', 'backups', 'ledger') or
                not parts[3].startswith('daily-') or parts[4:7] != ('evidence', 'data', 'plans')):
            raise CleanupError('BACKUP_PATH_UNSUPPORTED')
        if not stat.S_ISREG(path.lstat().st_mode) or _hash(path) != group['sha256']:
            raise CleanupError('BACKUP_EVIDENCE_CHANGED')
        current = _stat(path)
        if current['bytes'] != item['bytes']:
            raise CleanupError('BACKUP_EVIDENCE_CHANGED')
        checked.append((path, current))
    if len({r['device'] for _, r in checked}) != 1:
        raise CleanupError('BACKUP_CROSS_DEVICE')
    for _, item in checked:
        known_links = sum(other['device'] == item['device'] and other['inode'] == item['inode']
                          for _, other in checked)
        if item['links'] > known_links:
            raise CleanupError('BACKUP_UNKNOWN_HARDLINK')
    anchor, _ = checked[0]
    anchor.chmod(0o444)
    changed = 0
    for path, _ in checked[1:]:
        if path.samefile(anchor):
            continue
        descriptor, temporary = tempfile.mkstemp(prefix='.dedup-', dir=path.parent)
        os.close(descriptor)
        os.unlink(temporary)
        try:
            os.link(anchor, temporary)
            if _hash(path) != group['sha256']:
                raise CleanupError('BACKUP_EVIDENCE_CHANGED')
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            changed += 1
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    return {'sha256': group['sha256'], 'changed_paths': changed,
            'verified_same_inode': all(path.samefile(anchor) for path, _ in checked),
            'exclusive_bytes_reclaimed': None}


def apply_plan(root, plan, *, components):
    """Explicit operator action. Recheck every selected object before destructive changes."""
    if not components or set(components) - {'audit', 'backups', 'cache'}:
        raise CleanupError('CLEANUP_COMPONENT_INVALID')
    _check_manifest(root, plan)
    results = {'state': 'COMPLETE', 'plan_sha256': plan['plan_sha256'], 'audit': [],
               'backups': [], 'cache': [], 'unknown_entries_touched': False}
    if 'audit' in components:
        for item in plan['audit']:
            verify_evidence(root, item['path'], item['sha256'])
            results['audit'].append(archive_file(root, item['path'], item['sha256'], apply=True))
    if 'backups' in components:
        for group in plan['backup_duplicates']:
            results['backups'].append(_deduplicate(root, group))
    if 'cache' in components:
        for item in plan['cache']:
            if item['path'] not in _CACHE:
                raise CleanupError('CACHE_PATH_UNSUPPORTED')
            current = _cache_inventory(root, item['path'])
            if current is None:
                results['cache'].append({'path': item['path'], 'state': 'ALREADY_ABSENT'})
                continue
            if current['tree_fingerprint'] != item['tree_fingerprint']:
                raise CleanupError('CACHE_CHANGED')
            shutil.rmtree(_path(root, item['path']))
            results['cache'].append({'path': item['path'], 'state': 'REMOVED_REBUILDABLE_CACHE'})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--plan', type=Path, help='Read an existing cleanup plan for explicit apply')
    parser.add_argument('--previous-plan', type=Path, help='Read-only audit delta; original plan remains required')
    parser.add_argument('--apply', choices=['audit', 'backups', 'cache'], nargs='+')
    parser.add_argument('--verify-evidence', help='Verify an original audit SHA or preserved_path')
    args = parser.parse_args()
    if args.verify_evidence:
        if args.apply or args.plan or args.previous_plan:
            parser.error('verification cannot be combined with apply')
        result = verify_evidence(args.root, args.verify_evidence)
    elif args.apply:
        if args.plan is None or args.previous_plan:
            parser.error('apply requires the reviewed --plan')
        result = apply_plan(args.root, json.loads(args.plan.read_bytes()), components=args.apply)
    else:
        if args.plan:
            parser.error('--plan is only used with --apply')
        previous = json.loads(args.previous_plan.read_bytes()) if args.previous_plan else None
        result = plan_cleanup(args.root, previous_plan=previous)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
