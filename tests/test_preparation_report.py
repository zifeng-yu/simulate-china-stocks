import copy

from china_stock_sim.reporting.preparation import write_preparation_report


def test_empty_preparation_is_distinct_from_ledger(tmp_path):
    ledger = tmp_path / "reports/forward_current/index.html"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("existing ledger", encoding="utf-8")
    path = write_preparation_report(tmp_path, {"state": "NOT_STARTED"})
    html = path.read_text(encoding="utf-8")
    assert path == tmp_path / "reports/preparation/index.html"
    assert "尚未开始首次准备" in html
    assert "股票池尚未形成" in html
    assert 'href="../forward_current/index.html"' in html
    assert 'http-equiv="refresh" content="30"' in html
    assert "尚无请求记录" in html
    assert ledger.read_text(encoding="utf-8") == "existing ledger"


def test_complete_download_does_not_imply_trading_or_rights_ready(tmp_path):
    state = {
        "state": "PREPARATION_COMPLETE",
        "phase": "complete",
        "price_ready": True,
        "clues_downloaded": True,
        "signal_ready": False,
        "trading_ready": False,
        "requested_as_of": "2026-10-27",
        "effective_as_of": "2026-10-27",
        "updated_at": "2026-10-28T18:30:00+08:00",
        "pool": {"selected": [{"code": "SH600000", "name": "示例股份"}], "requested_size": 100},
        "securities": [
            {
                "code": "SH600000",
                "price_ready": True,
                "clues_downloaded": True,
                "signal_ready": False,
                "trading_ready": False,
            }
        ],
        "requests": {"one": {"query": "history", "params": {"code": "SH600000"}, "status": "OK"}},
    }
    original = copy.deepcopy(state)
    html = write_preparation_report(tmp_path, state).read_text(encoding="utf-8")
    assert "本轮数据准备完成，尚未开始交易" in html
    assert "权益线索不是可结算的现金或股份权益" in html
    assert "行情已校验" in html and "线索已下载" in html
    assert html.count("尚未就绪") >= 4
    assert "2026-10-28T18:30:00+08:00" in html
    assert "当前已列入计划的 1 项请求" in html
    assert state == original


def test_partial_state_explains_missing_requests_and_escapes_source_text(tmp_path):
    attack = '<script>alert("source")</script>'
    state = {
        "state": "PARTIAL",
        "error": attack,
        "pool": {
            "selected": [{"code": "SH600000", "name": attack}],
            "excluded": [
                {
                    "code": "SZ000001",
                    "name": "缺数股票",
                    "reason": "缺失历史日线",
                    "missing_dates": ["2026-10-26"],
                }
            ],
        },
        "requests": {
            "one": {
                "query": "history",
                "params": {"code": "sh.600000"},
                "status": "FAILED",
                "error": attack,
                "retryable": True,
                "next_retry_at": "2026-10-28T19:00:00+08:00",
            },
            "two": {
                "query": "dividends",
                "params": {"code": "SH600000", "year": 2026},
                "status": "WAITING_DOWNLOAD",
            },
            "three": {
                "query": "calendar",
                "params": {},
                "status": "CACHE_INVALID",
                "error": "缓存校验失败",
                "retryable": False,
            },
        },
    }
    html = write_preparation_report(tmp_path, state).read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "121 日日线尚未通过完整性校验" in html
    assert "2026-10-26" in html and "最早重试时间" in html
    assert "失败 / 缓存异常</span><b>2</b>" in html
    assert "待下载 / 待继续</span><b>1</b>" in html
    assert "程序不会自动反复重试" in html


def test_report_omits_raw_bars_and_replaces_previous_snapshot(tmp_path):
    path = write_preparation_report(tmp_path, {"state": "PREPARING", "bars": ["NEVER_RENDER_RAW"]})
    assert "NEVER_RENDER_RAW" not in path.read_text(encoding="utf-8")
    write_preparation_report(tmp_path, {"state": "WAITING_DATA"})
    html = path.read_text(encoding="utf-8")
    assert "等待数据下载或重试" in html
    assert "正在准备数据" not in html
    assert not list(path.parent.glob(".writing-*"))


def test_coverage_shows_bad_stock_reasons_without_flooding_report(tmp_path):
    state = {
        "state": "PARTIAL",
        "run_id": "task-<script>bad</script>",
        "universe_data_quality": [
            {
                "date": "HEALTHY_DAY_HIDDEN",
                "eligible_count": 100,
                "valid_count": 100,
                "missing_codes": [],
                "issues": [],
            },
            {
                "date": "2026-10-26",
                "eligible_count": 100,
                "valid_count": 98,
                "missing_codes": ["MISSING_CODE", "DUPLICATE_ISSUE"],
                "issues": [{"code": "DUPLICATE_ISSUE", "reason": "<script>坏行情</script>"}],
            },
            {
                "date": "2026-10-27",
                "eligible_count": 100,
                "valid_count": 60,
                "missing_codes": [],
                "issues": [
                    {"code": f"ISSUE_{number}", "reason": f"错误编号_{number}"}
                    for number in range(40)
                ],
            },
        ],
        "pool": {
            "excluded": [
                {"code": f"EXCLUDED_{number}", "name": "示例", "reason": "数据不完整"}
                for number in range(55)
            ]
        },
    }
    html = write_preparation_report(tmp_path, state).read_text(encoding="utf-8")
    assert "已记录 3 个交易日，其中 2 日存在缺失或异常" in html
    assert "HEALTHY_DAY_HIDDEN" not in html
    assert "100 / 98" in html
    assert "MISSING_CODE：当日日线缺失" in html
    assert html.count("DUPLICATE_ISSUE") == 1
    assert "&lt;script&gt;坏行情&lt;/script&gt;" in html
    assert "<script>" not in html
    assert "错误编号_17<" in html
    assert "错误编号_18<" not in html
    assert "最多展示 20 条股票问题" in html
    assert "manifest.json" in html and "universe_data_quality" in html
    assert "EXCLUDED_49<" in html and "EXCLUDED_50<" not in html
    assert "页面最多显示 50 只" in html
    assert "排除该股并继续处理其他股票" in html
