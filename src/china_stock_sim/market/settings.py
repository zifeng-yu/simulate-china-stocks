"""The data layer has its own settings and never loads accounts or model credentials."""

import importlib.metadata
import importlib.util
import tomllib
from dataclasses import dataclass
from pathlib import Path

from china_stock_sim.config import local_path
from china_stock_sim.market.preparation import load_preparation_config


@dataclass(frozen=True)
class MarketSettings:
    root: Path
    config: dict
    benchmarks: tuple[str, ...] = ("SH000001", "SZ399001", "SH000300")


def load_market_settings(root: Path) -> MarketSettings:
    root = Path(root).resolve()
    values = load_preparation_config(root)
    # Optional legacy declarative settings must agree; they cannot silently override the data file.
    path = local_path(root, "configs/default.toml")
    if path.is_file():
        with path.open("rb") as stream:
            declared = tomllib.load(stream).get("market", {})
        pairs = {"max_pool_size": "max_pool_size", "max_active_collection_seconds": "max_run_seconds",
                 "minimum_listing_sessions": "history_sessions", "primary_provider": "provider"}
        for old, current in pairs.items():
            if old in declared and declared[old] != values[current]:
                raise ValueError(f"数据配置不一致: default.market.{old} 与 preparation.{current}")
    return MarketSettings(root, values)


def data_doctor(root: Path) -> dict:
    """Import/version/API checks only; no login, network, .env, or file creation."""
    import os

    settings = load_market_settings(root)
    errors = []
    sdk = {"installed": importlib.util.find_spec("baostock") is not None}
    if sdk["installed"]:
        import baostock

        sdk["version"] = importlib.metadata.version("baostock")
        names = ("login", "logout", "query_trade_dates", "query_stock_basic",
                 "query_history_k_data_plus", "query_daily_history_k_AStock",
                 "query_adjust_factor", "query_dividend_data")
        sdk["missing_methods"] = [name for name in names if not callable(getattr(baostock, name, None))]
        if sdk["version"] != "0.9.4" or sdk["missing_methods"]:
            errors.append("BaoStock 版本或接口不匹配")
    else:
        errors.append("未安装 BaoStock")
    directory = local_path(settings.root, "data/state")
    parent = directory
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    writable = os.access(parent, os.W_OK | os.X_OK)
    if not writable:
        errors.append("数据目录不可写")
    return {"state": "READY" if not errors else "NOT_READY", "sdk": sdk,
            "storage_writable": writable, "network_tested": False, "errors": errors}
