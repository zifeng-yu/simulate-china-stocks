"""A small, portable operational homepage. Rendering never runs business work."""

import json
from html import escape

STATE_LABELS = {
    'SCHEDULED': '待定时运行', 'PENDING': '等待前序步骤', 'WAITING': '等待处理',
    'WAITING_DATA': '等待数据', 'COOLDOWN': '等待重试', 'RUNNING': '运行中',
    'COMPLETE': '运行完成', 'COMPLETED': '运行完成', 'READY': '已就绪',
    'FROZEN': '计划已冻结', 'FAILED': '运行失败', 'PARTIAL': '部分完成',
    'INTERRUPTED': '运行中断', 'DISABLED': '未启用', 'NOT_REQUIRED': '本日无需执行',
    'NOT_APPLICABLE': '无需执行', 'SKIPPED': '已跳过', 'UNKNOWN': '状态待确认',
    'MISSED': '已过截止时间', 'MISSED_CUTOFF': '已过截止时间',
    'MAINTENANCE': '维护中', 'NOT_STARTED': '尚未运行', 'IDLE': '等待下一次检查',
}
PHASE_LABELS = {
    'calendar': '核对交易日历', 'data': '采集与补齐数据', 'month_end': '月末换池',
    'input_review': '复查迟到数据', 'recovery': '恢复历史待办', 'settlement': '账户结算',
    'entitlements_before': '处理到期权益', 'execution': '撮合原冻结计划',
    'entitlements_after': '处理权益与税款', 'close': '日结与估值',
    'report_daily': '更新当日交易看板', 'plan': '生成或恢复策略计划',
    'report_plan': '更新下一日计划', 'backup': '校验与保存备份',
}


def _text(value, limit=800):
    if value is None:
        return ''
    return escape(str(value)[:limit], quote=True)


def _label(row):
    return row.get('label') or STATE_LABELS.get(row.get('state'), '状态待确认')


def _tone(state):
    if state in {'COMPLETE', 'COMPLETED', 'READY', 'FROZEN'}:
        return 'success'
    if state == 'RUNNING':
        return 'active'
    if state in {'FAILED', 'INTERRUPTED', 'MISSED', 'MISSED_CUTOFF'}:
        return 'error'
    if state in {'WAITING', 'WAITING_DATA', 'COOLDOWN', 'PARTIAL', 'MAINTENANCE'}:
        return 'warning'
    return 'neutral'


def _badge(row):
    return (f'<span class="badge {_tone(row.get("state"))}" '
            f'data-state="{_text(row.get("state"))}">{_text(_label(row), 100)}</span>')


def _date(value):
    return f'<time data-iso="{_text(value)}">{_text(value or "—")}</time>'


def _account_rows(accounts):
    if not accounts:
        return ''
    parts = []
    for item in accounts[:100]:
        phases = item.get('phases', [])
        if isinstance(phases, dict):
            phases = [{'phase': key, **value} for key, value in phases.items()
                      if isinstance(value, dict)]
        detail = ''.join(
            f'<span>{_text(PHASE_LABELS.get(p.get("phase"), p.get("title", "处理步骤")))}'
            f' · {_text(_label(p), 100)}</span>' for p in phases[:8]
        )
        parts.append(
            f'<div class="account-row"><div><strong>{_text(item.get("display_name") or item.get("account_id"), 100)}</strong>'
            f'<p>{_text(item.get("detail"))}</p><div class="subphases">{detail}</div></div>{_badge(item)}</div>'
        )
    return '<div class="account-rows">' + ''.join(parts) + '</div>'


def render_home(status, *, scope='local', cloud_publication=None):
    """One renderer serves the local file and the cloud's static index.html."""
    if scope not in {'local', 'cloud'}:
        raise ValueError('首页展示范围无效')
    nodes = status.get('nodes', [])
    if not isinstance(nodes, list) or len(nodes) > 30:
        raise ValueError('首页节点数量无效')
    source_time = status.get('sampled_at') or status.get('source_sampled_at')
    summary = status.get('summary') or {'state': 'UNKNOWN', 'label': '状态待确认'}
    calendar = status.get('calendar') or {}
    today = status.get('today', '')
    if isinstance(today, dict):
        today = today.get('date', '')
    trading_day = calendar.get('is_trading_day')
    day_label = '交易日' if trading_day is True else '非交易日' if trading_day is False else '交易日历待确认'
    calendar_note = (f'{day_label} · 最近可处理交易日 D：{calendar.get("completed_session") or "未知"}'
                     f' · 对应计划日 N：{calendar.get("planning_target") or "未知"}')
    current = status.get('current_run') or {}
    scheduler = status.get('scheduler') or {}
    workflow = scheduler.get('workflow', scheduler)
    monitor = scheduler.get('monitor', {})
    monitor_label = ('刷新任务已启用' if monitor.get('installed') and monitor.get('loaded')
                     and monitor.get('enabled') and not monitor.get('disabled')
                     else '刷新任务待确认')
    loaded = workflow.get('installed') and workflow.get('loaded') and workflow.get('enabled')
    schedule_label = '已启用' if loaded else '请检查调度状态'
    if workflow.get('disabled') is True:
        schedule_label = '定时触发已暂停'
    maintenance = status.get('maintenance') or {}
    if maintenance.get('active'):
        schedule_label = '维护中'
    running = current.get('state') == 'RUNNING'
    run_label = current.get('label') or (PHASE_LABELS.get(current.get('phase')) if running else None)
    run_label = run_label or STATE_LABELS.get(current.get('state'), '尚无运行记录')
    window = status.get('automatic_execution') or {}
    schedule_note = '18:10 日终到期 · 15分钟检查'
    if window.get('quiet_start') and window.get('quiet_end'):
        schedule_note += f' · {window["quiet_start"]}—{window["quiet_end"]}业务避让'
    if loaded and window.get('quiet') and not running and not maintenance.get('active'):
        run_label = f'待{window["quiet_end"]}后首次检查继续'
    trade_href = 'trading.html' if scope == 'cloud' else 'forward_current/index.html'
    rows = []
    for index, node in enumerate(nodes, 1):
        node_id = str(node.get('id', index))
        scheduled = node.get('scheduled_at')
        timing = []
        for title, key in [('最早开始', 'scheduled_at'), ('截止', 'deadline_at'), ('开始', 'started_at'),
                           ('结束', 'finished_at'), ('可重试', 'next_retry_at')]:
            if node.get(key):
                timing.append(f'<span>{title} {_date(node[key])}</span>')
        accounts = node.get('accounts') or []
        details = ''
        if accounts:
            details = (f'<details data-section="{_text(node_id)}"><summary>查看账户进度'
                       f'<span>{len(accounts)} 个账户</span></summary>{_account_rows(accounts)}</details>')
        children = ''.join(f'<span>{_text(child.get("title"), 100)} · {_text(_label(child), 100)}</span>'
                           for child in (node.get('details') or [])[:8])
        if children:
            details += '<div class="subphases">' + children + '</div>'
        rows.append(
            f'<li class="node" data-node="{_text(node_id)}" data-planned-at="{_text(scheduled)}">'
            f'<span class="number {_tone(node.get("state"))}">{index:02}</span>'
            f'<div class="node-main"><div class="node-heading"><h3>{_text(node.get("title", node_id), 120)}</h3>'
            f'{_badge(node)}</div><p>{_text(node.get("detail"))}</p>'
            f'<div class="timing">{"".join(timing)}</div>{details}</div></li>'
        )
    completed = sum(n.get('state') in {'COMPLETE', 'COMPLETED', 'READY', 'FROZEN'} for n in nodes)
    run_times = ''.join(f'<div><dt>{title}</dt><dd>{_date(current.get(key))}</dd></div>'
                        for title, key in [('开始时间', 'started_at'), ('结束时间', 'finished_at')])
    if running and current.get('started_at'):
        run_times += (f'<div><dt>本轮已持续</dt><dd data-elapsed-from="{_text(current["started_at"])}">计算中</dd></div>')
    if running and current.get('phase_started_at'):
        run_times += f'<div><dt>当前阶段开始</dt><dd>{_date(current["phase_started_at"])}</dd></div>'
        run_times += (f'<div><dt>当前阶段已持续</dt><dd data-elapsed-from="'
                      f'{_text(current["phase_started_at"])}">计算中</dd></div>')
    services = []
    for title, service in [('交易流程', workflow), ('首页刷新', monitor)]:
        runtime = service.get('runtime') or {}
        loaded_text = '已加载' if service.get('loaded') is True else '未加载或待确认'
        enabled_text = '启用' if service.get('enabled') and not service.get('disabled') else '未启用'
        last_exit = runtime.get('last_exit_code')
        services.append(f'<div><dt>{title}</dt><dd>{enabled_text} · {loaded_text} · '
            f'{_text(STATE_LABELS.get(runtime.get("state"), "进程待确认"))}'
            f'<br>上次退出码：{_text(last_exit if last_exit is not None else "未知")}</dd></div>')
    run_times += ''.join(services)
    publication = cloud_publication or status.get('publication') or {}
    if scope == 'cloud':
        publish_label = '每日 09:00、21:00 由本机上传'
        publish_detail = '此页面展示最近一次成功上传的快照。关机或离线恢复后，只补最新已到时点一次。'
        publish_time = publication.get('published_at') or publication.get('last_success_at')
        pub_rows = f'<div><dt>成功发布时间</dt><dd>{_date(publish_time)}</dd></div>'
        pub_rows += f'<div><dt>下一次计划同步</dt><dd>{_date(publication.get("next_due_at"))}</dd></div>'
    else:
        configured = publication.get('configured', False)
        publish_label = ('每日 09:00、21:00 自动推送' if publication.get('enabled') and configured
                         else '云端尚未启用')
        publish_detail = ('关机或离线恢复后，只补最新已到时点一次；同一时点成功后不重复。' if configured
                          else '等待配置服务器。云端只展示静态页面和必要数据，交易程序仍在本机运行。')
        pub_rows = f'<div><dt>最近成功上传</dt><dd>{_date(publication.get("last_success_at"))}</dd></div>'
        pub_rows += f'<div><dt>下一次计划同步</dt><dd>{_date(publication.get("next_due_at"))}</dd></div>'
    if publication.get('last_error'):
        publish_detail += ' 最近一次同步未成功，保留上一份完整页面，等待重试。'
    freshness = {
        'scope': scope, 'sampled_at': source_time,
        'stale_after_seconds': (status.get('freshness') or {}).get('local_stale_after_seconds', 1800),
        'next_due_at': publication.get('next_due_at'),
        'published_at': publication.get('published_at') or publication.get('last_success_at'),
    }
    payload = json.dumps({'status': status, 'freshness': freshness}, ensure_ascii=False, separators=(',', ':'))
    payload = (payload.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
               .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))
    if len(payload.encode()) > 500_000:
        raise ValueError('首页状态数据超出展示上限')
    mode_label = '云端只读快照' if scope == 'cloud' else '本机运行状态'
    refresh_label = ('云端每日 09:00、21:00 同步' if scope == 'cloud'
                     else '每 15 分钟更新 · ' + monitor_label)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><meta name="color-scheme" content="light">
<title>运行总览 · A股前向模拟</title><style>
*{{box-sizing:border-box}}:root{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#17283c;background:#f3f6fa;font-synthesis:none}}body{{margin:0}}a{{color:inherit}}button,a,summary{{-webkit-tap-highlight-color:transparent}}.wrap{{max-width:1180px;margin:auto;padding:32px 28px 24px}}header{{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:26px}}.brand{{font-size:11px;font-weight:750;letter-spacing:2px;color:#5d7491;margin-bottom:9px}}h1{{font-size:29px;letter-spacing:-.5px;margin:0 0 9px}}.subtitle{{margin:0;color:#64758b;font-size:13px;line-height:1.7}}.actions{{display:flex;align-items:center;gap:12px;flex-shrink:0}}.button{{text-decoration:none;border:1px solid #dbe3ed;background:white;border-radius:10px;padding:11px 16px;font-size:13px;font-weight:650}}.button.primary{{background:#214f89;color:white;border-color:#214f89;box-shadow:0 5px 14px #214f8914}}.button:hover{{filter:brightness(.96)}}.hero{{background:linear-gradient(120deg,#e7f0fd,#f4f8fe 70%);border:1px solid #d5e3f6;border-radius:16px;padding:23px 25px;display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:18px}}.eyebrow{{font-size:12px;font-weight:650;color:#587395;margin:0 0 8px}}.hero h2{{font-size:24px;margin:0 0 8px;color:#1d466f}}.hero p{{margin:0;color:#526b86;font-size:13px;line-height:1.7}}.progress{{white-space:nowrap;text-align:right;color:#446786;font-size:12px}}.progress strong{{font-size:27px;color:#1d466f;display:block;margin-bottom:5px}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:23px}}.metric{{border:1px solid #e1e7ef;background:white;border-radius:12px;padding:16px 18px;min-width:0}}.metric-label{{font-size:12px;color:#77869a;margin-bottom:9px}}.metric-value{{font-size:15px;font-weight:650;overflow-wrap:anywhere;line-height:1.5}}.metric-note{{color:#8592a3;font-size:11px;margin-top:5px}}.content{{display:grid;grid-template-columns:minmax(0,1fr) 292px;gap:20px;align-items:start}}.panel{{background:white;border:1px solid #e1e7ef;border-radius:14px;overflow:hidden}}.panel-head{{padding:21px 23px 16px;border-bottom:1px solid #edf0f5}}.panel h2{{font-size:16px;margin:0 0 7px}}.panel-head p{{font-size:12px;line-height:1.7;color:#7b889a;margin:0}}.nodes{{list-style:none;margin:0;padding:0 22px}}.node{{display:flex;gap:14px;padding:19px 0;border-bottom:1px solid #edf0f5}}.node:last-child{{border-bottom:0}}.number{{display:flex;align-items:center;justify-content:center;flex:0 0 29px;height:29px;border-radius:9px;font-size:11px;font-weight:750;margin-top:1px;background:#edf1f6;color:#788aa0}}.node-main{{min-width:0;flex:1}}.node-heading{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px}}.node h3{{font-size:14px;line-height:1.6;margin:0}}.node p{{font-size:12px;color:#6e7d90;margin:0;line-height:1.75;overflow-wrap:anywhere}}.badge{{display:inline-flex;align-items:center;justify-content:center;font-size:11px;padding:5px 9px;border-radius:6px;background:#edf1f6;color:#677990;font-weight:650;line-height:1.4;flex-shrink:0;max-width:200px;text-align:center}}.success{{background:#e8f5ef;color:#297550}}.active{{background:#e6effe;color:#2c61ac}}.warning{{background:#fff4df;color:#97691d}}.error{{background:#fcebec;color:#b54a52}}.timing{{display:flex;flex-wrap:wrap;gap:5px 14px;color:#8996a6;font-size:11px;margin-top:7px;line-height:1.7}}details{{margin-top:12px;border:1px solid #e8edf4;border-radius:8px;background:#fafbfd}}summary{{cursor:pointer;font-size:11px;font-weight:600;padding:9px 12px;color:#526b89}}summary span{{float:right;font-weight:400;color:#8a98a9}}.account-rows{{padding:0 12px}}.account-row{{display:flex;justify-content:space-between;gap:10px;padding:11px 0;border-top:1px solid #e8edf4}}.account-row>div{{min-width:0}}.account-row strong{{font-size:12px;overflow-wrap:anywhere}}.account-row .badge{{align-self:flex-start;font-size:10px}}.subphases{{display:flex;flex-direction:column;font-size:10px;color:#8390a2;line-height:1.8;margin-top:4px}}aside{{display:grid;gap:15px}}.side-body{{padding:19px 20px}}.side-body h3{{font-size:14px;margin:0 0 10px;line-height:1.65}}.side-body p{{font-size:12px;color:#738297;line-height:1.85;margin:0 0 16px}}dl{{margin:0}}dl>div{{display:flex;flex-direction:column;gap:4px;padding:10px 0;border-top:1px solid #edf0f5}}dt{{font-size:11px;color:#8a96a6}}dd{{font-size:12px;margin:0;line-height:1.6;overflow-wrap:anywhere}}.side-tag{{font-size:11px;color:#52749b;background:#edf3fa;border-radius:5px;padding:4px 7px;display:inline-block;margin-bottom:12px}}.notice{{background:#fff5df;border:1px solid #f0d79b;color:#876323;border-radius:10px;padding:13px 17px;font-size:12px;line-height:1.8;margin-bottom:17px}}.notice[hidden]{{display:none}}footer{{display:flex;justify-content:space-between;gap:12px;margin-top:23px;color:#8a96a7;font-size:11px;line-height:1.8}}time{{font-variant-numeric:tabular-nums}}a:focus-visible,summary:focus-visible{{outline:3px solid #8ab4ef;outline-offset:3px}}@media(max-width:880px){{.content{{grid-template-columns:minmax(0,1fr)}}aside{{grid-template-columns:1fr 1fr}}}}@media(max-width:560px){{.wrap{{padding:21px 14px}}header{{align-items:flex-start;gap:12px}}h1{{font-size:24px}}.brand{{font-size:9px;letter-spacing:1px}}.actions{{flex-direction:column;align-items:stretch;gap:7px}}.button{{padding:9px 11px;font-size:11px;text-align:center}}.hero{{padding:18px;gap:12px}}.hero h2{{font-size:20px}}.hero p{{font-size:12px}}.progress strong{{font-size:22px}}.metrics{{grid-template-columns:1fr 1fr;gap:9px}}.metric{{padding:13px 14px}}.metric-value{{font-size:13px}}.content{{gap:14px}}.panel-head{{padding:18px 16px 14px}}.nodes{{padding:0 15px}}.node{{gap:10px}}.node-heading{{align-items:flex-start}}.node h3{{font-size:13px}}.badge{{font-size:10px;max-width:120px;padding:4px 7px}}.node p{{font-size:11px}}.number{{flex-basis:25px;height:25px}}aside{{grid-template-columns:1fr}}footer{{flex-direction:column;gap:3px}}}}
</style></head><body><div class="wrap">
<header><div><div class="brand">SIMULATE CHINA STOCKS</div><h1>运行总览</h1><p class="subtitle">{_text(today)} · 北京时间 <span id="live-clock"></span><br>{mode_label}</p></div><nav class="actions" aria-label="看板导航"><a class="button primary" id="trading-link" href="{trade_href}">交易看板 ↗</a><a class="button" href="index.html">刷新页面</a></nav></header>
<div id="stale-notice" class="notice" role="status" hidden></div>
<section class="hero" aria-label="今日总体状态"><div><div class="eyebrow">今日运行安排</div><h2 id="summary-label">{_text(_label(summary), 120)}</h2><p>{_text(summary.get('detail'))}</p></div><div class="progress"><strong>{completed} / {len(nodes)}</strong>已完成节点</div></section>
<section class="metrics" aria-label="运行概况"><div class="metric"><div class="metric-label">交易调度</div><div class="metric-value">{_text(schedule_label)}</div><div class="metric-note">{_text(schedule_note, 160)}</div></div><div class="metric"><div class="metric-label">后台当前状态</div><div class="metric-value">{_text(run_label, 160)}</div><div class="metric-note">与今日节点分别展示</div></div><div class="metric"><div class="metric-label">源状态采样</div><div class="metric-value">{_date(source_time)}</div><div class="metric-note">{refresh_label}</div></div><div class="metric"><div class="metric-label">今日计划截止</div><div class="metric-value">09:00</div><div class="metric-note">已冻结计划按原记录执行</div></div></section>
<main class="content"><section class="panel"><div class="panel-head"><h2>每日关键节点</h2><p>{_text(calendar_note)}</p><p>{_text(calendar.get('display_note') or calendar.get('detail') or '按真实交易日历处理。未到时间的节点等待调度，完成状态以运行记录为准。')}</p></div><ol class="nodes">{''.join(rows)}</ol></section>
<aside><section class="panel"><div class="side-body"><span class="side-tag">{'采样时运行' if scope == 'cloud' and running else '当前运行' if running else '最近一次运行'}</span><h3>{_text(run_label, 160)}</h3><p>{_text(current.get('detail') or '后台流程和今日节点分开记录，前一交易日的复核不会代表今天已完成。')}</p><dl><div><dt>处理交易日</dt><dd>{_text(current.get('session') or current.get('business_session') or '—')}</dd></div><div><dt>计划目标日</dt><dd>{_text(current.get('target_session') or '—')}</dd></div>{run_times}</dl></div></section>
<section class="panel"><div class="side-body"><span class="side-tag">{'云端展示' if scope == 'cloud' else '云端发布'}</span><h3>{publish_label}</h3><p>{publish_detail}</p><dl>{pub_rows}</dl></div></section>
<section class="panel"><div class="side-body"><h3>状态说明</h3><p>首页只展示运行情况。查看成交、持仓、资金和策略计划，请进入交易看板。</p><p style="margin-bottom:0">本页每60秒检查更新。开关机、网络和后台任务耗时可能影响更新时间。</p></div></section></aside></main>
<footer><span>simulate-china-stocks · 运行状态与交易事实分开呈现</span><span>只读展示 · 刷新页面不触发交易或模型调用</span></footer>
</div><script type="application/json" id="run-home-data">{payload}</script><script>
(()=>{{'use strict';const data=JSON.parse(document.getElementById('run-home-data').textContent),fresh=data.freshness;
const fmt=new Intl.DateTimeFormat('zh-CN',{{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}});
document.querySelectorAll('time[data-iso]').forEach(el=>{{const d=new Date(el.dataset.iso);if(!Number.isNaN(d.getTime())){{el.textContent=fmt.format(d);el.dateTime=el.dataset.iso;el.title=el.dataset.iso}}}});
const storageKey='run-home-view:'+location.pathname;try{{const saved=JSON.parse(sessionStorage.getItem(storageKey)||'null');if(saved){{document.querySelectorAll('details[data-section]').forEach(d=>{{d.open=(saved.open||[]).includes(d.dataset.section)}});requestAnimationFrame(()=>window.scrollTo(0,saved.scroll||0))}}}}catch(_){{}}
window.addEventListener('beforeunload',()=>{{try{{sessionStorage.setItem(storageKey,JSON.stringify({{scroll:window.scrollY,open:Array.from(document.querySelectorAll('details[open]')).map(d=>d.dataset.section)}}))}}catch(_){{}}}});
function tick(){{const now=Date.now(),measureTime=fresh.scope==='cloud'?Date.parse(fresh.sampled_at):now;document.getElementById('live-clock').textContent=new Intl.DateTimeFormat('zh-CN',{{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}}).format(now);
document.querySelectorAll('[data-elapsed-from]').forEach(el=>{{const start=Date.parse(el.dataset.elapsedFrom);if(Number.isFinite(start)&&Number.isFinite(measureTime)){{const n=Math.max(0,Math.floor((measureTime-start)/1000));el.textContent=Math.floor(n/3600)+'小时 '+Math.floor(n%3600/60)+'分 '+n%60+'秒'}}}});
const sampled=Date.parse(fresh.sampled_at),deadline=Date.parse(fresh.next_due_at),published=Date.parse(fresh.published_at);const uncertain=!Number.isFinite(sampled)||sampled>now+300000||(Number.isFinite(published)&&published>now+300000)||(fresh.scope==='cloud'&&!Number.isFinite(deadline));const stale=fresh.scope==='cloud'?(Number.isFinite(deadline)&&now>=deadline):(Number.isFinite(sampled)&&now-sampled>fresh.stale_after_seconds*1000);const notice=document.getElementById('stale-notice');
if(uncertain){{notice.hidden=false;notice.textContent='采样或发布时间无法确认，请检查来源记录和本机时钟。';document.getElementById('summary-label').textContent='状态时点待确认'}}else if(stale){{notice.hidden=false;notice.textContent=fresh.scope==='cloud'?'下一次计划同步时间已到，新的发布尚未到达。以下仍是上一次成功上传的快照，等待本机补传。':'状态已超过30分钟未更新。以下为上次采样结果，请以新的后台记录为准。';document.getElementById('summary-label').textContent=fresh.scope==='cloud'?'云端等待下一次更新':'状态更新滞后'}}
document.querySelectorAll('.node[data-planned-at]').forEach(el=>{{const badge=el.querySelector('.badge');if(badge&&badge.dataset.state==='SCHEDULED'&&Number.isFinite(Date.parse(el.dataset.plannedAt))&&now>=Date.parse(el.dataset.plannedAt)){{badge.textContent='待后台确认'}}}});
}}tick();setInterval(tick,1000);}})();
</script></body></html>'''
