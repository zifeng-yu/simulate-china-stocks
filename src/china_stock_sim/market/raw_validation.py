"""Bounded, update-local reuse of fully checked immutable source objects.

This cache proves raw bytes and their recursive dependencies, never query
results, request success, normalization, or availability at a knowledge cutoff.
"""

import stat
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident

from china_stock_sim.config import local_path
from china_stock_sim.market.storage import digest, read_json


@dataclass
class _VerifiedRaw:
    body: dict
    identities: dict | None
    weight: int


class RawValidationScope:
    def __init__(self, root, *, max_entries=4096, max_bytes=32 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.objects = local_path(self.root, "data/raw/market")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.closed = False
        self.owner_thread = get_ident()
        self.metrics = Counter()

    def close(self):
        self.entries.clear()
        self.bytes = 0
        self.closed = True

    def _path(self, reference):
        path = local_path(self.root, reference)
        if not path.is_relative_to(self.objects):
            raise ValueError("原始对象不在本项目数据对象目录内")
        return path

    def _identity(self, reference):
        path = self._path(reference)
        # A formerly regular lexical path becoming a link cannot inherit a
        # proof for the old resolved path. Legal legacy links use full checking.
        current = self.root
        for part in Path(reference).parts:
            current /= part
            if current.is_symlink():
                return None
        value = path.stat()
        if not stat.S_ISREG(value.st_mode):
            return None
        return (str(path), value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)

    def _unchanged(self, identities):
        return identities is not None and all(
            self._identity(reference) == identity
            for reference, identity in identities.items())

    def _discard(self, reference):
        old = self.entries.pop(reference, None)
        if old is not None:
            self.bytes -= old.weight

    def lookup(self, reference):
        if self.closed:
            raise ValueError("原件验证作用域已关闭")
        known = self.entries.get(reference)
        if known is not None:
            try:
                unchanged = self._unchanged(known.identities)
            except (OSError, ValueError):
                unchanged = False
            if unchanged:
                self.entries.move_to_end(reference)
                self.metrics["cache_hits"] += 1
                return known
            self._discard(reference)
            self.metrics["invalidated"] += 1
        self.metrics["cache_misses"] += 1
        return None

    def read(self, reference, active=None):
        active = set() if active is None else active
        if reference in active:
            raise ValueError("原始对象依赖循环")
        known = self.lookup(reference)
        if known is not None:
            return known
        active.add(reference)
        try:
            before = self._identity(reference)
            path = self._path(reference)
            body = read_json(path)
            self.metrics["full_checks"] += 1
            self.metrics["raw_bytes_read"] += path.stat().st_size
            if not isinstance(body, dict) or digest(body) != path.stem:
                raise ValueError("原始对象摘要或结构不符")
            dependencies = body.get("dependencies", [])
            if not isinstance(dependencies, list) or any(
                    not isinstance(item, str) for item in dependencies):
                raise ValueError("原始对象依赖格式无效")
            identities = {reference: before} if before is not None else None
            for child in dependencies:
                validated = self.read(child, active)
                if identities is not None and validated.identities is not None:
                    identities.update(validated.identities)
                else:
                    identities = None
            if before is not None and (
                    self._identity(reference) != before
                    or (identities is not None and not self._unchanged(identities))):
                raise ValueError("原始对象读取期间变化")
            weight = path.stat().st_size + sum(
                len(name.encode()) + 128 for name in identities or ())
            result = _VerifiedRaw(body, identities, weight)
            # Capacity changes performance only. Large legal objects still
            # receive full verification and retain their ordinary semantics.
            if identities is not None and self.max_entries > 0 and weight <= self.max_bytes:
                self._discard(reference)
                while self.entries and (
                        len(self.entries) >= self.max_entries
                        or self.bytes + weight > self.max_bytes):
                    first = next(iter(self.entries))
                    self._discard(first)
                    self.metrics["evictions"] += 1
                self.entries[reference] = result
                self.bytes += weight
            else:
                self.metrics["uncached_reads"] += 1
            return result
        finally:
            active.remove(reference)
