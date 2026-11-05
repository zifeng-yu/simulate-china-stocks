"""Tencent unadjusted daily OHLC; explicitly incomplete trading-state capability.

Public endpoint also used by AKShare stock_zh_a_hist_tx. This adapter deliberately
does not import the old project's SDK or its security-prefix volume patch.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from .baostock import ProviderError, validate_request
from .http import fetch_json

INDEX_CODES = {"sh.000001", "sz.399001", "sh.000300"}
PRICE_FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "tradestatus",
    "isST",
    "volume_raw",
    "amount_raw",
]


def _number(value, field, *, positive=False):
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"腾讯 {field} 必须为数值字符串", category="FORMAT")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ProviderError(f"腾讯 {field} 不是有效数值", category="FORMAT") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ProviderError(f"腾讯 {field} 数值无效", category="FORMAT")
    return number


def normalize_prices(payload, params):
    """Map supported raw values, preserving unknown fields as None rather than zero."""
    code = params["code"]
    symbol = code.replace(".", "")
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise ProviderError("腾讯响应缺少有效状态字段", category="FORMAT")
    if payload["code"] != 0:
        raise ProviderError("腾讯响应未明确成功", retryable=True)
    try:
        source = payload["data"][symbol]
    except (KeyError, TypeError) as exc:
        raise ProviderError("腾讯响应缺少请求证券", category="FORMAT") from exc
    # Never silently fall back to qfqday/hfqday: execution needs raw prices.
    if not isinstance(source, dict) or not isinstance(source.get("day"), list):
        raise ProviderError("腾讯响应缺少不复权 day 数据", category="FORMAT")
    raw_rows = source["day"]
    output, seen = [], set()
    for values in raw_rows:
        if not isinstance(values, list) or len(values) < 6:
            raise ProviderError("腾讯日线字段数量不足", category="FORMAT")
        session = values[0]
        try:
            if not isinstance(session, str) or date.fromisoformat(session).isoformat() != session:
                raise ValueError
        except ValueError as exc:
            raise ProviderError("腾讯日线日期无效", category="FORMAT") from exc
        if not params["start_date"] <= session <= params["end_date"]:
            continue  # Tencent can return the last 640 bars despite the requested start.
        if session in seen:
            raise ProviderError(f"腾讯日线日期重复: {session}", category="FORMAT")
        seen.add(session)
        prices = {
            field: _number(values[index], field, positive=True)
            for field, index in (("open", 1), ("close", 2), ("high", 3), ("low", 4))
        }
        if (
            not prices["low"]
            <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"])
            <= prices["high"]
        ):
            raise ProviderError("腾讯 OHLC 关系无效", category="FORMAT")
        volume_raw = values[5] if values[5] not in ("", None) else None
        amount_raw = values[8] if len(values) > 8 and values[8] not in ("", None) else None
        if any(
            value is not None and not isinstance(value, str) for value in (volume_raw, amount_raw)
        ):
            raise ProviderError("腾讯原始成交量或成交额字段类型异常", category="FORMAT")
        volume, amount = None, None
        if code not in INDEX_CODES:
            # Main-board share volumes are lots of 100; turnover is ten-thousand CNY.
            # Unlike the previous project's prefix patch, SZ000xxx is a stock here.
            shares = None
            if volume_raw is not None:
                shares = _number(volume_raw, "volume") * 100
                if shares != shares.to_integral_value():
                    raise ProviderError("腾讯成交量转换后不是整数股", category="FORMAT")
                volume = str(int(shares))
            if amount_raw is not None:
                yuan = _number(amount_raw, "amount") * 10000
                amount = format(yuan, "f")
                # Quantization: source amount is rounded to 0.01 ten-thousand yuan.
                if (
                    shares is not None
                    and shares > 0
                    and not prices["low"] * shares - 100 <= yuan <= prices["high"] * shares + 100
                ):
                    raise ProviderError(
                        "腾讯成交量/成交额单位与当日价格范围不一致", category="FORMAT"
                    )
                if shares == 0 and yuan != 0:
                    raise ProviderError("腾讯零成交量与成交额不一致", category="FORMAT")
        output.append(
            {
                "date": session,
                "code": code,
                **{key: format(value, "f") for key, value in prices.items()},
                "preclose": None,
                "volume": volume,
                "amount": amount,
                "adjustflag": "3",
                "tradestatus": None,
                "isST": None,
                "volume_raw": volume_raw,
                "amount_raw": amount_raw,
            }
        )
    return sorted(output, key=lambda row: row["date"])


class TencentProvider:
    provider_id = "tencent"
    adapter_version = "1"
    supported_queries = frozenset({"prices"})

    def __init__(self, timeout=20):
        self.timeout = timeout

    def fetch(self, query: str, params: dict) -> dict:
        if query != "prices":
            raise ProviderError("腾讯适配器只提供 prices，不提供完整历史交易状态或权益")
        params = validate_request("history", params)
        code = params["code"]
        if (
            code not in INDEX_CODES
            and re.fullmatch(r"(?:sh\.60[0135]|sz\.00[0123])\d{3}", code) is None
        ):
            raise ProviderError("腾讯适配器当前只验证沪深主板普通股及配置的三个大盘指数")
        if (
            date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])
        ).days > 550:
            raise ProviderError("腾讯单次历史价格窗口最多 550 个日历日，避免源 640 行上限截断")
        url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?" + urlencode(
            {
                "param": f"{code.replace('.', '')},day,{params['start_date']},{params['end_date']},640,"
            }
        )
        response = fetch_json(url, timeout=self.timeout)
        rows = normalize_prices(response["payload"], params)
        return {
            "fields": PRICE_FIELDS.copy(),
            "rows": rows,
            "fetched_at": response["fetched_at"],
            "provider": self.provider_id,
            "adapter_version": self.adapter_version,
            "price_basis": "unadjusted",
            "asset_type": "index" if code in INDEX_CODES else "stock",
            "capabilities": {
                "ohlc": True,
                "preclose": False,
                "trading_status": False,
                "st_status": False,
                "volume_shares": bool(rows)
                and code not in INDEX_CODES
                and all(row["volume"] is not None for row in rows),
                "amount_yuan": bool(rows)
                and code not in INDEX_CODES
                and all(row["amount"] is not None for row in rows),
            },
            # Live comparison against BaoStock confirms the endpoint rounds quantities.
            # Converted units are correct but must not be compared as exact-share values.
            "quantity_precision": {
                "volume_shares": "100" if code not in INDEX_CODES else None,
                "amount_yuan": "100" if code not in INDEX_CODES else None,
                "basis": "source_rounded" if code not in INDEX_CODES else "unverified_index_units",
            },
            "source_url": response["source_url"],
            "source_sha256": response["sha256"],
            "source_raw_text": response["raw_text"],
        }
