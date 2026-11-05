import copy
import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from china_stock_sim.market.providers.baostock import ProviderError
from china_stock_sim.market.providers.http import (
    HTTPProviderError,
    _download,
    fetch_json,
    retry_after,
)
from china_stock_sim.market.providers.tencent import TencentProvider, normalize_prices

PARAMS = {"code": "sh.600000", "start_date": "2026-10-26", "end_date": "2026-10-27"}
ROW = ["2026-10-27", "9.56", "9.82", "9.87", "9.53", "807530.00", {}, "0.24", "78932.23"]
URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=test"


def payload(rows=None, symbol="sh600000"):
    return {
        "code": 0,
        "data": {symbol: {"day": rows if rows is not None else [copy.deepcopy(ROW)]}},
    }


def test_real_shape_stock_units_and_unknown_fields():
    row = normalize_prices(payload(), PARAMS)[0]
    assert row["volume"] == "80753000"
    assert row["amount"] == "789322300.00"
    assert row["preclose"] is row["tradestatus"] is row["isST"] is None
    assert row["volume_raw"] == "807530.00"
    assert row["adjustflag"] == "3"


def test_shenzhen_main_board_does_not_inherit_old_index_prefix_unit_bug():
    row = ["2026-10-27", "11.27", "11.47", "11.55", "11.24", "1529292.00", {}, "0.79", "175040.42"]
    result = normalize_prices(payload([row], "sz000001"), {**PARAMS, "code": "sz.000001"})[0]
    assert result["volume"] == "152929200"
    assert result["amount"] == "1750404200.00"


@pytest.mark.parametrize("code", ["sh.000001", "sz.399001", "sh.000300"])
def test_indices_keep_unconfirmed_quantity_units_unknown(code):
    row = normalize_prices(payload(symbol=code.replace(".", "")), {**PARAMS, "code": code})[0]
    assert row["volume"] is row["amount"] is None
    assert row["volume_raw"] == ROW[5] and row["amount_raw"] == ROW[8]


def test_missing_amount_is_not_fabricated_and_dates_are_cropped():
    other = ["2026-09-01", *ROW[1:]]
    rows = normalize_prices(payload([other, ROW[:6]]), PARAMS)
    assert len(rows) == 1 and rows[0]["amount"] is None
    no_volume = [*ROW[:5], "", *ROW[6:]]
    row = normalize_prices(payload([no_volume]), PARAMS)[0]
    assert row["volume"] is None and row["amount"] == "789322300.00"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.__setitem__(1, "NaN"),
        lambda row: row.__setitem__(2, "999"),
        lambda row: row.__setitem__(5, "80753000"),
        lambda row: row.__setitem__(8, "NaN"),
        lambda row: row.__setitem__(0, "2026-02-30"),
    ],
)
def test_bad_prices_or_quantity_scale_rejected(mutation):
    row = copy.deepcopy(ROW)
    mutation(row)
    with pytest.raises(ProviderError) as error:
        normalize_prices(payload([row]), PARAMS)
    assert error.value.category == "FORMAT"


def test_duplicate_and_adjusted_only_data_rejected():
    with pytest.raises(ProviderError, match="重复"):
        normalize_prices(payload([ROW, ROW]), PARAMS)
    adjusted = {"code": 0, "data": {"sh600000": {"qfqday": [ROW]}}}
    with pytest.raises(ProviderError, match="不复权"):
        normalize_prices(adjusted, PARAMS)
    with pytest.raises(ProviderError) as malformed:
        normalize_prices({"data": {}}, PARAMS)
    assert malformed.value.category == "FORMAT"


def test_provider_query_scope_and_raw_evidence(monkeypatch):
    calls = []

    def source(url, **kwargs):
        calls.append((url, kwargs))
        return {
            "payload": payload(),
            "source_url": url,
            "sha256": "source-digest",
            "raw_text": json.dumps(payload()),
            "fetched_at": "2026-10-28T20:00:00+08:00",
        }

    monkeypatch.setattr("china_stock_sim.market.providers.tencent.fetch_json", source)
    result = TencentProvider(timeout=12).fetch("prices", {**PARAMS, "code": "SH600000"})
    assert result["provider"] == "tencent"
    assert result["capabilities"]["trading_status"] is False
    assert result["source_raw_text"] and result["source_sha256"] == "source-digest"
    assert result["quantity_precision"] == {
        "volume_shares": "100",
        "amount_yuan": "100",
        "basis": "source_rounded",
    }
    assert calls[0][1] == {"timeout": 12}
    with pytest.raises(ProviderError):
        TencentProvider().fetch("history", PARAMS)
    with pytest.raises(ProviderError):
        TencentProvider().fetch("prices", {**PARAMS, "start_date": "2020-01-01"})
    with pytest.raises(ProviderError):
        TencentProvider().fetch("prices", {**PARAMS, "code": "SH688001"})
    assert len(calls) == 1


def test_retry_after_integer_date_and_invalid():
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    assert retry_after("90", now) == 90
    assert retry_after("Wed, 28 Oct 2026 12:02:00 GMT", now) == 120
    assert retry_after("Wed, 28 Oct 2026 11:00:00 GMT", now) == 0
    assert retry_after("nonsense", now) is None


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (403, False)])
def test_http_status_keeps_server_cooldown(monkeypatch, status, retryable):
    def opened(*args, **kwargs):
        raise HTTPError(URL, status, "error", {"Retry-After": "120"}, None)

    monkeypatch.setattr(
        "china_stock_sim.market.providers.http.build_opener",
        lambda *args: SimpleNamespace(open=opened),
    )
    with pytest.raises(HTTPProviderError) as error:
        _download(URL, 5)
    assert error.value.retryable is retryable
    assert error.value.retry_after_seconds == 120
    assert error.value.status_code == status
    assert error.value.category == ("NETWORK" if retryable else "CONFIGURATION")


def test_process_deadline_and_retry_metadata(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(HTTPProviderError, match="总超时") as error:
        fetch_json(URL, timeout=0.01)
    assert error.value.retryable
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": "limited",
                    "retryable": True,
                    "retry_after_seconds": 600,
                    "status_code": 429,
                }
            ),
        ),
    )
    with pytest.raises(HTTPProviderError) as error:
        fetch_json(URL)
    assert error.value.retry_after_seconds == 600


@pytest.mark.parametrize("timeout", [2.5, 45, 120])
def test_http_open_uses_remaining_budget_without_hidden_socket_cap(monkeypatch, timeout):
    captured = []

    def opened(request, *, timeout):
        captured.append(timeout)
        return nullcontext(SimpleNamespace(status=200, read=lambda size: b"{}"))

    monkeypatch.setattr(
        "china_stock_sim.market.providers.http.build_opener",
        lambda *args: SimpleNamespace(open=opened),
    )
    assert _download(URL, timeout)["payload"] == {}
    assert captured == [timeout]


def test_http_worker_and_process_receive_the_same_total_deadline(monkeypatch):
    captured = []

    def launch(*args, **kwargs):
        captured.append((json.loads(kwargs["input"]), kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "data": {}}))

    monkeypatch.setattr(subprocess, "run", launch)
    assert fetch_json(URL, timeout=45) == {}
    assert captured == [({"url": URL, "timeout": 45}, 45)]


def test_process_deadline_kills_and_reaps_a_trickling_worker_without_network(monkeypatch, tmp_path):
    original_run = subprocess.run
    pid_file = tmp_path / "isolated-worker.pid"
    child = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True:\n"
        "    sys.stdout.write(' ')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.02)\n"
    )

    def launch(*args, **kwargs):
        # Exercise the real subprocess timeout against a local fake worker only.
        return original_run([sys.executable, "-c", child, str(pid_file)], **kwargs)

    monkeypatch.setattr(subprocess, "run", launch)
    started = time.monotonic()
    with pytest.raises(HTTPProviderError, match="总超时") as error:
        fetch_json(URL, timeout=1)
    assert error.value.retryable and time.monotonic() - started < 5
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_http_rejects_unapproved_host_before_launch(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("no subprocess"))
    with pytest.raises(HTTPProviderError):
        fetch_json("https://example.com/prices")


def test_shared_transport_allows_exact_eastmoney_public_endpoint(monkeypatch):
    captured = []

    def launch(*args, **kwargs):
        captured.append(json.loads(kwargs["input"])["url"])
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"ok": True, "data": {"fixture": True}})
        )

    monkeypatch.setattr(subprocess, "run", launch)
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_SHAREBONUS_DET"
    assert fetch_json(url) == {"fixture": True}
    assert captured == [url]
    with pytest.raises(HTTPProviderError):
        fetch_json("https://datacenter-web.eastmoney.com/private")
    assert captured == [url]


def test_http_bad_json_is_format_not_configuration_or_network(monkeypatch):
    response = SimpleNamespace(status=200, read=lambda size: b"<html>source changed</html>")
    monkeypatch.setattr(
        "china_stock_sim.market.providers.http.build_opener",
        lambda *args: SimpleNamespace(open=lambda *a, **k: nullcontext(response)),
    )
    with pytest.raises(HTTPProviderError) as error:
        _download(URL, 5)
    assert error.value.category == "FORMAT" and not error.value.retryable


def test_http_preserves_explicit_format_category_across_process(monkeypatch):
    response = {"ok": False, "error": "invalid JSON", "retryable": False, "category": "FORMAT"}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=json.dumps(response)),
    )
    with pytest.raises(HTTPProviderError) as error:
        fetch_json(URL)
    assert error.value.category == "FORMAT"
