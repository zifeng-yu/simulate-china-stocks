"""BaoStock adapter with a hard deadline around each isolated SDK process.

The subprocess deadline and RESULT marker originate from the previous project's
``src/ashare/data.py::_worker_unlocked``. No old project runtime is required.
"""

import json
import math
import re
import subprocess
import sys
from datetime import date, datetime
from typing import Any

from china_stock_sim.market.tax_facts import tax_field_capabilities

RESULT_PREFIX = "RESULT="
HISTORY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"
_PARAMS = {
    "calendar": {"start_date", "end_date"},
    "securities": set(),
    "session_bars": {"date"},
    "history": {"code", "start_date", "end_date"},
    "adjustments": {"code", "start_date", "end_date"},
    "dividends": {"code", "year"},
}


class ProviderError(RuntimeError):
    """A failed request; the caller decides whether and when to retry."""

    def __init__(self, message: str, *, retryable: bool = False, category: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.category = category or ("NETWORK" if retryable else "CONFIGURATION")


def validate_request(query: str, params: dict) -> dict:
    """Reject unknown operations/arguments before starting any SDK connection."""
    if not isinstance(query, str) or query not in _PARAMS:
        raise ProviderError("不支持的数据查询")
    if not isinstance(params, dict):
        raise ProviderError("查询参数必须是对象")
    params = dict(params)
    # The dividend basis is deliberately fixed, but callers may state it explicitly.
    if query == "dividends" and "yearType" in params:
        if params.pop("yearType") != "operate":
            raise ProviderError("分红查询仅支持 yearType=operate")
    if set(params) != _PARAMS[query]:
        raise ProviderError(f"{query} 参数必须为 {', '.join(sorted(_PARAMS[query])) or '空'}")
    for name in ("date", "start_date", "end_date"):
        if name in params:
            value = params[name]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ProviderError(f"{name} 必须为 YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ProviderError(f"{name} 不是有效日期") from exc
    if "start_date" in params and params["start_date"] > params["end_date"]:
        raise ProviderError("start_date 不能晚于 end_date")
    if "code" in params:
        code = params["code"]
        if not isinstance(code, str):
            raise ProviderError("股票代码必须是字符串")
        match = re.fullmatch(r"(sh|sz|bj)\.?([0-9]{6})", code.lower())
        if match is None:
            raise ProviderError("股票代码必须含市场前缀，例如 sh.600000")
        params["code"] = f"{match[1]}.{match[2]}"
    if "year" in params:
        year = params["year"]
        if isinstance(year, str) and re.fullmatch(r"\d{4}", year):
            year = int(year)
        if type(year) is not int or not 1900 <= year <= 9999:
            raise ProviderError("year 必须是四位年份")
        params["year"] = str(year)
    return params


def _validate_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ProviderError("数据子进程返回了无效结果", category="FORMAT")
    fields, rows = payload.get("fields"), payload.get("rows")
    if (
        not isinstance(fields, list)
        or not all(isinstance(field, str) and field for field in fields)
        or len(set(fields)) != len(fields)
        or not isinstance(rows, list)
    ):
        raise ProviderError("数据子进程返回了无效字段或记录", category="FORMAT")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != set(fields)
            or not all(isinstance(value, str) for value in row.values())
        ):
            raise ProviderError("数据子进程返回了不完整记录", category="FORMAT")
    try:
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError("数据子进程缺少有效获取时间", category="FORMAT") from exc
    if fetched_at.utcoffset() is None or fetched_at.utcoffset().total_seconds() != 8 * 3600:
        raise ProviderError("数据获取时间必须为上海时区", category="FORMAT")
    if payload.get("provider") != "baostock" or payload.get("adapter_version") != "1":
        raise ProviderError("数据子进程返回了不匹配的适配器标识", category="FORMAT")
    return {
        key: payload[key] for key in ("fields", "rows", "fetched_at", "provider", "adapter_version")
    }


class BaoStockProvider:
    provider_id = "baostock"
    adapter_version = "1"
    event_tax_capabilities = tax_field_capabilities(provider_id)

    def __init__(self, timeout: float = 45):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("数据请求超时必须是正数")
        self.timeout = timeout

    def fetch(self, query: str, params: dict) -> dict:
        normalized = validate_request(query, params)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "china_stock_sim.market.providers.worker"],
                input=json.dumps({"query": query, "params": normalized}, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"BaoStock 请求超过 {self.timeout:g} 秒，子进程已终止", retryable=True
            ) from exc
        except OSError as exc:
            raise ProviderError("无法启动数据子进程，请检查 Python 环境") from exc
        lines = [
            line[len(RESULT_PREFIX) :]
            for line in completed.stdout.splitlines()
            if line.startswith(RESULT_PREFIX)
        ]
        if not lines:
            raise ProviderError(
                f"数据子进程未返回结果（退出码 {completed.returncode}）", retryable=True
            )
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise ProviderError("数据子进程返回了损坏的 JSON", category="FORMAT") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ProviderError("数据子进程返回了无效状态", category="FORMAT")
        if not result["ok"]:
            message = result.get("error")
            raise ProviderError(
                message if isinstance(message, str) else "BaoStock 请求失败",
                retryable=result.get("retryable") is True,
                category=result.get("category"),
            )
        if completed.returncode != 0:
            raise ProviderError("数据子进程异常退出，结果未被接受", retryable=True)
        payload = _validate_payload(result.get("data"))
        if query == "dividends":
            payload["event_tax_capabilities"] = dict(self.event_tax_capabilities)
        return payload
