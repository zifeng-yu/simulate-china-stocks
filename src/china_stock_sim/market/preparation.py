"""Resumable, bounded first preparation. Never calls a model or writes a ledger."""

import fcntl
import time
import tomllib
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as daytime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from china_stock_sim.config import local_path
from china_stock_sim.market.providers import BaoStockProvider, ProviderError
from china_stock_sim.market.quality import (
    DataUnavailableError,
    compare_overlapping_bars,
    normalize_bars,
    normalize_code,
    validate_window,
)
from china_stock_sim.market.storage import (
    ResponseCache,
    digest,
    read_json,
    response_content_digest,
    write_json,
)
from china_stock_sim.market.universe import normalize_securities, select_pool

SHANGHAI = ZoneInfo("Asia/Shanghai")
PIPELINE_VERSION = 1


class SourceConflictError(ValueError):
    """A source observation differs from a frozen preparation dependency."""


def error_category(error: Exception) -> str:
    declared = getattr(error, "category", None)
    if declared in {"UNPUBLISHED", "NETWORK", "FORMAT", "CONFLICT", "CACHE", "CONFIGURATION"}:
        return declared
    if isinstance(error, SourceConflictError):
        return "CONFLICT"
    if isinstance(error, DataUnavailableError):
        return "UNPUBLISHED"
    if isinstance(error, ProviderError):
        return "NETWORK" if error.retryable else "CONFIGURATION"
    return "FORMAT"


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def load_preparation_config(root) -> dict:
    path = local_path(root, "configs/preparation.toml")
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    for key, expected in (
        ("schema_version", 1),
        ("history_sessions", 121),
        ("liquidity_sessions", 20),
        ("provider", "baostock"),
    ):
        if config.get(key) != expected or isinstance(config.get(key), bool):
            raise ValueError(f"首次准备配置 {key} 必须为 {expected}")
    ranges = {
        "max_pool_size": (1, 100),
        "calendar_lookback_days": (300, 1000),
        "request_timeout_seconds": (5, 120),
        "max_requests_per_run": (1, 1000),
        "max_run_seconds": (5, 7200),
    }
    for key, (minimum, maximum) in ranges.items():
        value = config.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    delays = config.get("retry_delays_seconds")
    if (
        not isinstance(delays, list)
        or not delays
        or any(type(x) is not int or not 1 <= x <= 3600 for x in delays)
        or delays != sorted(delays)
    ):
        raise ValueError("重试间隔必须是递增的正整数秒列表")
    return config


def validate_as_of(value: str, now: datetime) -> date:
    requested = date.fromisoformat(value)
    if requested.isoformat() != value:
        raise ValueError("截止日期必须为 YYYY-MM-DD")
    local = now.astimezone(SHANGHAI)
    latest = local.date() if local.time() >= daytime(18, 10) else local.date() - timedelta(days=1)
    if requested > latest:
        raise ValueError("只能准备已经结束并到达 18:10 采集时点的日期；不能使用未来行情")
    return requested


def calendar_sessions(response: dict, start: str, end: str) -> list[str]:
    rows = response["rows"]
    if not rows:
        raise DataUnavailableError("交易日历尚未返回数据")
    days = {}
    for row in rows:
        value = row.get("calendar_date")
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("交易日历日期无效")
        if (
            value in days
            or not start <= value <= end
            or row.get("is_trading_day") not in {"0", "1"}
        ):
            raise ValueError("交易日历重复、越界或交易状态无效")
        days[value] = row["is_trading_day"]
    expected = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if len(days) != expected:
        raise ValueError("交易日历存在缺日，不能按周一至周五猜测交易日")
    sessions = sorted(day for day, flag in days.items() if flag == "1")
    if len(sessions) < 121:
        raise ValueError("交易日历不足 121 个交易日")
    return sessions[-121:]


def validate_clues(response: dict, query: str, params: dict) -> list[dict]:
    """Check source shape and return only effective events through the cutoff.

    These are source clues, not approved cash/share entitlements or signal factors.
    """
    required = (
        {"code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"}
        if query == "adjustments"
        else {"code", "dividOperateDate"}
    )
    if not required.issubset(response["fields"]):
        raise ValueError(f"{query} 响应缺少必要字段")
    output, seen = [], set()
    for row in response["rows"]:
        if normalize_code(row["code"]) != normalize_code(params["code"]):
            raise ValueError("权益线索证券代码不一致")
        event_date = row["dividOperateDate"]
        if not event_date:  # Source can publish a distribution before setting an ex-date.
            if query == "adjustments":
                raise ValueError("复权因子缺少生效日期")
            continue
        if date.fromisoformat(event_date).isoformat() != event_date:
            raise ValueError("权益线索日期无效")
        identity = digest(row)
        if identity in seen:
            raise ValueError("权益线索记录重复")
        seen.add(identity)
        if query == "adjustments":
            for field in required - {"code", "dividOperateDate"}:
                try:
                    number = Decimal(row[field])
                except InvalidOperation as exc:
                    raise ValueError("复权因子不是数值") from exc
                if not number.is_finite() or number <= 0:
                    raise ValueError("复权因子必须为正的有限数值")
            if not params["start_date"] <= event_date <= params["end_date"]:
                raise ValueError("复权因子生效日期越界")
        elif date.fromisoformat(event_date).year != int(params["year"]):
            raise ValueError("分红生效年份与 operate 查询不符")
        output.append(row)
    return output


@contextmanager
def preparation_lock(root):
    path = local_path(root, "data/state/preparation/writer.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有首次准备进程运行，请等待后再继续") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def data_status(root) -> dict:
    path = local_path(root, "data/state/preparation/latest.json")
    if not path.is_file():
        return {
            "state": "NOT_STARTED",
            "price_ready": False,
            "signal_ready": False,
            "trading_ready": False,
            "network_tested": False,
        }
    return read_json(path)


class Preparation:
    def __init__(
        self,
        root,
        as_of: str,
        *,
        download=False,
        provider=None,
        clock=now_shanghai,
        max_requests=None,
        budget_seconds=None,
        repair=False,
    ):
        self.root, self.clock, self.download = root, clock, download
        if repair and not download:
            raise ValueError("缓存修复需要显式允许 download")
        self.repair = repair
        self.config = load_preparation_config(root)
        self.as_of = validate_as_of(as_of, clock()).isoformat()
        self.provider = provider or BaoStockProvider(timeout=self.config["request_timeout_seconds"])
        self.maximum = self.config["max_requests_per_run"] if max_requests is None else max_requests
        self.budget = self.config["max_run_seconds"] if budget_seconds is None else budget_seconds
        if type(self.maximum) is not int or not 1 <= self.maximum <= 1000:
            raise ValueError("每次请求上限必须在 1 到 1000 之间")
        if type(self.budget) is not int or not 5 <= self.budget <= 7200:
            raise ValueError("本次时间预算必须在 5 到 7200 秒之间")
        identity = {
            "as_of": self.as_of,
            "pipeline_version": PIPELINE_VERSION,
            "provider": self.provider.provider_id,
            "adapter_version": self.provider.adapter_version,
            "history_sessions": 121,
            "pool_size": self.config["max_pool_size"],
            "calendar_lookback_days": self.config["calendar_lookback_days"],
        }
        self.run_id = self.as_of + "-" + digest(identity)[:16]
        self.state_dir = local_path(root, f"data/state/preparation/{self.run_id}")
        self.output_dir = local_path(root, f"data/normalized/preparation/{self.run_id}")
        self.cache = ResponseCache(local_path(root, "data/raw/baostock"))
        self.manifest_path = self.state_dir / "manifest.json"
        self.identity = identity
        self.calls = 0
        self.seen = {}
        self.frozen_pool = None
        self.expected_dependencies = {}

    def publish(self):
        """Persist data progress only; report rendering belongs to the caller."""
        self.state["updated_at"] = self.clock().isoformat()
        self.state["requests_this_run"] = self.calls
        write_json(self.manifest_path, self.state)
        write_json(local_path(self.root, "data/state/preparation/latest.json"), self.state)

    def request_identity(self, query, params):
        return {
            "provider": self.provider.provider_id,
            "adapter_version": self.provider.adapter_version,
            "query": query,
            "params": params,
            "vintage": self.run_id,
        }

    def load_frozen_pool(self):
        path = self.state_dir / "pool.json"
        if not path.exists():
            if self.state.get("pool", {}).get("frozen_at"):
                raise ValueError("冻结股票池文件缺失，不能重新选池掩盖丢失")
            return
        envelope = read_json(path)
        pool = envelope["pool"]
        if envelope.get("sha256") != digest(pool):
            raise ValueError("已冻结股票池校验失败，不能重新选池掩盖损坏")
        if pool.get("identity") != self.identity or not isinstance(pool.get("dependencies"), dict):
            raise ValueError("冻结股票池缺少匹配的任务身份或依赖证明")
        for key, dependency in pool["dependencies"].items():
            identity = dependency["identity"]
            if (
                digest(identity) != key
                or identity != self.request_identity(identity["query"], identity["params"])
                or not dependency.get("content_sha256")
            ):
                raise ValueError("冻结股票池依赖身份无效")
        self.frozen_pool = pool
        self.expected_dependencies = pool["dependencies"]

    def recover_attempts(self):
        """Retain counts and failure evidence across interrupted process restarts."""
        for path in sorted((self.state_dir / "attempts").glob("*.json")):
            record = read_json(path)
            identity = record["request"]
            if identity.get("vintage") != self.run_id:
                raise ValueError("尝试记录属于其他准备任务")
            key = digest(identity)
            previous = record["outcome"]
            current = self.state["requests"].get(key)
            if current is None or previous["attempts"] > current["attempts"]:
                self.state["requests"][key] = previous
        for key, status in self.state["requests"].items():
            if status["status"] == "RUNNING":
                status.update(
                    status="INTERRUPTED",
                    error_category="INTERRUPTED",
                    error="上次请求在结果确认前中断，允许恢复",
                    retryable=True,
                )
                write_json(
                    self.state_dir / "attempts" / f"{key}-{status['attempts']}.json",
                    {
                        "request": self.request_identity(status["query"], status["params"]),
                        "outcome": status.copy(),
                        "rejected_response": None,
                    },
                )

    def check_frozen_content(self, key, response):
        expected = self.expected_dependencies.get(key, {}).get("content_sha256")
        expected = expected or self.state["requests"][key].get("accepted_content_sha256")
        if expected and response_content_digest(response) != expected:
            identity = self.request_identity(
                self.state["requests"][key]["query"], self.state["requests"][key]["params"]
            )
            reference = self.cache.preserve_revision(identity, response)
            self.state["requests"][key]["revision_reference"] = "data/raw/baostock/" + reference
            raise SourceConflictError(
                "源内容与已冻结依据不同，已保留订正证据；需要受控确认，不能重选池"
            )

    def source_dependencies(self, keys):
        dependencies = {}
        for key, status in self.state["requests"].items():
            if key not in keys:
                continue
            if status["status"] != "OK":
                continue
            dependencies[key] = {
                "identity": self.request_identity(status["query"], status["params"]),
                "response_sha256": status["raw_sha256"],
                "content_sha256": status["content_sha256"],
            }
        return dependencies

    def fetch(self, query, params, validator):
        identity = self.request_identity(query, params)
        key = digest(identity)
        if key in self.seen:
            return self.seen[key]
        status = self.state["requests"].setdefault(
            key,
            {
                "query": query,
                "params": params,
                "attempts": 0,
                "status": "PENDING",
            },
        )
        cache_problem = None
        try:
            cached = self.cache.load(identity)
            if cached is not None:
                self.check_frozen_content(key, cached)
                value = validator(cached)
                status.update(
                    status="OK",
                    fetched_at=cached["fetched_at"],
                    raw_reference=f"data/raw/baostock/{key}.json",
                    raw_sha256=digest(cached),
                    content_sha256=response_content_digest(cached),
                )
                for field in ("error", "error_category", "next_retry_at", "retryable"):
                    status.pop(field, None)
                self.seen[key] = value
                return value
            if key in self.expected_dependencies or status.get("accepted_content_sha256"):
                raise ValueError("已冻结依赖的原始缓存缺失")
        except SourceConflictError as exc:
            status.update(status="CONFLICT", error_category="CONFLICT", error=str(exc))
            self.seen[key] = None
            return None
        except (ValueError, KeyError, TypeError) as exc:
            status.update(
                status="CACHE_INVALID", error_category="CACHE", error=str(exc), retryable=False
            )
            if not self.repair:
                self.seen[key] = None
                return None
            cache_problem = str(exc)
        if not self.download:
            if status["status"] in {"PENDING", "BUDGET_PAUSED"}:
                status["status"] = "WAITING_DOWNLOAD"
            return None
        if self.calls >= self.maximum or time.monotonic() - self.started >= self.budget:
            status["status"] = "BUDGET_PAUSED"
            return None
        if status.get("next_retry_at") and self.clock() < datetime.fromisoformat(
            status["next_retry_at"]
        ):
            return None
        if cache_problem is not None:
            repair = self.cache.quarantine(identity, cache_problem)
            status.setdefault("repairs", []).append(repair)
        self.calls += 1
        status.update(
            status="RUNNING",
            attempts=status["attempts"] + 1,
            last_attempt_at=self.clock().isoformat(),
        )
        self.publish()  # An interruption records an attempt, never a false success.
        response = None
        try:
            self.state["network_tested"] = True
            if isinstance(self.provider, BaoStockProvider):
                # Bound the last request by the remaining active budget as well.
                self.provider.timeout = max(
                    0.1,
                    min(
                        self.config["request_timeout_seconds"],
                        self.budget - (time.monotonic() - self.started),
                    ),
                )
            response = self.provider.fetch(query, params)
            self.check_frozen_content(key, response)
            value = validator(response)
            self.cache.save(identity, response)
            status.update(
                status="OK",
                fetched_at=response["fetched_at"],
                raw_reference=f"data/raw/baostock/{key}.json",
                raw_sha256=digest(response),
                content_sha256=response_content_digest(response),
            )
            status.pop("error", None)
            status.pop("error_category", None)
            status.pop("next_retry_at", None)
            status.pop("retryable", None)
            self.state["network_tested"] = True
            self.seen[key] = value
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            retryable = exc.retryable if isinstance(exc, ProviderError) else True
            delays = self.config["retry_delays_seconds"]
            delay = delays[min(status["attempts"] - 1, len(delays) - 1)]
            status.update(
                status="CONFLICT" if isinstance(exc, SourceConflictError) else "FAILED",
                error=str(exc),
                error_category=error_category(exc),
                retryable=retryable,
                next_retry_at=(self.clock() + timedelta(seconds=delay)).isoformat(),
            )
            write_json(
                self.state_dir / "attempts" / f"{key}-{status['attempts']}.json",
                {
                    "request": identity,
                    "outcome": status.copy(),
                    "rejected_response": response,
                },
            )
            self.seen[key] = None
        self.publish()
        return self.seen[key]

    def run(self):
        with preparation_lock(self.root):
            self.started = time.monotonic()
            self.calls, self.seen = 0, {}
            self.frozen_pool, self.expected_dependencies = None, {}
            if self.manifest_path.exists():
                self.state = read_json(self.manifest_path)
                if self.state["identity"] != self.identity:
                    raise ValueError("首次准备任务身份不一致")
            else:
                self.state = {
                    "run_id": self.run_id,
                    "identity": self.identity,
                    "requested_as_of": self.as_of,
                    "created_at": self.clock().isoformat(),
                    "requests": {},
                    "network_tested": False,
                }
            self.state.update(
                state="PREPARING",
                phase="calendar",
                price_ready=False,
                signal_ready=False,
                trading_ready=False,
                clues_downloaded=False,
                corporate_actions_status="CLUES_ONLY",
                metadata_scope="current_snapshot_not_historical_point_in_time",
                report="reports/preparation/index.html",
            )
            self.state.update(securities=[], conflicts=[], universe_data_quality=[])
            for field in ("error", "error_category", "metadata_issues"):
                self.state.pop(field, None)
            try:
                self.recover_attempts()
                self.load_frozen_pool()
                self.pipeline()
            except (ValueError, KeyError, TypeError) as exc:
                self.state.update(
                    state="NEEDS_DATA_REVIEW", error=str(exc), error_category=error_category(exc)
                )
            self.publish()
            return self.state

    def pipeline(self):
        start = (
            date.fromisoformat(self.as_of) - timedelta(days=self.config["calendar_lookback_days"])
        ).isoformat()
        sessions = self.fetch(
            "calendar",
            {"start_date": start, "end_date": self.as_of},
            lambda r: calendar_sessions(r, start, self.as_of),
        )
        if sessions is None:
            self.state["state"] = "WAITING_DATA"
            return
        self.state.update(sessions=sessions, effective_as_of=sessions[-1], phase="universe")
        securities = self.fetch("securities", {}, lambda r: self.securities(r, sessions[-1]))
        if securities is None:
            self.state["state"] = "WAITING_DATA"
            return
        eligible = {
            r["code"] for r in securities if r["eligible_metadata"] and r["listed"] <= sessions[0]
        }
        pool_path = self.state_dir / "pool.json"
        source_keys = {
            digest(
                self.request_identity("calendar", {"start_date": start, "end_date": self.as_of})
            ),
            digest(self.request_identity("securities", {})),
        }
        if not eligible and any(not row["eligibility_known"] for row in securities):
            self.state.update(
                state="NEEDS_DATA_REVIEW",
                error_category="FORMAT",
                error="证券资格信息不足，不能确认股票池为空",
                metadata_issues=[row for row in securities if not row["eligibility_known"]],
            )
            return
        bars = []
        if eligible:
            self.state["phase"] = "liquidity20"
            complete = True
            self.state["universe_data_quality"] = []
            for session in sessions[-20:]:
                source_keys.add(digest(self.request_identity("session_bars", {"date": session})))
                result = self.fetch(
                    "session_bars",
                    {"date": session},
                    lambda r, d=session: self.bulk(r, d, eligible),
                )
                if result is None:
                    complete = False
                else:
                    bars.extend(result["bars"])
                    self.state["universe_data_quality"].append(result["coverage"])
            if not complete:
                self.state["state"] = "WAITING_DATA"
                return
        if self.frozen_pool is not None:
            pool = self.frozen_pool
            if pool["sessions"] != sessions or set(pool["dependencies"]) != source_keys:
                raise SourceConflictError("冻结池的交易日或依赖集合与本次请求不一致")
        else:
            pool = select_pool(securities, bars, sessions, self.config["max_pool_size"])
            pool.update(
                frozen_at=self.clock().isoformat(),
                sessions=sessions,
                source_requests=sorted(source_keys),
                identity=self.identity,
                dependencies=self.source_dependencies(source_keys),
            )
            write_json(pool_path, {"pool": pool, "sha256": digest(pool)})
            self.frozen_pool = pool
            self.expected_dependencies = pool["dependencies"]
        # Keep the full exclusion list in the frozen pool file, not every progress rewrite.
        self.state["pool"] = {
            **pool,
            "excluded": pool["excluded"][:50],
            "excluded_count": len(pool["excluded"]),
            "full_pool_reference": str(pool_path.relative_to(self.root)),
        }
        if not pool["selected"]:
            self.state.update(state="NO_ELIGIBLE_SECURITIES", phase="complete")
            return
        self.state.update(phase="history_and_clues", securities=[])
        for stock in pool["selected"]:
            code = stock["code"]
            params = {"code": code, "start_date": sessions[0], "end_date": sessions[-1]}
            bars = self.fetch("history", params, lambda r, c=code: self.history(r, c, sessions))
            result = {
                "code": code,
                "name": stock["name"],
                "price_ready": bars is not None,
                "clues_downloaded": False,
                "signal_ready": False,
                "trading_ready": False,
                "state": "PRICE_READY" if bars is not None else "WAITING_DATA",
            }
            history_key = digest(self.request_identity("history", params))
            if self.state["requests"][history_key]["status"] == "CONFLICT":
                result.update(state="CONFLICT", error_category="CONFLICT")
                reference = self.state["requests"][history_key].get("revision_reference")
                result["conflict_reference"] = reference
                self.state["conflicts"].append({"code": code, "count": 1, "reference": reference})
            if bars is not None:
                # The batch inputs are immutable dependencies of the already frozen pool.
                batch_bars = []
                for session in sessions[-20:]:
                    batch_key = digest(self.request_identity("session_bars", {"date": session}))
                    batch_bars.extend(
                        row for row in self.seen[batch_key]["bars"] if row["code"] == code
                    )
                conflicts = compare_overlapping_bars(batch_bars, bars)
                if conflicts:
                    conflict_path = self.state_dir / "conflicts" / f"{code}.json"
                    evidence = {
                        "code": code,
                        "state": "CONFLICT",
                        "error_category": "CONFLICT",
                        "conflicts": conflicts,
                        "history_request": history_key,
                        "batch_requests": sorted(
                            source_keys
                            - {
                                digest(
                                    self.request_identity(
                                        "calendar", {"start_date": start, "end_date": self.as_of}
                                    )
                                ),
                                digest(self.request_identity("securities", {})),
                            }
                        ),
                    }
                    write_json(conflict_path, evidence)
                    result.update(
                        state="CONFLICT",
                        price_ready=False,
                        error_category="CONFLICT",
                        conflict_count=len(conflicts),
                        conflict_reference=str(conflict_path.relative_to(self.root)),
                    )
                    self.state["conflicts"].append(
                        {
                            "code": code,
                            "count": len(conflicts),
                            "reference": result["conflict_reference"],
                        }
                    )
                    bars = None
            if bars is not None:
                document = {
                    "code": code,
                    "sessions": sessions,
                    "bars": bars,
                    "price_basis": "unadjusted",
                    "volume_unit": "shares",
                    "amount_unit": "CNY",
                    "price_ready": True,
                    "signal_ready": False,
                    "trading_ready": False,
                    "source_requests": [
                        key
                        for key, row in self.state["requests"].items()
                        if row["query"] == "history" and row["params"] == params
                    ],
                }
                write_json(self.output_dir / f"{code}.json", document)
                result.update(
                    artifact_reference=str(
                        (self.output_dir / f"{code}.json").relative_to(self.root)
                    ),
                    artifact_sha256=digest(document),
                )
                request = self.state["requests"][history_key]
                request["accepted_content_sha256"] = request["content_sha256"]
            adjustments = self.fetch(
                "adjustments", params, lambda r, p=params: validate_clues(r, "adjustments", p)
            )
            dividends, all_dividends = [], True
            for year in range(
                date.fromisoformat(sessions[0]).year, date.fromisoformat(sessions[-1]).year + 1
            ):
                dp = {"code": code, "year": year}
                part = self.fetch(
                    "dividends", dp, lambda r, p=dp: validate_clues(r, "dividends", p)
                )
                if part is None:
                    all_dividends = False
                else:
                    dividends.extend(
                        row
                        for row in part
                        if sessions[0] <= row["dividOperateDate"] <= sessions[-1]
                    )
            result["clues_downloaded"] = adjustments is not None and all_dividends
            if result["clues_downloaded"]:
                write_json(
                    self.output_dir / f"{code}-clues.json",
                    {
                        "code": code,
                        "adjustments": adjustments,
                        "dividends": dividends,
                        "status": "CLUES_ONLY",
                        "trading_ready": False,
                    },
                )
            self.state["securities"].append(result)
            self.publish()
        self.state["price_ready"] = all(row["price_ready"] for row in self.state["securities"])
        self.state["clues_downloaded"] = all(
            row["clues_downloaded"] for row in self.state["securities"]
        )
        complete = self.state["price_ready"] and self.state["clues_downloaded"]
        self.state.update(state="PREPARATION_COMPLETE" if complete else "PARTIAL", phase="complete")
        self.state.pop("error", None)
        self.state.pop("error_category", None)

    @staticmethod
    def securities(response, session):
        if not response["rows"]:
            raise DataUnavailableError("证券信息返回空数据")
        required = {"code", "code_name", "ipoDate", "outDate", "type", "status"}
        if not required.issubset(response["fields"]):
            raise ValueError("证券信息响应缺少必要字段")
        return normalize_securities(response["rows"], session)

    @staticmethod
    def bulk(response, session, eligible):
        if not response["rows"]:
            raise DataUnavailableError("全市场日线为空，不能当作全体停牌")
        bars, issues, seen, conflicted = [], [], set(), set()
        for row in response["rows"]:
            try:
                code = normalize_code(row.get("code"))
            except ValueError:
                issues.append({"code": str(row.get("code")), "reason": "忽略无法识别的代码"})
                continue
            if code not in eligible:
                continue
            if code in seen:
                conflicted.add(code)
                issues.append({"code": code, "reason": "同日重复证券，全部排除"})
                continue
            seen.add(code)
            try:
                bars.extend(normalize_bars([row], allowed_dates=[session]))
            except ValueError as exc:
                issues.append({"code": code, "reason": str(exc)})
        bars = [bar for bar in bars if bar["code"] not in conflicted]
        if not bars:
            raise ValueError("全市场日线没有通过校验的目标股票")
        return {
            "bars": bars,
            "coverage": {
                "date": session,
                "eligible_count": len(eligible),
                "valid_count": len(bars),
                "missing_codes": sorted(eligible - {bar["code"] for bar in bars}),
                "issues": issues,
            },
        }

    @staticmethod
    def history(response, code, sessions):
        bars = normalize_bars(response["rows"], expected_code=code, allowed_dates=sessions)
        validation = validate_window(bars, code, sessions)
        if not validation["complete"]:
            raise DataUnavailableError(
                f"{code} 历史窗口缺日: {','.join(validation['missing_dates'])}"
            )
        return bars


def repair_preparation_cache(root, as_of: str, **kwargs) -> dict:
    """Explicitly repair damaged dependencies within the ordinary run budgets.

    Frozen stock choices are retained. Changed source facts are saved as conflict
    evidence for a controlled decision rather than silently replacing those facts.
    """
    return Preparation(root, as_of, download=True, repair=True, **kwargs).run()
