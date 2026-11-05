"""Pure calendar and readiness presentation for the local evening lamp."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from china_stock_sim.common.fees import aware_time
from china_stock_sim.market.calendar import TradingCalendar

TZ = ZoneInfo('Asia/Shanghai')
LABELS = {'READY': '可关机', 'WORKING': '业务处理中', 'WAITING': '尚未就绪',
          'BLOCKED': '需要处理', 'UNKNOWN': '待核查'}
MESSAGES = {
    'DAYTIME_WINDOW': '今晚检查尚未开始，18:10后按今日交易日重新核查',
    'CALENDAR_UNAVAILABLE': '真实交易日历或下一交易日无法确认',
    'ACCOUNT_MISSING': '配置账户不存在', 'ACCOUNT_STRATEGY_MISMATCH': '账户策略身份不符',
    'ACCOUNT_NOT_ACTIVE_FOR_DAY': '账户尚未覆盖本业务日的观察起点',
    'CONTINUOUS_WATERMARK_PENDING': '连续账务或估值水位尚未覆盖本业务日',
    'FINAL_STATEMENT_PENDING': '本业务日日结尚未达到COMPLETE/FINAL',
    'RESTATEMENT_PENDING': '相关账单仍待重述',
    'HISTORICAL_ORDERS_PENDING': '本业务日及以前还有未终结订单',
    'TAX_OBLIGATION_PENDING': '存在待核或待缴的税款义务',
    'SHARE_ALLOCATION_PENDING': '权益股份成本仍待分配',
    'DUE_ENTITLEMENT_PENDING': '本业务日已到期的权益仍未到账',
    'ENTITLEMENT_KIND_UNKNOWN': '权益类别无法确认，不能判断到期义务',
    'CASH_RESERVATION_UNFUNDED': '当前现金不足以覆盖冻结预留与已知税负',
    'CORPORATE_ACTION_PENDING': '存在影响本周期的未决权益事项',
    'NEXT_PLAN_PENDING': '下一交易日尚无合法冻结计划',
    'PLAN_PROOF_UNAVAILABLE': '冻结计划的既有完整性证明缺失或已失效',
    'DECISION_PROOF_UNAVAILABLE': '冻结决策的既有完整性证明缺失或已失效',
    'BUSINESS_REVIEW_UNAVAILABLE': '尚无本周期成功业务复核记录',
    'NEW_FACTS_PENDING_REVIEW': '新市场事实尚待业务复核',
    'FINANCIAL_WRITER_ACTIVE': '账本写入正在进行，稍后再检查',
    'WORKFLOW_ACTIVE': '业务阶段仍在处理',
    'WRITER_IDENTITY_UNKNOWN': '当前写入者或阶段无法可靠确认',
    'INPUT_CHANGED_DURING_PROBE': '采样期间状态变化，等待下一次一致核查',
    'MAINTENANCE_ACTIVE': '项目正在维护',
    'MAINTENANCE_UNKNOWN': '维护状态无法确认',
    'WAITING_BUSINESS_WINDOW': '仍待完成，原自动业务将在19:00后继续',
    'LEDGER_UNAVAILABLE': '本地账本暂不可读',
    'MARKET_UNAVAILABLE': '本地市场索引暂不可读',
    'JOURNAL_UNAVAILABLE': '本地业务阶段记录暂不可读',
    'PROBE_FAILED': '本次只读核查未能完成',
    'ACCOUNT_CONFIGURATION_INVALID': '三个原账户的配置身份无法确认',
}


def reason(code):
    return {'code': code, 'message': MESSAGES.get(code, '持久化事实或证据不一致，需核查（' + code + '）')}


def calendar_window(rows, now):
    now = aware_time(now).astimezone(TZ)
    calendar = TradingCalendar(rows)
    today = now.date().isoformat()
    if today not in calendar.days:
        raise ValueError('missing calendar')
    daytime = calendar.days[today] and time(9) <= now.time() < time(18, 10)
    if calendar.days[today] and (daytime or now.time() >= time(18, 10)):
        day = today
    else:
        previous = calendar.previous_session(today)
        if not previous.ready:
            raise ValueError('missing previous session')
        day = previous.data
    following = calendar.next_session(day)
    if not following.ready:
        raise ValueError('missing next session')
    target = following.data
    boundaries = [now + timedelta(seconds=90),
                  datetime.combine(now.date() + timedelta(days=1), time(), TZ),
                  datetime.fromisoformat(target + 'T09:00:00+08:00')]
    if calendar.days[today]:
        boundaries.extend(datetime.combine(now.date(), t, TZ) for t in (time(9), time(18, 10), time(19)))
    expires = min(value for value in boundaries if value > now)
    return {'business_session': day, 'target_session': target,
            'daytime': daytime, 'expires_at': expires.isoformat()}


def envelope(now, window=None):
    now = aware_time(now).astimezone(TZ)
    window = window or {}
    return {'schema_version': 1, 'state': 'UNKNOWN', 'label': LABELS['UNKNOWN'],
            'business_session': window.get('business_session'), 'target_session': window.get('target_session'),
            'sampled_at': now.isoformat(), 'expires_at': window.get('expires_at', (now + timedelta(seconds=30)).isoformat()),
            'accounts': [], 'reasons': [], 'optional_work': []}


def evaluate(facts, now, window, *, operational=None):
    output = envelope(now, window)
    if window['daytime']:
        return {**output, 'label': '待晚间', 'reasons': [reason('DAYTIME_WINDOW')]}
    for row in facts['accounts']:
        codes = list(dict.fromkeys(row['reasons']))
        ready = row['state'] == 'READY' and not codes
        plan = row.get('plan') or {}
        summary = (f"日结完成；下一交易日{'合法空计划' if plan.get('status') == 'EMPTY' else str(plan.get('order_count', 0)) + '笔冻结订单'}"
                   if ready else '；'.join(reason(code)['message'] for code in codes))
        output['accounts'].append({'account_id': row['account_id'], 'ready': ready,
                                   'summary': summary, 'reasons': [reason(code) for code in codes]})
    op = operational or {'state': 'READY', 'reasons': [], 'optional_work': []}
    output['optional_work'] = op.get('optional_work', [])
    output['reasons'] = [reason(code) for code in op.get('reasons', [])]
    states = {row['state'] for row in facts['accounts']} | {op['state']}
    if 'BLOCKED' in states:
        state = 'BLOCKED'
    elif 'UNKNOWN' in states:
        state = 'UNKNOWN'
    elif op['state'] == 'WORKING':
        state = 'WORKING'
    elif op['state'] != 'READY' or not output['accounts'] or not all(row['ready'] for row in output['accounts']):
        state = 'WAITING'
    else:
        state = 'READY'
    if state == 'WAITING' and time(18, 10) <= now.time() < time(19):
        output['reasons'].append(reason('WAITING_BUSINESS_WINDOW'))
    return {**output, 'state': state, 'label': LABELS[state]}
