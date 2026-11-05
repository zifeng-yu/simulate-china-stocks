"""Private one-request subprocess entrypoint; never loads project configuration."""

import json
import socket
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .baostock import HISTORY_FIELDS, RESULT_PREFIX, ProviderError, validate_request

_NETWORK_CODES = {f"1000200{number}" for number in range(1, 9)}


def _check_status(result, phase: str) -> None:
    code = str(result.error_code)
    if code != "0":
        raise ProviderError(
            f"BaoStock {phase} 失败 {code}: {result.error_msg}",
            retryable=code in _NETWORK_CODES or (phase == "退出" and code == "10001010"),
        )


def _read_rows(result) -> tuple[list[str], list[dict[str, str]]]:
    """Keep the old probe_worker.bsframe final pagination check, without pandas."""
    _check_status(result, "查询")
    fields = list(result.fields)
    if not all(isinstance(field, str) and field for field in fields) or len(set(fields)) != len(
        fields
    ):
        raise ProviderError("BaoStock 返回了无效字段", category="FORMAT")
    rows = []
    while result.next():
        _check_status(result, "分页")
        values = result.get_row_data()
        if len(values) != len(fields) or not all(isinstance(value, str) for value in values):
            raise ProviderError("BaoStock 返回了不完整记录", category="FORMAT")
        rows.append(dict(zip(fields, values, strict=True)))
    # A final next() can return False because the server failed, not because EOF.
    _check_status(result, "分页结束")
    return fields, rows


def execute(query: str, params: dict, sdk) -> dict:
    """Explicit dispatch only; sdk is injected in offline tests."""
    params = validate_request(query, params)
    _check_status(sdk.login(), "登录")
    try:
        if query == "calendar":
            result = sdk.query_trade_dates(**params)
        elif query == "securities":
            result = sdk.query_stock_basic()
        elif query == "session_bars":
            result = sdk.query_daily_history_k_AStock(**params)
        elif query == "history":
            result = sdk.query_history_k_data_plus(
                **params, fields=HISTORY_FIELDS, frequency="d", adjustflag="3"
            )
        elif query == "adjustments":
            result = sdk.query_adjust_factor(**params)
        elif query == "dividends":
            result = sdk.query_dividend_data(**params, yearType="operate")
        else:  # validate_request already rejects this, retaining a closed dispatcher.
            raise ProviderError("不支持的数据查询")
        fields, rows = _read_rows(result)
    except BaseException:
        # Preserve the original failure if logout also fails. Process exit closes sockets.
        try:
            sdk.logout()
        except Exception:
            pass
        raise
    _check_status(sdk.logout(), "退出")
    return {
        "fields": fields,
        "rows": rows,
        "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "provider": "baostock",
        "adapter_version": "1",
    }


def main() -> int:
    socket.setdefaulttimeout(10)
    try:
        # Requests contain at most a handful of scalar parameters.
        raw = sys.stdin.read(16385)
        if len(raw) > 16384:
            raise ProviderError("数据查询请求过大")
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("数据查询请求不是有效 JSON") from exc
        if not isinstance(request, dict) or set(request) != {"query", "params"}:
            raise ProviderError("数据查询请求必须仅含 query 和 params")
        params = validate_request(request["query"], request["params"])
        try:
            import baostock as sdk
        except ImportError as exc:
            raise ProviderError("BaoStock SDK 未安装或无法导入，请先安装项目依赖") from exc
        data = execute(request["query"], params, sdk)
        response = {"ok": True, "data": data}
    except ProviderError as exc:
        response = {
            "ok": False,
            "error": str(exc),
            "retryable": exc.retryable,
            "category": exc.category,
        }
    except (ConnectionError, TimeoutError, OSError, EOFError) as exc:
        response = {
            "ok": False,
            "error": f"BaoStock 网络请求失败: {exc}",
            "retryable": True,
            "category": "NETWORK",
        }
    except Exception as exc:
        response = {
            "ok": False,
            "error": f"BaoStock 适配器错误: {type(exc).__name__}: {exc}",
            "retryable": False,
            "category": "FORMAT"
            if isinstance(exc, (ValueError, TypeError, KeyError))
            else "CONFIGURATION",
        }
    print(RESULT_PREFIX + json.dumps(response, ensure_ascii=False), flush=True)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
