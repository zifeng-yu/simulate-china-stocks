"""Fixed, auditable executable names for the three existing launchd roles."""

import os
import stat
from pathlib import Path

_NAMES = {'workflow': 'A股模拟-业务调度', 'homepage': 'A股模拟-首页刷新',
          'upload': 'A股模拟-云端上传'}
_BODY = b'''#!/bin/sh
set -eu
launcher_dir=$(CDPATH= cd -P -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
project_root=$(CDPATH= cd -P -- "$launcher_dir/../../.." && pwd -P)
exec "$project_root/.venv/bin/python" "$@"
'''


def launcher_bytes(role):
    """Return the exact versioned shell bytes; no project path or secrets embedded."""
    if not isinstance(role, str) or role not in _NAMES:
        raise ValueError('UNKNOWN_LAUNCHER_ROLE')
    return _BODY


def launcher_path(root, role):
    """Read-only path construction; a preview does not require an installed file."""
    launcher_bytes(role)
    return Path(root).resolve() / 'deploy' / 'launchd' / 'bin' / _NAMES[role]


def validate_launcher(root, role):
    """Accept only the fixed regular executable and exact controlled shell bytes.

    This validates the launcher only. Installers independently retain their
    existing Python check, including a virtualenv's legitimate interpreter link.
    """
    expected = launcher_bytes(role)
    root = Path(root).resolve()
    path = launcher_path(root, role)
    try:
        if any(part.is_symlink() for part in (path, *path.parents)
               if part.is_relative_to(root)):
            raise ValueError('LAUNCHER_INVALID')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != len(expected):
                raise ValueError('LAUNCHER_INVALID')
            actual = stream.read(len(expected) + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if (actual != expected or not os.access(path, os.X_OK)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise ValueError('LAUNCHER_INVALID')
        return path
    except OSError:
        raise ValueError('LAUNCHER_INVALID') from None
