"""Build a small, deterministic pool from a current BaoStock metadata snapshot.

Board classification uses source security type plus the stated code scheme. It
is not an exchange-certified historical security master or corporate-action audit.
"""

import re
from collections import defaultdict
from decimal import Decimal

from china_stock_sim.market.quality import (
    DataQualityError,
    normalize_bars,
    normalize_code,
    normalize_date,
    validate_sessions,
    validate_window,
)

BOARD_BASIS = "BaoStock type=1 股票 + 沪深主板代码规则；非交易所官方板块字段"


def normalize_securities(rows: list[dict], as_of: str) -> list[dict]:
    """Keep excluded source rows and explicit reasons instead of guessing values."""
    snapshot_date = normalize_date(as_of)
    normalized = []
    seen = set()
    required = {"code", "code_name", "ipoDate", "outDate", "type", "status"}
    for row in rows:
        if not isinstance(row, dict):
            raise DataQualityError("证券元数据行必须为对象")
        reasons = []
        missing = sorted(required - row.keys())
        if missing:
            reasons.append("证券元数据字段缺失: " + ", ".join(missing))
        try:
            code = normalize_code(row.get("code"))
            valid_code = True
        except DataQualityError:
            code = str(row.get("code") or "")
            valid_code = False
            reasons.append("证券代码无效")
        if code in seen:
            raise DataQualityError(f"证券元数据代码重复: {code}")
        seen.add(code)
        name = row.get("code_name")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            reasons.append("证券名称缺失")
        security_type = row.get("type")
        if str(security_type) != "1":
            reasons.append("证券类型不是源明示股票(type=1)")
        status = row.get("status")
        source_status = int(str(status)) if str(status) in {"0", "1"} else None
        if source_status is None:
            reasons.append("上市状态未知或无效")
        elif source_status != 1:
            reasons.append("源标记为非上市状态")
        if re.fullmatch(r"SH60[0135]\d{3}", code):
            board = "沪市主板"
        elif re.fullmatch(r"SZ00[0123]\d{3}", code):
            board = "深市主板"
        else:
            board = None
            reasons.append("不在沪深主板普通 A 股代码范围")
        listed = None
        try:
            listed = normalize_date(row.get("ipoDate"))
        except DataQualityError:
            reasons.append("上市日期未知或无效")
        if listed is not None and listed > snapshot_date:
            reasons.append("截至快照日尚未上市")
        delisted = None
        raw_out = row.get("outDate")
        if not isinstance(raw_out, str):
            reasons.append("退市日期字段未知或无效")
        elif raw_out:
            try:
                delisted = normalize_date(raw_out)
            except DataQualityError:
                reasons.append("退市日期无效")
        if delisted is not None:
            if listed is not None and delisted < listed:
                reasons.append("退市日期早于上市日期")
            if delisted <= snapshot_date:
                reasons.append("截至快照日已退市")
        if "ST" in name.upper() or "退" in name:
            reasons.append("证券名称含风险警示或退市标记")
        eligibility_known = (
            not missing
            and valid_code
            and bool(name)
            and str(security_type) in {"1", "2", "3", "4", "5"}
            and source_status is not None
            and (str(security_type) != "1" or listed is not None)
            and isinstance(raw_out, str)
            and (raw_out == "" or delisted is not None)
            and not (delisted is not None and listed is not None and delisted < listed)
        )
        normalized.append(
            {
                "code": code,
                "name": name,
                "listed": listed,
                "delisted": delisted,
                "board": board,
                "board_basis": BOARD_BASIS,
                "security_type": "普通人民币A股" if str(security_type) == "1" and board else None,
                "source_type": security_type,
                "source_status": source_status,
                "as_of": snapshot_date,
                "metadata_scope": "current_snapshot",
                "corporate_actions_status": "pending_verification",
                "eligible_metadata": not reasons,
                "eligibility_known": eligibility_known,
                "exclusion_reason": "；".join(reasons),
            }
        )
    return sorted(normalized, key=lambda row: row["code"])


def select_pool(
    securities: list[dict], bars20: list[dict], sessions: list[str], max_size: int = 100
) -> dict:
    """Rank by 20-session mean turnover; never fill gaps or select on returns.

    ``ready`` here means a nonempty candidate pool, not readiness to trade or
    completed corporate-action verification. The caller still fetches 121 bars.
    """
    dates = validate_sessions(sessions)
    if len(dates) != 121:
        raise DataQualityError("首次准备股票池需要完整的 121 个交易日窗口")
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise DataQualityError("股票池数量必须为正整数")
    recent = dates[-20:]
    grouped = defaultdict(list)
    for bar in normalize_bars(bars20, allowed_dates=recent):
        grouped[bar["code"]].append(bar)
    candidates, excluded = [], []
    seen = set()
    for security in securities:
        code = security["code"]
        if code in seen:
            raise DataQualityError(f"证券元数据代码重复: {code}")
        seen.add(code)
        reason = ""
        details = {}
        if security.get("eligible_metadata") is not True:
            reason = security.get("exclusion_reason") or "证券元数据未通过校验"
        elif security.get("as_of") != dates[-1]:
            reason = "证券元数据快照日期与准备截止日期不符"
        elif security["listed"] > dates[0]:
            reason = "上市时间不足完整 121 个交易日窗口"
        else:
            window = validate_window(grouped[code], code, recent)
            if not window["complete"]:
                reason = "近 20 个交易日日线缺失"
                details["missing_dates"] = window["missing_dates"]
            elif grouped[code][-1]["isST"] != 0:
                reason = "当前日线标记为 ST"
            elif grouped[code][-1]["tradestatus"] != 1:
                reason = "当前停牌"
            else:
                mean = sum((Decimal(bar["amount"]) for bar in grouped[code]), Decimal(0)) / 20
                if mean <= 0:
                    reason = "近 20 个交易日平均成交额不大于零"
                else:
                    candidates.append(
                        {
                            "code": code,
                            "name": security["name"],
                            "listed": security["listed"],
                            "mean_amount20": format(mean, "f"),
                            "board": security["board"],
                            "board_basis": security["board_basis"],
                            "corporate_actions_status": "pending_verification",
                        }
                    )
        if reason:
            excluded.append(
                {"code": code, "name": security.get("name", ""), "reason": reason, **details}
            )
    candidates.sort(key=lambda row: (-Decimal(row["mean_amount20"]), row["code"]))
    selected = candidates[:max_size]
    for row in candidates[max_size:]:
        excluded.append(
            {"code": row["code"], "name": row["name"], "reason": f"成交额排名超出前 {max_size} 名"}
        )
    return {
        "selected": selected,
        "excluded": sorted(excluded, key=lambda row: row["code"]),
        "requested_size": max_size,
        "eligible_count": len(candidates),
        "selected_count": len(selected),
        "ready": bool(selected),
        "corporate_actions_status": "pending_verification",
    }
