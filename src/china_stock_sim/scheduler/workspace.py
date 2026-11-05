"""Workspace directories and a process/thread-safe outer workflow writer lock."""

import fcntl
import os
import threading
from contextlib import contextmanager

from china_stock_sim.config import Settings, local_path
from china_stock_sim.readiness.writer_owner import clear_owner, publish_owner

DIRECTORIES = (
    "data/raw",
    "data/normalized",
    "data/state",
    "data/backups",
    "data/plans",
    "reports/forward_current",
    "logs",
)
_LOCAL = threading.local()


def ensure_directories(settings: Settings) -> None:
    for relative in DIRECTORIES:
        local_path(settings.root, relative).mkdir(parents=True, exist_ok=True)


@contextmanager
def writer_lock(settings: Settings, *, reentrant=False):
    lock_path = local_path(settings.root, "data/state/writer.lock")
    identity = (os.getpid(), str(lock_path))
    held = getattr(_LOCAL, "held", {})
    if identity in held:
        if not reentrant:
            raise RuntimeError("已有进程在更新账本/页面，请等待当前运行结束")
        held[identity] += 1
        try:
            yield
        finally:
            held[identity] -= 1
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有进程在更新账本/页面，请等待当前运行结束") from exc
        held[identity] = 1
        _LOCAL.held = held
        try:
            token = publish_owner(settings.root, lock_path, stream.fileno())
        except Exception:
            token = None
        try:
            yield
        finally:
            try:
                clear_owner(settings.root, token)
            except Exception:
                pass
            del held[identity]
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
