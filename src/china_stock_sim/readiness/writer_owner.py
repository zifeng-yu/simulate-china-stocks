"""Best-effort owner observation published only by an acquired workspace writer.

Failure of this optional metadata never changes the writer's financial result or
flock semantics. Readers cannot manufacture ownership by opening the lock file.
"""

import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from china_stock_sim.scheduler.process import probe_process

OWNER_PATH = 'data/state/writer-owner.json'
MAX_OWNER_BYTES = 2048


def publish_owner(root, lock_path, descriptor):
    token, temporary = uuid4().hex, None
    try:
        item = os.fstat(descriptor)
        current = Path(lock_path).lstat()
        if (item.st_dev, item.st_ino) != (current.st_dev, current.st_ino):
            return None
        process = probe_process(os.getpid())
        if process.get('state') != 'ALIVE' or not process.get('birth'):
            return None
        body = {'schema_version': 1, 'pid': os.getpid(), 'process_birth': process['birth'],
                'token': token, 'fd': descriptor, 'lock_dev': item.st_dev, 'lock_ino': item.st_ino}
        raw = json.dumps(body, sort_keys=True).encode()
        if len(raw) > MAX_OWNER_BYTES:
            return None
        path = Path(root) / OWNER_PATH
        if path.is_symlink():
            return None
        fd, temporary = tempfile.mkstemp(prefix='.writer-owner-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return token
    except Exception:
        return None
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def clear_owner(root, token):
    if token is None:
        return
    try:
        path = Path(root) / OWNER_PATH
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OWNER_BYTES:
            return
        value = json.loads(path.read_bytes())
        if value.get('token') == token and value.get('pid') == os.getpid():
            path.unlink()
    except Exception:
        pass
