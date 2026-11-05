"""加载仓库内配置；不访问父项目，不调用网络或模型。"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_CASH_FEN = 2**63 - 1


def valid_account_label(value):
    """Identifiers are opaque text; report filenames must encode them separately."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


@dataclass(frozen=True)
class AccountSpec:
    id: str
    strategy: str
    initial_cash_fen: int


@dataclass(frozen=True)
class Settings:
    root: Path
    database: Path
    report: Path
    timezone: ZoneInfo
    accounts: tuple[AccountSpec, ...]
    default_account: str
    raw: dict


def project_root(explicit: str | Path | None = None) -> Path:
    selected = explicit or os.environ.get("CHINA_STOCK_SIM_ROOT")
    if selected:
        return Path(selected).expanduser().resolve()
    # Editable source checkout: package -> src -> repository.
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "configs/default.toml").is_file():
        raise ValueError("无法定位项目配置，请显式传入 --root 项目目录")
    return candidate


def local_path(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise ValueError("运行文件路径必须是项目内的相对路径")
    resolved = (root / value).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("运行文件路径不能通过符号链接指向项目外")
    return resolved


def load_settings(root: str | Path | None = None) -> Settings:
    base = project_root(root)
    with (base / "configs/default.toml").open("rb") as stream:
        raw = tomllib.load(stream)
    if (
        type(raw.get("schema_version")) is not int
        or raw["schema_version"] != 1
        or raw.get("mode") != "paper"
    ):
        raise ValueError("只支持 schema_version=1 的 paper 模式配置")
    if raw.get("timezone") != "Asia/Shanghai":
        raise ValueError("本方案使用 Asia/Shanghai 时区")
    seeds = raw.get("accounts")
    if (not isinstance(seeds, list) or not seeds or any(
            not isinstance(item, dict) or set(item) != {"id", "strategy", "initial_cash_fen"}
            for item in seeds)):
        raise ValueError("初始账户配置必须为非空列表，且每项明确ID、策略和本金")
    accounts = tuple(AccountSpec(**item) for item in seeds)
    if (any(not valid_account_label(a.id) for a in accounts)
            or len({a.id for a in accounts}) != len(accounts)):
        raise ValueError("账户ID必须为不重复的非空字符串且不超过256字符")
    for account in accounts:
        if type(account.initial_cash_fen) is not int or not 0 < account.initial_cash_fen <= MAX_CASH_FEN:
            raise ValueError("初始资金必须为正整数分，且不能超过SQLite整数范围")
        if not valid_account_label(account.strategy):
            raise ValueError("策略标识必须为非空字符串且不超过256字符")
    reporting = raw["reporting"]
    if reporting["aggregate_accounts"] is not False or not valid_account_label(reporting["default_account"]):
        raise ValueError("报告必须选择独立账户，不允许合并账户资产")
    for section in [raw[name] for name in ('schedule', 'market', 'execution')] + list(raw['strategies'].values()):
        if type(section.get('enabled')) is not bool:
            raise ValueError('流程各层enabled必须是明确布尔值')
    schedule = raw['schedule']
    if schedule.get('daily_start') != '18:10' or schedule.get('plan_cutoff') != '09:00':
        raise ValueError('流程使用北京时间18:10日终及09:00计划截止，与账本守卫保持一致')
    from china_stock_sim.scheduler.planning import automatic_window
    automatic_window('2000-01-01T12:00:00+08:00', schedule)
    for name, default, maximum in [('awake_check_minutes', 15, 60),
                                   ('retry_minutes', 15, 1440),
                                   ('max_sessions_per_run', 20, 500),
                                   ('max_run_seconds', 3600, 86400)]:
        value = schedule.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'日程{name}必须为1至{maximum}整数')
    if type(raw['market'].get('max_active_collection_seconds')) is not int or not 1 <= raw['market']['max_active_collection_seconds'] <= 86400:
        raise ValueError('采集活动预算必须为正整数秒且不超过86400')
    database = local_path(base, raw["paths"]["database"])
    report = local_path(base, raw["paths"]["report"])
    if not database.is_relative_to(base / "data/state"):
        raise ValueError("账本必须保存于 data/state 下")
    if not report.is_relative_to(base / "reports"):
        raise ValueError("生成页面必须保存于 reports 下")
    return Settings(
        base,
        database,
        report,
        ZoneInfo(raw["timezone"]),
        accounts,
        reporting["default_account"],
        raw,
    )
