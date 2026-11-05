"""Validate unadjusted daily bars before they can enter the local cache.

The only missing-value repair is BaoStock's documented suspended-day zero
volume/amount case. Raw quantities are retained so the repair remains auditable.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation


class DataQualityError(ValueError):
    """A source response cannot safely be used as daily market data."""


class DataUnavailableError(DataQualityError):
    """A requested market observation has not been supplied yet."""


PRICE_ROUNDING_TOLERANCE = Decimal("0.01")


def normalize_code(value: object) -> str:
    """Accept SH600000 or sh.600000 without guessing the exchange."""
    if not isinstance(value, str):
        raise DataQualityError("证券代码必须包含交易所")
    match = re.fullmatch(r"(SH|SZ|BJ)\.?(\d{6})", value.strip().upper())
    if match is None:
        raise DataQualityError(f"无效证券代码: {value!r}")
    return "".join(match.groups())


def normalize_date(value: object) -> str:
    if not isinstance(value, str):
        raise DataQualityError("日期必须为 YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise DataQualityError(f"无效日期: {value!r}") from exc
    if result.isoformat() != value:
        raise DataQualityError(f"日期不是 YYYY-MM-DD: {value!r}")
    return value


def validate_sessions(sessions: list[str]) -> list[str]:
    dates = [normalize_date(value) for value in sessions]
    if not dates or dates != sorted(set(dates)):
        raise DataQualityError("交易日窗口必须非空、升序且不重复")
    return dates


def _decimal(value: object, field: str, *, blank_allowed: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if blank_allowed:
            return None
        raise DataQualityError(f"{field} 不允许空值")
    if isinstance(value, bool):
        raise DataQualityError(f"{field} 必须是数值")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DataQualityError(f"{field} 不是合法数值: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise DataQualityError(f"{field} 必须是有限非负数")
    return number


def _flag(value: object, field: str, choices: set[int]) -> int:
    # Source flag fields are categorical: don't silently turn unknown/empty into 0.
    if isinstance(value, bool) or str(value).strip() not in {str(item) for item in choices}:
        raise DataQualityError(f"{field} 未知或无效: {value!r}")
    return int(str(value).strip())


def normalize_bars(
    rows: list[dict],
    expected_code: str | None = None,
    allowed_dates: list[str] | None = None,
) -> list[dict]:
    """Return strict, JSON-serializable bars, sorted by security and session.

    Prices and amount use decimal strings; volume and categorical flags are
    integers. A bad row rejects the response rather than creating a partial cache.
    """
    expected = normalize_code(expected_code) if expected_code is not None else None
    allowed = set(validate_sessions(allowed_dates)) if allowed_dates is not None else None
    required = {
        "code",
        "date",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "tradestatus",
        "isST",
        "adjustflag",
    }
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            missing = sorted(required - row.keys()) if isinstance(row, dict) else sorted(required)
            raise DataQualityError(f"日线必要字段缺失: {', '.join(missing)}")
        code, session = normalize_code(row["code"]), normalize_date(row["date"])
        if expected is not None and code != expected:
            raise DataQualityError(f"证券代码不符: 期望 {expected}, 返回 {code}")
        if allowed is not None and session not in allowed:
            raise DataQualityError(f"交易日越界: {code} {session}")
        identity = (code, session)
        if identity in seen:
            raise DataQualityError(f"重复证券交易日: {code} {session}")
        seen.add(identity)
        trade = _flag(row["tradestatus"], "tradestatus", {0, 1})
        is_st = _flag(row["isST"], "isST", {0, 1})
        adjustment = _flag(row["adjustflag"], "adjustflag", {3})
        prices = {
            field: _decimal(row[field], field)
            for field in ("open", "high", "low", "close", "preclose")
        }
        if any(price <= 0 for price in prices.values()):
            raise DataQualityError(f"价格必须大于零: {code} {session}")
        if not (
            prices["low"]
            <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"])
            <= prices["high"]
        ):
            raise DataQualityError(f"OHLC 价格关系非法: {code} {session}")
        volume = _decimal(row["volume"], "volume", blank_allowed=trade == 0)
        amount = _decimal(row["amount"], "amount", blank_allowed=trade == 0)
        halted_zero = (
            trade == 0
            and all(
                prices[field] == prices["preclose"] for field in ("open", "high", "low", "close")
            )
            and volume in (None, Decimal(0))
            and amount in (None, Decimal(0))
        )
        if trade == 0 and not halted_zero:
            raise DataQualityError(f"停牌数据与价格或量额不符: {code} {session}")
        changed = halted_zero and (volume is None or amount is None)
        volume = Decimal(0) if volume is None else volume
        amount = Decimal(0) if amount is None else amount
        if volume != volume.to_integral_value():
            raise DataQualityError(f"volume 必须为整数股数: {code} {session}")
        if (volume == 0) != (amount == 0):
            raise DataQualityError(f"成交量与成交额的零值状态不一致: {code} {session}")
        if volume > 0:
            average = amount / volume
            # Prices can be rounded to the A-share quotation tick. One cent per
            # share also covers source amount rounding; it is not a unit guess.
            if not (
                prices["low"] - PRICE_ROUNDING_TOLERANCE
                <= average
                <= prices["high"] + PRICE_ROUNDING_TOLERANCE
            ):
                raise DataQualityError(f"成交均价超出当日高低价及一分钱容差: {code} {session}")
        result.append(
            {
                "code": code,
                "date": session,
                **{field: format(value, "f") for field, value in prices.items()},
                "volume": int(volume),
                "amount": format(amount, "f"),
                "tradestatus": trade,
                "isST": is_st,
                "adjustflag": adjustment,
                "volume_raw_bao": row.get("volume_raw_bao", row["volume"]),
                "amount_raw_bao": row.get("amount_raw_bao", row["amount"]),
                "halt_normalized": changed or (halted_zero and row.get("halt_normalized") is True),
            }
        )
    return sorted(result, key=lambda row: (row["code"], row["date"]))


def compare_overlapping_bars(left: list[dict], right: list[dict]) -> list[dict]:
    """Compare shared security/sessions numerically, preserving both observations."""
    fields = (
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "tradestatus",
        "isST",
        "adjustflag",
    )
    first = {(row["code"], row["date"]): row for row in left}
    conflicts = []
    for row in right:
        identity = (row["code"], row["date"])
        previous = first.get(identity)
        if previous is None:
            continue
        differences = {
            field: {"batch": previous[field], "history": row[field]}
            for field in fields
            if Decimal(str(previous[field])) != Decimal(str(row[field]))
        }
        if differences:
            conflicts.append({"code": row["code"], "date": row["date"], "fields": differences})
    return sorted(conflicts, key=lambda row: (row["code"], row["date"]))


def validate_window(bars: list[dict], code: str, sessions: list[str]) -> dict:
    """A complete window contains exactly one valid bar for each requested day."""
    dates = validate_sessions(sessions)
    checked = normalize_bars(bars, expected_code=code, allowed_dates=dates)
    present = {row["date"] for row in checked}
    missing = [session for session in dates if session not in present]
    return {
        "complete": not missing,
        "missing_dates": missing,
        "expected_rows": len(dates),
        "actual_rows": len(checked),
    }
