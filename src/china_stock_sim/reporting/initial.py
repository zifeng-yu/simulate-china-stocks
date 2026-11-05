"""从已初始化账本生成静态页；不伪造行情、成交或金融观察日。"""

import json
import os
import re
from datetime import datetime
from html import escape

from china_stock_sim.common.accounts import select_account
from china_stock_sim.config import Settings
from china_stock_sim.market.storage import atomic_text
from china_stock_sim.reporting.audit import account_filename, audit_account, write_audit_report
from china_stock_sim.reporting.presentation import money, overview_html, report_path


def account_display(account):
    initial, cash = money(account['initial_cash_fen']), money(account['cash_fen'])
    movement = money(account['cash_fen'] - account['initial_cash_fen'])
    return {'account_id': account['account_id'], 'initial': initial, 'cash': cash,
        'formula': f'当前资金计算：初始注资 {initial} + 后续现金流水 {movement} = 现金 {cash}；'
                   f'现金 + 持仓市值 0.00 = 当前资产 {cash}。'}


def write_initial_report(settings: Settings, snapshot: dict) -> None:
    accounts = sorted(snapshot["accounts"], key=lambda row: row['account_id'])
    selection = select_account(accounts, settings.default_account)
    selected = next(row for row in accounts if row["account_id"] == selection['account_id'])
    selected_display = account_display(selected)
    generated = datetime.now(settings.timezone).isoformat(timespec="seconds")
    identity_sections = []
    for account in accounts:
        audit = audit_account(settings.root, {'account': account, 'plans': [], 'orders': [], 'fills': [],
            'account_revision': account.get('account_revision', 0),
            'report_revision': account.get('report_revision', 0)})
        output = settings.root / 'reports/strategies' / (account_filename(account['account_id']) + '-audit.json')
        write_audit_report(settings.root, output, audit)
        href = escape(os.path.relpath(output, settings.report.parent), quote=True)
        view_href = escape(os.path.relpath(output.with_suffix('.html'), settings.report.parent), quote=True)
        hidden = '' if account['account_id'] == selection['account_id'] else ' hidden'
        identity_sections.append(f'<section data-account="{escape(account["account_id"], quote=True)}"{hidden}>'
            + overview_html(account, audit) + f'<p><a href="{view_href}">查看本账户审计（中文）</a> · '
            + f'<a href="{href}">导出本账户审计 JSON（尚无已冻结计划）</a></p></section>')
    options = "".join(
        '<option value="'
        + escape(row["account_id"], quote=True)
        + '"'
        + (" selected" if row["account_id"] == selection['account_id'] else "")
        + ">"
        + escape(row["account_id"])
        + " · 独立账户</option>"
        for row in accounts
    )
    rows = "".join(
        "<tr><td>"
        + escape(row["account_id"])
        + "</td><td>"
        + money(row["initial_cash_fen"])
        + "</td><td>"
        + money(row["cash_fen"])
        + "</td><td>0</td><td>尚未开始前向模拟</td></tr>"
        for row in accounts
    )
    serialized = json.dumps(snapshot['accounts'], ensure_ascii=False).replace("<", "\\u003c")
    data_report = settings.root / "reports/data-sync/index.html"
    data_status = "此初始账本尚未引用指数行情，不表示市场库没有数据。"
    if data_report.is_file():
        href = escape(os.path.relpath(data_report, settings.report.parent), quote=True)
        data_status += f' <a href="{href}">查看最近的数据采集状态</a>。'
    content = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>前向模拟 · 初始账本</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6f5;color:#193b3a;font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1080px;margin:auto;padding:40px 24px}h1{font-size:32px;margin:10px 0}h2{font-size:21px}small,.muted{color:#5d7170}.tag{color:#246c5b;font-weight:650;letter-spacing:.08em}.notice{background:#fff3d4;border:1px solid #ebd9a7;padding:16px;border-radius:9px;margin:22px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card,section{padding:22px;border:1px solid #dbe4df;background:white;border-radius:12px}.card b{display:block;font-size:25px}.card span{font-size:13px;color:#5d7170}section{margin:22px 0}select{font:inherit;border:1px solid #b2c9bc;border-radius:6px;padding:8px 15px;background:white}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #e2e9e6;white-space:nowrap}.scroll{overflow:auto}.empty{color:#5d7170;padding:12px 0}code{font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere}footer{font-size:13px;color:#5d7170;overflow-wrap:anywhere}@media(max-width:700px){.cards{grid-template-columns:1fr 1fr}main{padding:24px 14px}.card,section{padding:16px}.card b{font-size:21px}}
</style></head><body><main>
<div class="tag">本机前向模拟 · 初始化</div><h1>账户已经建立，交易尚未开始</h1>
<p class="muted">__ACCOUNT_COUNT__ 个独立虚拟账户，各自本金以实际账本为准。切换查看，不合并总资产。</p>
<label>当前账户 <select id="account">__OPTIONS__</select></label>
__SELECTION__
<div class="notice"><strong>INITIALIZED_NOT_RUNNING</strong><br>当前账户尚未启用正式观察。数据、账本、三策略、规则执行与整日恢复流程已具备；账户须绑定当前策略修订并显式启用，自动调度另行配置和安装；此页展示初始资金，尚无交易日报。</div>
<div class="cards"><div class="card"><span>初始本金（元）</span><b id="initial">__INITIAL__</b></div><div class="card"><span>现金 / 可用现金（元）</span><b id="cash">__CASH__</b></div><div class="card"><span>持仓市值（元）</span><b>0.00</b></div><div class="card"><span>当前资产（元）</span><b id="equity">__CASH__</b></div></div>
<section><h2>运行状态</h2><p>最后日结交易日：尚无<br>金融观察日：0<br>本账单估值行情：尚未使用（未开始观察）<br>交易费用：0.00 元<br>当日盈亏：尚未日结<br>买单资金预留：0.00 元<br>应收 / 应付权益：0.00 元</p><p class="muted">页面生成时间：<time id="updated" datetime="__TIME__">__TIME__</time><br><span id="age"></span>初始化时间不是交易观察日，刷新页面不会增加收益或成交。</p></section>
__IDENTITIES__
<section><h2>独立账户</h2><div class="scroll"><table><thead><tr><th>账户</th><th>初始本金（元）</th><th>现金（元）</th><th>持仓只数</th><th>状态</th></tr></thead><tbody>__ROWS__</tbody></table></div></section>
<section><h2>买卖与持仓</h2><div class="empty">暂无买卖计划、成交和持仓。账户显式启用并运行策略、执行与日结流程后，将显示实际前向模拟记录。</div><p id="formula">__FORMULA__</p></section>
<section><h2>下一交易日计划 / 大盘</h2><div class="empty">计划：尚未生成。<br>__DATA_STATUS__</div></section>
<footer>金额及账户来自本项目 SQLite 持久账本。<br>数据库：__DATABASE__</footer>
<script type="application/json" id="accounts">__JSON__</script>
<script type="application/json" id="account-display">__DISPLAY_JSON__</script>
<script>
const accounts=JSON.parse(document.getElementById('account-display').textContent);
document.getElementById('account').addEventListener('change',e=>{const a=accounts.find(x=>x.account_id===e.target.value);document.getElementById('initial').textContent=a.initial;document.getElementById('cash').textContent=a.cash;document.getElementById('equity').textContent=a.cash;document.getElementById('formula').textContent=a.formula;document.querySelectorAll('[data-account]').forEach(s=>s.hidden=s.dataset.account!==e.target.value)});
const age=()=>{const mins=Math.floor((Date.now()-Date.parse(document.getElementById('updated').dateTime))/60000);document.getElementById('age').textContent=mins<0?'本机时钟早于页面生成时间，请核对时钟。':`距页面生成 ${mins} 分钟；此页不会自动获取新行情。`};age();setInterval(age,60000);
</script></main></body></html>"""
    replacements = {
        "__DATA_STATUS__": data_status,
        "__OPTIONS__": options,
        "__ACCOUNT_COUNT__": str(len(accounts)),
        "__IDENTITIES__": ''.join(identity_sections),
        "__SELECTION__": ('<p class="notice">默认账户不可用，已按账户 ID 稳定排序选择 '
            + escape(selection['account_id']) + '。</p>') if selection['fallback'] else '',
        "__FORMULA__": selected_display['formula'],
        "__INITIAL__": money(selected["initial_cash_fen"]),
        "__CASH__": money(selected["cash_fen"]),
        "__TIME__": escape(generated),
        "__ROWS__": rows,
        "__DATABASE__": escape(str(settings.database.relative_to(settings.root))),
        "__JSON__": serialized,
        "__DISPLAY_JSON__": json.dumps([account_display(row) for row in accounts],
                                       ensure_ascii=False).replace('<', '\\u003c'),
    }
    content = re.sub(r'__[A-Z_]+__', lambda match: replacements[match[0]], content)
    atomic_text(report_path(settings, settings.report), content)
