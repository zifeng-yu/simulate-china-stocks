"""A bounded, read-only view of first-preparation state, separate from the ledger."""

import json
from collections import Counter
from html import escape
from pathlib import Path

from china_stock_sim.config import local_path
from china_stock_sim.market.storage import atomic_text

_STATES = {
    "NOT_STARTED": "尚未开始首次准备",
    "PREPARING": "正在准备数据",
    "WAITING_DATA": "等待数据下载或重试",
    "NEEDS_DATA_REVIEW": "数据需要检查",
    "NO_ELIGIBLE_SECURITIES": "当前没有符合条件的股票",
    "PARTIAL": "部分数据已完成，仍有缺失",
    "PREPARATION_COMPLETE": "本轮数据准备完成，尚未开始交易",
}
_PHASES = {
    "calendar": "交易日历",
    "universe": "证券基本信息",
    "liquidity20": "近 20 个交易日行情与选池",
    "history_and_clues": "股票池历史行情与权益线索",
    "complete": "本次处理已结束",
}
_REQUESTS = {
    "PENDING": "待处理",
    "WAITING_DOWNLOAD": "待下载",
    "BUDGET_PAUSED": "达到本次预算，等待继续",
    "RUNNING": "请求中（以最后记录为准）",
    "OK": "成功",
    "FAILED": "失败",
    "CACHE_INVALID": "缓存校验失败",
}
_QUERIES = {
    "calendar": "交易日历",
    "securities": "证券信息",
    "session_bars": "全市场日线",
    "history": "个股历史日线",
    "adjustments": "复权因子线索",
    "dividends": "分红线索",
}


def _text(value, default="尚无", limit=2000) -> str:
    if value is None or value == "":
        value = default
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…（过长内容已截断）"
    return escape(text, quote=True)


def _badge(value, yes, no) -> str:
    return f'<span class="{"good" if value is True else "pending"}">{yes if value is True else no}</span>'


def _code(value) -> str:
    return str(value or "").replace(".", "").upper()


def _details(request: dict) -> str:
    parts = []
    if request.get("error"):
        parts.append(_text(request["error"]))
    if request.get("retryable") is True:
        parts.append("允许重试；最早重试时间：" + _text(request.get("next_retry_at")))
    elif request.get("retryable") is False:
        parts.append("需检查原因，程序不会自动反复重试")
    if request.get("fetched_at"):
        parts.append("源数据获取时间：" + _text(request["fetched_at"]))
    return "<br>".join(parts) or "—"


def _coverage_summary(state: dict) -> str:
    coverage = state.get("universe_data_quality", [])
    problem_days = [
        day
        for day in coverage
        if day.get("missing_codes")
        or day.get("issues")
        or day.get("valid_count") != day.get("eligible_count")
    ]
    rows, remaining = [], 20
    for day in problem_days:
        missing, issues = day.get("missing_codes", []), day.get("issues", [])
        displayed = []
        issue_codes = {_code(issue.get("code")) for issue in issues}
        for issue in issues:
            if remaining == 0:
                break
            displayed.append(_text(issue.get("code")) + "：" + _text(issue.get("reason")))
            remaining -= 1
        for code in missing:
            if remaining == 0:
                break
            if _code(code) not in issue_codes:
                displayed.append(_text(code) + "：当日日线缺失")
                remaining -= 1
        rows.append(
            "<tr><td>"
            + _text(day.get("date"))
            + "</td><td>"
            + _text(day.get("eligible_count"))
            + " / "
            + _text(day.get("valid_count"))
            + "</td><td>"
            + str(len(missing))
            + " / "
            + str(len(issues))
            + '</td><td class="detail">'
            + ("<br>".join(displayed) or "详情见任务 manifest")
            + "</td></tr>"
        )
    if not coverage:
        body = '<p class="empty">尚无全市场日线覆盖结果，需先完成对应日期的数据请求。</p>'
    elif not rows:
        body = '<p class="empty">已记录日期暂无个股缺失或校验异常；尚未完成的请求见下方明细。</p>'
    else:
        body = (
            '<div class="scroll"><table><thead><tr><th>问题日期</th><th>应覆盖 / 有效股票</th>'
            "<th>缺失 / 异常条数</th><th>股票与原因</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
    return (
        "<section><h2>近 20 日全市场数据覆盖</h2>"
        f"<p>已记录 {len(coverage)} 个交易日，其中 {len(problem_days)} 日存在缺失或异常。"
        "个别股票数据不合格时，会排除该股并继续处理其他股票；不会用缺失值补出行情。</p>"
        + body
        + '<p class="muted">页面仅列问题日期，最多展示 20 条股票问题。完整覆盖及问题列表见 '
        "<code>data/state/preparation/"
        + _text(state.get("run_id"), "任务目录")
        + "/manifest.json</code> 的 universe_data_quality；不代表尚未下载的日期已通过检查。</p></section>"
    )


def write_preparation_report(root, state: dict) -> Path:
    """Render known requests and a small stock pool, never raw price histories."""
    root = Path(root).resolve()
    path = local_path(root, "reports/preparation/index.html")
    requests = list(state.get("requests", {}).values())
    counts = Counter(request.get("status", "PENDING") for request in requests)
    failed = counts["FAILED"] + counts["CACHE_INVALID"]
    waiting = sum(
        count
        for status, count in counts.items()
        if status not in {"OK", "FAILED", "CACHE_INVALID", "RUNNING"}
    )
    pool = state.get("pool") or {}
    selected = pool.get("selected", [])
    securities = {_code(row.get("code")): row for row in state.get("securities", [])}
    # Before the per-stock stage finishes, selected stocks still appear as pending.
    stocks = selected or state.get("securities", [])
    stock_rows = []
    for stock in stocks:
        code = stock.get("code")
        progress = securities.get(_code(code), {})
        missing = []
        if progress.get("price_ready") is not True:
            missing.append("121 日日线尚未通过完整性校验")
        if progress.get("clues_downloaded") is not True:
            missing.append("权益线索尚未完整下载")
        errors = [
            request
            for request in requests
            if _code(request.get("params", {}).get("code")) == _code(code) and request.get("error")
        ]
        detail = (
            "<br>".join(
                [_text(item) for item in missing]
                + [
                    _text(_QUERIES.get(request.get("query"), request.get("query")))
                    + "："
                    + _text(request["error"])
                    for request in errors
                ]
            )
            or "行情和来源线索已准备；具体用途就绪及交易计划另行检查"
        )
        stock_rows.append(
            "<tr><td>"
            + _text(code)
            + "<br><small>"
            + _text(stock.get("name"))
            + "</small></td>"
            + "<td>"
            + _badge(progress.get("price_ready"), "行情已校验", "待准备")
            + "</td>"
            + "<td>"
            + _badge(progress.get("clues_downloaded"), "线索已下载", "待下载")
            + "</td>"
            + "<td>"
            + _badge(progress.get("signal_ready"), "已就绪", "尚未就绪")
            + "</td>"
            + "<td>"
            + _badge(progress.get("trading_ready"), "状态标记已就绪", "尚未就绪")
            + "</td>"
            + '<td class="detail">'
            + detail
            + "</td></tr>"
        )
    stock_table = (
        "".join(stock_rows)
        or '<tr><td colspan="6" class="empty">股票池尚未形成。先完成日历、证券信息与近 20 日行情。</td></tr>'
    )
    request_rows = []
    for request in requests:
        params = json.dumps(request.get("params", {}), ensure_ascii=False, sort_keys=True)
        status = request.get("status", "PENDING")
        request_rows.append(
            "<tr><td>"
            + _text(_QUERIES.get(request.get("query"), request.get("query")))
            + "<br><code>"
            + _text(params)
            + "</code></td><td>"
            + _text(_REQUESTS.get(status, status))
            + "<br><small>尝试 "
            + _text(request.get("attempts", 0))
            + " 次</small></td>"
            + '<td class="detail">'
            + _details(request)
            + "</td></tr>"
        )
    request_table = (
        "".join(request_rows)
        or '<tr><td colspan="3" class="empty">尚无请求记录；不会凭空显示下载成功。</td></tr>'
    )
    excluded = pool.get("excluded", [])
    excluded_rows = "".join(
        "<tr><td>"
        + _text(row.get("code"))
        + "</td><td>"
        + _text(row.get("name"))
        + "</td><td>"
        + _text(row.get("reason"))
        + (
            "<br>缺失日期：" + _text(", ".join(row["missing_dates"]))
            if row.get("missing_dates")
            else ""
        )
        + "</td></tr>"
        for row in excluded[:50]
    )
    exclusions = (
        (
            f"<details><summary>未入选股票及原因：{len(excluded)} 只（页面最多显示 50 只）</summary>"
            '<div class="scroll"><table><thead><tr><th>代码</th><th>名称</th><th>原因</th></tr></thead><tbody>'
            + excluded_rows
            + "</tbody></table></div></details>"
        )
        if excluded
        else ""
    )
    state_code = state.get("state", "NOT_STARTED")
    phase = state.get("phase")
    sessions = state.get("sessions", [])
    window = (
        (_text(sessions[0]) + " 至 " + _text(sessions[-1]) + f" · {len(sessions)} 个交易日")
        if sessions
        else "尚未确定"
    )
    error = (
        '<p class="error">本次错误：' + _text(state["error"]) + "</p>" if state.get("error") else ""
    )
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>首次准备 · 数据下载进度</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6f5;color:#193b3a;font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}main{{max-width:1200px;margin:auto;padding:32px 22px}}h1{{font-size:32px;margin:8px 0}}h2{{font-size:21px}}a{{color:#246c5b}}small,.muted,.empty{{color:#5d7170}}.tag{{color:#246c5b;font-weight:650}}.notice{{background:#fff3d4;border:1px solid #ebd9a7;padding:16px;border-radius:10px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}.card,section{{padding:20px;border:1px solid #dbe4df;background:#fff;border-radius:12px}}.card b{{display:block;font-size:28px}}.card span{{font-size:14px}}section{{margin:20px 0}}table{{border-collapse:collapse;width:100%}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #e2e9e6;vertical-align:top}}th{{white-space:nowrap}}td{{min-width:100px}}.detail{{min-width:230px;overflow-wrap:anywhere}}.scroll{{overflow:auto}}.good{{color:#246c5b}}.pending{{color:#836016}}.error{{color:#9b3030;overflow-wrap:anywhere}}code{{font-size:12px;overflow-wrap:anywhere}}summary{{cursor:pointer;padding:12px 0}}footer{{color:#5d7170;font-size:13px;overflow-wrap:anywhere}}@media(max-width:700px){{.cards{{grid-template-columns:repeat(2,1fr)}}main{{padding:24px 12px}}section{{padding:14px}}}}
</style></head><body><main>
<div class="tag">前向模拟 · 首次准备</div><h1>{_text(_STATES.get(state_code, state_code))}</h1>
<p class="muted">这是数据准备进度，交易资金、持仓和成交请查看 <a href="../forward_current/index.html">账户账本</a>。</p>
<div class="notice">下载完成仅代表本轮行情与源线索准备完成。<strong>权益线索不是可结算的现金或股份权益</strong>；还需核验与转换。三策略及整日恢复流程已接通，正式运行与自动调度须显式启用；本页不表示已生成交易计划、撮合成交或每日账单。</div>
<div class="cards">
<div class="card"><span>已成功请求</span><b>{counts["OK"]}</b></div>
<div class="card"><span>失败 / 缓存异常</span><b>{failed}</b></div>
<div class="card"><span>待下载 / 待继续</span><b>{waiting}</b></div>
<div class="card"><span>最后记录为请求中</span><b>{counts["RUNNING"]}</b></div>
</div>
<p class="muted">统计仅包含当前已列入计划的 {len(requests)} 项请求；后续阶段会继续生成请求，不表示总下载百分比。本次已发出 {_text(state.get("requests_this_run", 0))} 次请求。</p>
<section><h2>准备范围与更新时间</h2>
<p>请求截止日：{_text(state.get("requested_as_of"))}<br>实际最后交易日：{_text(state.get("effective_as_of"))}<br>历史窗口：{window}<br>当前阶段：{_text(_PHASES.get(phase, phase))}<br>首次创建：{_text(state.get("created_at"))}<br>最后更新：{_text(state.get("updated_at"))}</p>
<p>行情：{_badge(state.get("price_ready"), "已校验", "尚未完整")}
 · 权益线索：{_badge(state.get("clues_downloaded"), "已下载（仍需核验）", "尚未完整")}
 · 信号：{_badge(state.get("signal_ready"), "已就绪", "尚未就绪")}
 · 交易：{_badge(state.get("trading_ready"), "状态标记已就绪", "尚未就绪")}</p>
<p class="muted">免费接口成功下载记录：{"已有" if state.get("network_tested") is True else "尚无"}。证券基本信息是当前快照，不能当作历史每一天当时已知的信息。</p>{error}</section>
{_coverage_summary(state)}
<section><h2>股票池与逐股准备状态</h2><p>当前入选 {len(selected)} 只；配置目标 {_text(pool.get("requested_size"))} 只。行情列表示完整性校验状态，不是股票价格或买卖建议。</p>
<div class="scroll"><table><thead><tr><th>股票</th><th>历史行情</th><th>权益线索</th><th>策略信号</th><th>交易条件</th><th>缺失 / 错误详情</th></tr></thead><tbody>{stock_table}</tbody></table></div>{exclusions}</section>
<section><h2>请求与错误明细</h2><div class="scroll"><table><thead><tr><th>查询与范围</th><th>状态</th><th>获取时间 / 原因 / 重试</th></tr></thead><tbody>{request_table}</tbody></table></div></section>
<footer>任务：{_text(state.get("run_id"))}<br>状态文件：data/state/preparation/latest.json<br>页面每 30 秒重新加载；只有程序继续运行并写入状态时，进度才会变化。关闭电脑或暂停程序后，刷新页面不会发起下载。页面不加载外部资源，不展示原始海量行情。</footer>
</main></body></html>"""
    atomic_text(path, html)
    return path
