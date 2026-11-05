"""Offline provider contract tests: no SDK installation or network required."""

import builtins
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from china_stock_sim.market.providers import BaoStockProvider, ProviderError, worker
from china_stock_sim.market.providers.baostock import HISTORY_FIELDS, validate_request


class Result:
    def __init__(self, *, rows=None, error="0", final_error="0"):
        self.fields = ["date", "code", "close"]
        self.error_code = error
        self.error_msg = "stub SDK error"
        self._rows = iter(rows if rows is not None else [["2026-10-27", "sh.600000", "10.20"]])
        self._final_error = final_error

    def next(self):
        try:
            self._current = next(self._rows)
            return True
        except StopIteration:
            self.error_code = self._final_error
            return False

    def get_row_data(self):
        return self._current


class SDK:
    def __init__(self, result=None, login_error="0", logout_error="0"):
        self.result = result if result is not None else Result()
        self.login_error = login_error
        self.logout_error = logout_error
        self.calls = []

    def login(self):
        self.calls.append(("login", {}))
        return Result(error=self.login_error)

    def logout(self):
        self.calls.append(("logout", {}))
        return Result(error=self.logout_error)

    def _query(self, name, params):
        self.calls.append((name, params))
        return self.result

    def query_trade_dates(self, **params):
        return self._query("query_trade_dates", params)

    def query_stock_basic(self, **params):
        return self._query("query_stock_basic", params)

    def query_daily_history_k_AStock(self, **params):
        return self._query("query_daily_history_k_AStock", params)

    def query_history_k_data_plus(self, **params):
        return self._query("query_history_k_data_plus", params)

    def query_adjust_factor(self, **params):
        return self._query("query_adjust_factor", params)

    def query_dividend_data(self, **params):
        return self._query("query_dividend_data", params)


DATES = {"start_date": "2026-01-01", "end_date": "2026-10-27"}


@pytest.mark.parametrize(
    ("query", "params", "method", "expected"),
    [
        ("calendar", DATES, "query_trade_dates", DATES),
        ("securities", {}, "query_stock_basic", {}),
        (
            "session_bars",
            {"date": "2026-10-27"},
            "query_daily_history_k_AStock",
            {"date": "2026-10-27"},
        ),
        (
            "history",
            {"code": "SH600000", **DATES},
            "query_history_k_data_plus",
            {
                "code": "sh.600000",
                **DATES,
                "fields": HISTORY_FIELDS,
                "frequency": "d",
                "adjustflag": "3",
            },
        ),
        (
            "adjustments",
            {"code": "sz.000001", **DATES},
            "query_adjust_factor",
            {"code": "sz.000001", **DATES},
        ),
        (
            "dividends",
            {"code": "sh.600000", "year": 2026},
            "query_dividend_data",
            {"code": "sh.600000", "year": "2026", "yearType": "operate"},
        ),
    ],
)
def test_explicit_dispatch_and_raw_result(query, params, method, expected):
    sdk = SDK()
    before = datetime.now().astimezone()
    result = worker.execute(query, params, sdk)
    after = datetime.now().astimezone()
    assert sdk.calls == [("login", {}), (method, expected), ("logout", {})]
    assert result["rows"] == [{"date": "2026-10-27", "code": "sh.600000", "close": "10.20"}]
    assert result["fields"] == ["date", "code", "close"]
    assert result["provider"] == "baostock"
    assert result["adapter_version"] == "1"
    timestamp = datetime.fromisoformat(result["fetched_at"])
    assert timestamp.utcoffset() == timedelta(hours=8)
    assert before <= timestamp <= after


@pytest.mark.parametrize(
    ("query", "params"),
    [
        ("__import__", {}),
        ("query_trade_dates", DATES),
        ([], {}),
        ("securities", {"code": "sh.600000"}),
        ("calendar", {"start_date": "2026-02-30", "end_date": "2026-10-27"}),
        ("calendar", {"start_date": "2026-1-1", "end_date": "2026-10-27"}),
        ("calendar", {"start_date": "2026-10-28", "end_date": "2026-10-27"}),
        ("session_bars", {"date": None}),
        ("history", {"code": "600000", **DATES}),
        ("history", {"code": "sh.600000", **DATES, "adjustflag": "1"}),
        ("history", {"code": 600000, **DATES}),
        ("dividends", {"code": "sh.600000", "year": True}),
        ("dividends", {"code": "sh.600000", "year": 26}),
        ("dividends", {"code": "sh.600000", "year": 2026, "yearType": "report"}),
        ("securities", []),
    ],
)
def test_invalid_request_is_nonretryable_before_subprocess(monkeypatch, query, params):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request must not create a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ProviderError) as error:
        BaoStockProvider().fetch(query, params)
    assert not error.value.retryable


def test_dividend_basis_is_fixed_and_request_is_not_mutated():
    params = {"code": "SH600000", "year": 2026, "yearType": "operate"}
    assert validate_request("dividends", params) == {"code": "sh.600000", "year": "2026"}
    assert params == {"code": "SH600000", "year": 2026, "yearType": "operate"}


@pytest.mark.parametrize("stage", ["initial", "final"])
def test_server_error_discards_partial_data_and_logs_out(stage):
    result = Result(
        error="10002007" if stage == "initial" else "0",
        final_error="10002007" if stage == "final" else "0",
    )
    sdk = SDK(result)
    with pytest.raises(ProviderError, match="10002007") as error:
        worker.execute("securities", {}, sdk)
    assert error.value.retryable
    assert sdk.calls[-1][0] == "logout"


def test_parameter_sdk_error_is_not_retried():
    with pytest.raises(ProviderError) as error:
        worker.execute("securities", {}, SDK(Result(error="10004001")))
    assert not error.value.retryable


def test_login_and_logout_failures_are_not_accepted():
    sdk = SDK(login_error="10002002")
    with pytest.raises(ProviderError, match="登录") as error:
        worker.execute("securities", {}, sdk)
    assert error.value.retryable
    assert sdk.calls == [("login", {})]
    with pytest.raises(ProviderError, match="退出"):
        worker.execute("securities", {}, SDK(logout_error="10002007"))


@pytest.mark.parametrize("logout_raises", [False, True])
def test_logout_does_not_replace_original_query_error(logout_raises):
    sdk = SDK(Result(error="10004001"), logout_error="10001010")
    if logout_raises:
        sdk.logout = lambda: (_ for _ in ()).throw(ConnectionError("logout failed"))
    with pytest.raises(ProviderError, match="10004001") as error:
        worker.execute("securities", {}, sdk)
    assert not error.value.retryable


@pytest.mark.parametrize("stage,code,retryable", [
    ("logout", "10001010", True),
    ("login", "10001010", False),
    ("query", "10001010", False),
    ("final_page", "10001010", False),
    ("logout", "10004001", False),
    ("logout", "unknown", False),
])
def test_only_logout_specific_failure_gets_temporary_classification(stage, code, retryable):
    sdk = SDK(
        Result(error=code if stage == "query" else "0",
               final_error=code if stage == "final_page" else "0"),
        login_error=code if stage == "login" else "0",
        logout_error=code if stage == "logout" else "0",
    )
    with pytest.raises(ProviderError, match=code) as error:
        worker.execute("securities", {}, sdk)
    assert error.value.retryable is retryable
    assert error.value.category == ("NETWORK" if retryable else "CONFIGURATION")


def test_logout_failure_stays_rejected_and_retryable_through_worker_provider(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "baostock", SDK(logout_error="10001010"))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"query":"securities","params":{}}'))
    monkeypatch.setattr(worker.socket, "setdefaulttimeout", lambda value: None)
    assert worker.main() == 1
    output = capsys.readouterr().out
    response = json.loads(output.removeprefix("RESULT="))
    assert response["ok"] is False and "data" not in response
    assert response["retryable"] is True and response["category"] == "NETWORK"
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=output, stderr=""),
    )
    with pytest.raises(ProviderError, match="退出 失败 10001010") as error:
        BaoStockProvider().fetch("securities", {})
    assert error.value.retryable and error.value.category == "NETWORK"


def test_malformed_sdk_row_is_rejected():
    with pytest.raises(ProviderError, match="不完整") as error:
        worker.execute("securities", {}, SDK(Result(rows=[["2026-10-27", "sh.600000"]])))
    assert error.value.category == "FORMAT"


def test_successful_empty_results_are_allowed():
    assert worker.execute("securities", {}, SDK(Result(rows=[])))["rows"] == []


def _payload():
    return {
        "fields": ["code"],
        "rows": [{"code": "sh.600000"}],
        "fetched_at": "2026-10-28T12:30:00+08:00",
        "provider": "baostock",
        "adapter_version": "1",
    }


def test_fetch_uses_stdin_and_subprocess_deadline(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="login success!\nRESULT="
            + json.dumps({"ok": True, "data": _payload()})
            + "\nlogout success!\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert BaoStockProvider().fetch("securities", {}) == _payload()
    assert captured["command"] == [sys.executable, "-m", "china_stock_sim.market.providers.worker"]
    assert json.loads(captured["input"]) == {"query": "securities", "params": {}}
    assert captured["timeout"] == 45
    assert captured["capture_output"] is True
    assert captured["encoding"] == "utf-8"


def test_fetch_timeout_is_retryable(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ProviderError, match="子进程已终止") as error:
        BaoStockProvider(timeout=0.1).fetch("securities", {})
    assert error.value.retryable


@pytest.mark.parametrize(
    ("stdout", "code", "retryable"),
    [
        ("unstructured SDK error", 1, True),
        ('RESULT={"ok":true,"data":{}}', 0, False),
        ('RESULT={"ok":false,"error":"network","retryable":true}', 1, True),
        ('RESULT={"ok":false,"error":"missing SDK","retryable":false}', 1, False),
        ("RESULT=broken", 0, False),
        ('RESULT={"ok":"true"}', 0, False),
        ("RESULT=" + json.dumps({"ok": True, "data": _payload()}), 1, True),
    ],
)
def test_process_failures_are_not_success(monkeypatch, stdout, code, retryable):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=stdout, stderr=""),
    )
    with pytest.raises(ProviderError) as error:
        BaoStockProvider().fetch("securities", {})
    assert error.value.retryable is retryable


def test_main_missing_sdk_is_nonretryable(monkeypatch, capsys):
    original_import = builtins.__import__

    def missing_sdk(name, *args, **kwargs):
        if name == "baostock":
            raise ModuleNotFoundError("baostock")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_sdk)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"query":"securities","params":{}}'))
    monkeypatch.setattr(worker.socket, "setdefaulttimeout", lambda value: None)
    assert worker.main() == 1
    response = json.loads(capsys.readouterr().out.removeprefix("RESULT="))
    assert response["ok"] is False
    assert response["retryable"] is False
    assert "SDK" in response["error"]


def test_main_network_failure_and_socket_timeout(monkeypatch, capsys):
    sdk = SDK()
    sdk.login = lambda: (_ for _ in ()).throw(ConnectionError("offline stub"))
    monkeypatch.setitem(sys.modules, "baostock", sdk)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"query":"securities","params":{}}'))
    timeouts = []
    monkeypatch.setattr(worker.socket, "setdefaulttimeout", timeouts.append)
    assert worker.main() == 1
    response = json.loads(capsys.readouterr().out.removeprefix("RESULT="))
    assert response["retryable"] is True
    assert timeouts == [10]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "45"])
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ValueError):
        BaoStockProvider(timeout=timeout)


def test_provider_error_category_defaults_and_format_worker_roundtrip(monkeypatch, capsys):
    assert ProviderError("offline", retryable=True).category == "NETWORK"
    assert ProviderError("missing SDK").category == "CONFIGURATION"
    sdk = SDK(Result(rows=[["incomplete"]]))
    monkeypatch.setitem(sys.modules, "baostock", sdk)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"query":"securities","params":{}}'))
    monkeypatch.setattr(worker.socket, "setdefaulttimeout", lambda value: None)
    assert worker.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output.removeprefix("RESULT="))["category"] == "FORMAT"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=output, stderr=""),
    )
    with pytest.raises(ProviderError) as error:
        BaoStockProvider().fetch("securities", {})
    assert error.value.category == "FORMAT" and not error.value.retryable
