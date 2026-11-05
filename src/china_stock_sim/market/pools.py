"""Single current pool workflow, with effective dates and retained historical facts."""

from pathlib import Path

from china_stock_sim.market.contracts import session_date
from china_stock_sim.market.preparation import Preparation
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.settings import load_market_settings
from china_stock_sim.market.update import import_preparation


def prepare_pool(root: Path, as_of: str, *, download=False, repair=False, force=False, **options):
    """First pool or month-end successor. Failure never removes the active pool."""
    load_market_settings(root)
    as_of = session_date(as_of)
    service = MarketDataService(root)
    active = service.pool(as_of)
    if active.state == "CORRUPT":
        return {"state": "NEEDS_DATA_REVIEW", "active_pool": active.as_dict(),
                "reason": "当前应生效股票池的依据损坏，须先修复；不能按首次建池重选"}
    effective = as_of
    if active.ready:
        if not force:
            month_end = service.calendar_operation("is_month_end", as_of)
            if not month_end.ready:
                return {"state": "WAITING_CALENDAR", "active_pool": active.as_dict()}
            if not month_end.data or active.data["date"] == as_of:
                return {"state": "UNCHANGED", "active_pool": active.as_dict()}
        following = service.calendar_operation("next_session", as_of)
        if not following.ready:
            return {"state": "WAITING_CALENDAR", "active_pool": active.as_dict()}
        effective = following.data
    prepared = Preparation(root, as_of, download=download, repair=repair, **options).run()
    if prepared.get("price_ready") and prepared.get("pool", {}).get("selected"):
        imported = import_preparation(root, as_of=as_of, effective_from=effective)
        return {"state": "POOL_PREPARED", "effective_from": effective,
                "run_id": prepared["run_id"], "import": imported, "preparation": prepared}
    return {"state": "KEEP_PREVIOUS" if active.ready else "WAITING_DATA",
            "active_pool": active.as_dict(), "preparation": prepared}
