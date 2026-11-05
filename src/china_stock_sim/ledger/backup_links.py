"""Short-lived, hash-guarded identity changes made by one backup writer.

This is not a persistent trust list. A missed or interrupted transition leaves
ordinary verification cold; it never authorizes ignoring a filesystem change.
"""

import os
import stat
from pathlib import Path


def owned_regular(root, relative):
    root = Path(root).resolve()
    value = Path(relative)
    if value.is_absolute() or not value.parts or '..' in value.parts:
        raise ValueError('备份证据路径无效')
    current = root
    for part in value.parts:
        current /= part
        if current.is_symlink():
            raise ValueError('备份证据路径不可含符号链接')
    return current


def identity(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('备份证据必须是常规文件')
    return {'stamp': [value.st_dev, value.st_ino, value.st_size,
                      value.st_mtime_ns, value.st_ctime_ns],
            'mode': stat.S_IMODE(value.st_mode), 'nlink': value.st_nlink}


def permitted(before, after, delta):
    return (before['stamp'][:4] == after['stamp'][:4]
            and before['mode'] == after['mode']
            and after['nlink'] == before['nlink'] + delta)


class VerifiedLinkBatch:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.records = {}
        self.invalid = set()

    def linked(self, source, before, after, checksum):
        """Called only after the writer verified both bytes and exact post-stat."""
        key = tuple(before['stamp'][:2])
        if key in self.invalid or not permitted(before, after, 1):
            self.invalid.add(key)
            return
        previous = self.records.get(key)
        if previous is not None:
            if previous['new'] != before or previous['sha256'] != checksum:
                self.invalid.add(key)
                return
            previous['new'] = after
            previous['verified'].append(after)
        else:
            self.records[key] = {'source': str(source.relative_to(self.root)),
                                 'old': before, 'new': after, 'sha256': checksum,
                                 'verified': [before, after]}

    def remove_backup(self, folder, hash_file):
        """Remove the already-selected expired point, tracking surviving aliases."""
        folder = Path(folder)
        base = self.root / 'data/backups/ledger'
        if (folder.parent != base or not folder.name.startswith('daily-')
                or folder.is_symlink()):
            raise ValueError('轮换目标不是受控daily备份目录')
        grouped, ordinary, directories = {}, [], []
        for parent, dirs, files in os.walk(folder, followlinks=False):
            current = Path(parent)
            directories.append(current)
            for name in list(dirs):
                child = current / name
                if child.is_symlink():
                    ordinary.append(child)
                    dirs.remove(name)
            for name in files:
                child = current / name
                info = child.lstat()
                key = (info.st_dev, info.st_ino)
                if stat.S_ISREG(info.st_mode) and key in self.records and key not in self.invalid:
                    grouped.setdefault(key, []).append(child)
                else:
                    ordinary.append(child)
        for key, paths in grouped.items():
            record = self.records[key]
            anchor = owned_regular(self.root, record['source'])
            before = identity(anchor)
            if before != record['new'] or any(identity(path) != before for path in paths):
                self.invalid.add(key)
                raise ValueError('轮换前共享原件身份发生非受控变化')
            for path in paths:
                if identity(path) != identity(anchor):
                    self.invalid.add(key)
                    raise ValueError('轮换期间共享原件发生变化')
                path.unlink()
            after = identity(anchor)
            if (not permitted(before, after, -len(paths))
                    or hash_file(anchor) != record['sha256'] or identity(anchor) != after):
                self.invalid.add(key)
                raise ValueError('轮换后共享原件内容或身份变化')
            record['new'] = after
            record['verified'].append(after)
        for path in ordinary:
            path.unlink()
        for path in reversed(directories):
            path.rmdir()

    def transitions(self):
        result = []
        for key, record in self.records.items():
            if key not in self.invalid:
                try:
                    if identity(owned_regular(self.root, record['source'])) == record['new']:
                        for old in record['verified']:
                            if old != record['new']:
                                result.append({key: value for key, value in
                                    {**record, 'old': old}.items() if key != 'verified'})
                except (OSError, ValueError):
                    pass
        return result

    def current_guard(self, path, verified, checksum):
        """A new backup proof may use only a guard verified while hashing bytes."""
        current = identity(path)
        if current == verified:
            return current
        key = tuple(verified['stamp'][:2])
        record = self.records.get(key)
        if (key in self.invalid or record is None or record['sha256'] != checksum
                or verified not in record['verified'] or current != record['new']
                or identity(owned_regular(self.root, record['source'])) != record['new']):
            raise ValueError('备份成员在摘要核验后发生非受控变化')
        return current
