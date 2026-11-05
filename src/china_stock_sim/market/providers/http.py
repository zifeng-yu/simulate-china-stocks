"""Small public HTTP transport with a process deadline and server cooldown metadata."""

import hashlib
import json
import math
import socket
import subprocess
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .baostock import ProviderError

MAX_BYTES = 2_000_000
ALLOWED_ENDPOINTS = {
    ("proxy.finance.qq.com", "/ifzqgtimg/appstock/app/newfqkline/get"),
    ("datacenter-web.eastmoney.com", "/api/data/v1/get"),
}


class HTTPProviderError(ProviderError):
    def __init__(
        self, message, *, retryable=False, retry_after_seconds=None, status_code=None, category=None
    ):
        super().__init__(message, retryable=retryable, category=category)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


def retry_after(value: str | None, now: datetime | None = None) -> int | None:
    if not value:
        return None
    if value.strip().isdigit():
        return int(value.strip())
    try:
        timestamp = parsedate_to_datetime(value)
        if timestamp.tzinfo is None:
            return None
        return max(0, math.ceil((timestamp - (now or datetime.now(timezone.utc))).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or (parsed.hostname, parsed.path) not in ALLOWED_ENDPOINTS
    ):
        raise HTTPProviderError("公开行情请求地址不在适配器白名单内")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPProviderError("公开行情接口发生重定向，需要核实来源地址")


def _download(url: str, timeout: float) -> dict:
    _validate_url(url)
    request = Request(
        url, headers={"Accept": "application/json", "User-Agent": "ChinaStockSim/0.1"}
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise HTTPProviderError("公开行情响应超出大小限制", category="FORMAT")
            status = response.status
    except HTTPError as exc:
        raise HTTPProviderError(
            f"公开行情 HTTP {exc.code}",
            retryable=exc.code in {408, 425, 429} or 500 <= exc.code <= 599,
            retry_after_seconds=retry_after(exc.headers.get("Retry-After")),
            status_code=exc.code,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HTTPProviderError(f"公开行情网络失败: {exc}", retryable=True) from exc
    try:
        raw_text = raw.decode("utf-8")
        payload = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPProviderError(
            "公开行情返回非 UTF-8 JSON，不能作为成功数据", category="FORMAT"
        ) from exc
    return {
        "payload": payload,
        "raw_text": raw_text,
        "source_url": url,
        "http_status": status,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    }


def fetch_json(url: str, *, timeout: float = 20) -> dict:
    """One request only; caller owns retries and must honor retry_after_seconds."""
    _validate_url(url)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("HTTP 超时必须为有限正数")
    try:
        process = subprocess.run(
            [sys.executable, "-m", "china_stock_sim.market.providers.http"],
            input=json.dumps({"url": url, "timeout": timeout}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPProviderError("公开行情请求达到总超时，子进程已终止", retryable=True) from exc
    except OSError as exc:
        raise HTTPProviderError("无法启动公开行情子进程") from exc
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPProviderError("公开行情子进程没有返回有效结果", category="FORMAT") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise HTTPProviderError("公开行情子进程返回无效状态", category="FORMAT")
    if not result["ok"]:
        raise HTTPProviderError(
            result.get("error", "公开行情请求失败"),
            retryable=result.get("retryable") is True,
            retry_after_seconds=result.get("retry_after_seconds"),
            status_code=result.get("status_code"),
            category=result.get("category"),
        )
    if process.returncode != 0:
        raise HTTPProviderError("公开行情子进程异常退出", retryable=True)
    return result["data"]


def main():
    socket.setdefaulttimeout(10)
    try:
        request = json.loads(sys.stdin.read(8193))
        if not isinstance(request, dict) or set(request) != {"url", "timeout"}:
            raise HTTPProviderError("无效的公开行情请求")
        data = _download(request["url"], request["timeout"])
        result = {"ok": True, "data": data}
    except HTTPProviderError as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "retryable": exc.retryable,
            "retry_after_seconds": exc.retry_after_seconds,
            "status_code": exc.status_code,
            "category": exc.category,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"公开行情适配错误: {type(exc).__name__}",
            "retryable": False,
            "category": "CONFIGURATION",
        }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
