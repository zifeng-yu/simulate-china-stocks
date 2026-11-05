"""Source acquisition times required for an end-of-day distribution check."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from china_stock_sim.config import local_path
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.market.storage import read_json


def collection_floor(as_of: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(session_date(as_of)).date(),
                            time(18, 10), ZoneInfo("Asia/Shanghai"))


def coverage_freshness_issues(repository, reference, required_at, *, knowledge_cutoff):
    """Check original leaves, never a synthetic object's later publication time.

    The caller validates immutable source objects before invoking this traversal.
    Old objects stay in the repository; this check only refuses unsuitable uses.
    """
    issues, seen = [], set()
    known = aware_time(knowledge_cutoff)

    def visit(path):
        if path in seen:
            return
        seen.add(path)
        raw = read_json(local_path(repository.root, path))
        dependencies = raw.get("dependencies", [])
        if dependencies:
            for dependency in dependencies:
                visit(dependency)
            return
        fetched = aware_time(raw.get("fetched_at"))
        if fetched < required_at or fetched > known:
            issues.append({"kind": "EVENT_COVERAGE_STALE", "raw_reference": path,
                           "required_fetched_at": required_at.isoformat(),
                           "fetched_at": fetched.isoformat()})

    visit(reference)
    return tuple(issues)
