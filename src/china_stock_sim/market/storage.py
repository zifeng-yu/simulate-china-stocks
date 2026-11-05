"""Project-local atomic files and content-checked source response cache."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def response_content_digest(response: dict) -> str:
    """Ignore acquisition time and ordering, not the actual source observations."""
    return digest(
        {
            "provider": response["provider"],
            "adapter_version": response["adapter_version"],
            "fields": sorted(response["fields"]),
            "rows": sorted(response["rows"], key=digest),
        }
    )


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 文件应为对象: {path.name}")
    return value


class ResponseCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def load(self, identity: dict) -> dict | None:
        path = self.directory / f"{digest(identity)}.json"
        if not path.exists():
            return None
        envelope = read_json(path)
        if envelope.get("identity") != identity or envelope.get("sha256") != digest(
            envelope.get("response")
        ):
            raise ValueError(f"原始响应缓存校验失败: {path.name}")
        return envelope["response"]

    def save(self, identity: dict, response: dict) -> None:
        existing = self.load(identity)
        if existing is not None and digest(existing) != digest(response):
            raise ValueError("已有成功响应不能原地覆盖；请先记录修复或源订正")
        write_json(
            self.directory / f"{digest(identity)}.json",
            {
                "identity": identity,
                "sha256": digest(response),
                "response": response,
            },
        )

    def quarantine(self, identity: dict, reason: str) -> dict:
        """Move a damaged file aside without losing its original bytes."""
        source = self.directory / f"{digest(identity)}.json"
        record = {
            "identity": identity,
            "reason": reason,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "original_name": source.name,
            "missing": not source.exists(),
        }
        directory = self.directory / "quarantine" / f"{digest(identity)}-{uuid4().hex}"
        directory.mkdir(parents=True)
        if source.exists():
            record["file_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            os.replace(source, directory / "original.json")
        write_json(directory / "repair.json", record)
        return {**record, "quarantine_reference": str(directory.relative_to(self.directory))}

    def preserve_revision(self, identity: dict, response: dict) -> str:
        """Keep a conflicting source response without replacing the frozen input."""
        path = self.directory / "revisions" / f"{digest(identity)}-{digest(response)}.json"
        write_json(path, {"identity": identity, "sha256": digest(response), "response": response})
        return str(path.relative_to(self.directory))
