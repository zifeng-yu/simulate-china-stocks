"""Public Eastmoney distribution report; no key, cookies, model or private API.

Source page: https://data.eastmoney.com/yjfp/detail/600000.html
PRETAX_BONUS_RMB, BONUS_RATIO and IT_RATIO are per ten shares, confirmed
against IMPL_PLAN_PROFILE and issuer distributions. See acceptance evidence.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from china_stock_sim.market.quality import normalize_code
from china_stock_sim.market.tax_facts import tax_field_capabilities

from .baostock import ProviderError
from .http import HTTPProviderError, fetch_json, retry_after

ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"


class EastmoneyEventProvider:
    provider_id = "eastmoney_events"
    adapter_version = "1"
    event_tax_capabilities = tax_field_capabilities(provider_id)

    def __init__(self, timeout: float = 15, *, opener=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("请求超时必须是正数")
        self.timeout = timeout
        # Injected opener is solely for deterministic offline tests. Production
        # requests use the killable HTTP subprocess, including trickling bodies.
        self.opener = opener

    def fetch(self, query: str, params: dict) -> dict:
        if query not in {"events", "dividends"} or set(params) != {"code"}:
            raise ProviderError("东方财富事件仅支持 events/code 查询")
        return self.fetch_events(params["code"])

    def fetch_events(self, code: str) -> dict:
        code = normalize_code(code)
        if code[:2] not in {"SH", "SZ"}:
            raise ProviderError("只采集沪深普通证券权益")
        pages, rows, references = [], [], []
        count, expected_pages = None, 1
        deadline = time.monotonic() + self.timeout
        for page in range(1, 21):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPProviderError("东方财富事件查询达到总超时", retryable=True)
            params = {
                "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code[2:]}")', "pageSize": "100",
                "pageNumber": str(page), "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                "source": "WEB", "client": "WEB",
            }
            url = ENDPOINT + "?" + urllib.parse.urlencode(params)
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/",
            })
            try:
                if self.opener is None:
                    payload = fetch_json(url, timeout=remaining)["payload"]
                else:
                    with self.opener(request, timeout=remaining) as response:
                        body = response.read(8_000_001)
                    if len(body) > 8_000_000:
                        raise ProviderError("东方财富响应超过大小限制", category="FORMAT")
                    try:
                        payload = json.loads(body)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ProviderError("东方财富事件响应格式异常", category="FORMAT") from exc
            except urllib.error.HTTPError as exc:
                raise HTTPProviderError(
                    f"东方财富 HTTP {exc.code}",
                    retryable=exc.code in {408, 425, 429} or 500 <= exc.code <= 599,
                    retry_after_seconds=retry_after(exc.headers.get("Retry-After")),
                    status_code=exc.code,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ProviderError("东方财富网络请求失败", retryable=True) from exc
            if time.monotonic() > deadline:
                raise HTTPProviderError("东方财富事件查询达到总超时", retryable=True)
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise ProviderError("东方财富事件查询未确认成功", category="FORMAT")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ProviderError("东方财富未返回事件结果；不能把未知当作无事件",
                                    retryable=True, category="UNPUBLISHED")
            current, page_count, total = result.get("data"), result.get("pages"), result.get("count")
            if (not isinstance(current, list) or not all(isinstance(row, dict) for row in current)
                    or type(page_count) is not int or not 0 <= page_count <= 20
                    or type(total) is not int or total < 0):
                raise ProviderError("东方财富事件分页字段异常", category="FORMAT")
            if page == 1:
                count, expected_pages = total, max(1, page_count)
            elif total != count or page_count != expected_pages:
                raise ProviderError("东方财富事件分页期间数据变动", retryable=True)
            for row in current:
                if row.get("SECURITY_CODE") != code[2:]:
                    raise ProviderError("东方财富返回非请求证券的事件", category="FORMAT")
            rows.extend(current)
            pages.append(payload)
            references.append(url)
            if page >= expected_pages:
                break
        if len(rows) != count:
            raise ProviderError("东方财富事件分页不完整", retryable=True)
        return {
            "fields": sorted({key for row in rows for key in row}), "rows": rows,
            "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "provider": self.provider_id, "adapter_version": self.adapter_version,
            "event_tax_capabilities": dict(self.event_tax_capabilities),
            "source_urls": references, "raw_pages": pages,
            "coverage": {"code": code, "complete": True,
                         "scope": "reported_cash_bonus_capital_distributions",
                         "complex_actions_complete": False},
        }


EastmoneyProvider = EastmoneyEventProvider
