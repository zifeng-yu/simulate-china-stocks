"""A bounded display projection, separate from workflow execution and financial audit."""

import json
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from china_stock_sim.ledger.monitoring import MAX_ACCOUNTS, read_runtime_facts
from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import aware_time
from china_stock_sim.market.service import MarketDataService

from .planning import automatic_window, plan_schedule
from .state import WorkflowJournal

TZ = ZoneInfo('Asia/Shanghai')
PHASES = {'calendar', 'data', 'month_end', 'input_review', 'recovery', 'settlement',
          'entitlements_before', 'execution', 'entitlements_after', 'close',
          'report_daily', 'plan', 'report_plan', 'backup'}
TITLES = {'morning_plan': '盘前计划', 'data': '日终数据准备', 'month_end': '月末换池及补缺',
          'input_review': '迟到输入复查', 'settlement': '权益、撮合与日结',
          'report_daily': '日结后报告', 'next_plan': '下一交易日计划',
          'report_plan': '规划后报告', 'backup': '备份预检及归档',
          'calendar': '交易日历', 'recovery': '历史恢复', 'plan': '策略规划',
          'entitlements_before': '交易前权益', 'execution': '原计划撮合',
          'entitlements_after': '交易后权益及税款', 'close': '日结'}
LABELS = {'SCHEDULED': '待时', 'PENDING': '等待处理', 'WAITING_UPSTREAM': '等待上游',
          'RUNNING': '运行中', 'COMPLETE': '已完成', 'WAITING_DATA': '等待数据',
          'COOLDOWN': '冷却中', 'FAILED': '失败', 'INTERRUPTED': '运行中断',
          'DISABLED': '已禁用', 'NOT_REQUIRED': '无需执行', 'UNKNOWN': '状态待确认',
          'MISSED': '已过计划截止', 'MAINTENANCE': '维护中'}
STRATEGIES = {'S1': 's1', 'S1-LT': 's1_lt', 'LLM-Daily': 'llm'}
MAX_PHASES = 2000
REUSED = {'COMPLETE_BACKUP_ALREADY_EXISTS', 'CURRENT_REPORT_ALREADY_PUBLISHED',
          'ALREADY_FROZEN', 'FROZEN_PLAN_EXISTS', 'UNCHANGED', 'ALREADY_APPLIED'}


def _text(value, limit=160):
    if not isinstance(value, str):
        return None
    # Do not serialize arbitrary errors, endpoint credentials, or private paths.
    if re.search(r'(?i)(https?://|bearer\s|sk-[\w-]+|api[_-]?key|/Users/|/home/|Traceback)', value):
        return '内容已隐藏'
    return ''.join(c for c in value if c.isprintable())[:limit]


def _day(value):
    try:
        return value if isinstance(value, str) and date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _instant(value):
    try:
        return aware_time(value).astimezone(TZ) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _iso(value):
    parsed = _instant(value)
    return parsed.isoformat() if parsed else None


def _at(day, value):
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(value), TZ) if day else None


def _view(state, detail='', **extra):
    return {'state': state, 'label': LABELS.get(state, LABELS['UNKNOWN']),
            'detail': detail, 'started_at': None, 'finished_at': None,
            'next_retry_at': None, **extra}


def _reason(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', value) else None


def _result(value):
    value = value if isinstance(value, dict) else {}
    out = {key: _reason(value.get(key)) for key in ('state', 'status', 'reason')}
    out['next_retry_at'] = _iso(value.get('next_retry_at'))
    out['completion'] = value.get('completion') if type(value.get('completion')) is bool else None
    out['reasons'] = [_reason(row.get('kind')) for row in value.get('reasons', [])[:8]
                      if isinstance(row, dict) and _reason(row.get('kind'))] if isinstance(
                          value.get('reasons'), list) else []
    out['published'] = [{key: row[key] for key in ('account_id', 'report_revision')
                        if key in row and (key == 'account_id' and isinstance(row[key], str)
                                           or key != 'account_id' and type(row[key]) is int)}
                       for row in value.get('published', [])[:MAX_ACCOUNTS]
                       if isinstance(row, dict)] if isinstance(value.get('published'), list) else []
    return out


def _probe(pid):
    try:
        from .process import probe_process
        return probe_process(pid)
    except (ImportError, OSError, ValueError):
        return {'state': 'UNKNOWN', 'birth': None}


def _current_run(journal, now, probe):
    empty = {'state': 'NOT_STARTED', 'phase': None, 'label': '尚无运行记录',
             'session': None, 'target_session': None, 'started_at': None,
             'finished_at': None, 'elapsed_seconds': None, 'run_id': None,
             'identity_verified': False}
    row = journal.get('runs', {}).get(journal.get('current'))
    if not row:
        if journal.get('current') is not None or journal.get('current_run') is not None or journal.get(
                'state') not in {None, 'NOT_STARTED'}:
            return {**empty, 'state': 'UNKNOWN', 'label': '流程记录缺少可确认的当前尝试'}
        return empty
    attempt = row.get('attempts', [])[-1] if row.get('attempts') else {}
    metadata = attempt.get('metadata', {})
    result = attempt.get('result') or {}
    start, finish = _instant(attempt.get('started_at')), _instant(attempt.get('finished_at'))
    state = attempt.get('state', 'UNKNOWN')
    if state not in {'RUNNING', 'COMPLETE', 'PARTIAL', 'FAILED', 'WAITING', 'INTERRUPTED', 'SKIPPED'}:
        state = 'UNKNOWN'
    verified = False
    if not start or start > now or finish and (finish < start or finish > now):
        state = 'UNKNOWN'
    elif state == 'RUNNING':
        birth = metadata.get('process_birth')
        if not isinstance(birth, str) or not birth or type(attempt.get('pid')) is not int:
            state = 'UNKNOWN'
        else:
            try:
                observed = probe(attempt['pid'])
            except Exception:
                observed = {}
            if observed.get('state') == 'DEAD' or observed.get('birth') and observed['birth'] != birth:
                state = 'INTERRUPTED'
            elif observed.get('state') == 'ALIVE' and observed.get('birth') == birth:
                verified = True
            else:
                state = 'UNKNOWN'
    elif not finish:
        state = 'UNKNOWN'
    return {**empty, 'state': state, 'label': LABELS.get(state, {
        'PARTIAL': '部分完成', 'WAITING': '等待处理', 'SKIPPED': '已跳过'}.get(state, '状态待确认')),
        'session': _day(metadata.get('business_session')) or _day(result.get('session')),
        'target_session': _day(metadata.get('target_session')) or _day(result.get('target_session')),
        'started_at': start.isoformat() if start else None,
        'finished_at': finish.isoformat() if finish else None,
        'elapsed_seconds': max(0, int(((finish or now) - start).total_seconds())) if start else None,
        'run_id': _text(row.get('run_id'), 80), 'identity_verified': verified}


def _phase_rows(journal, current, now):
    rows = []
    for run_id, run in journal.get('runs', {}).items():
        for attempt_index, attempt in enumerate(run.get('attempts', [])):
            starts = {}
            for phase_index, value in enumerate(attempt.get('phases', [])):
                if not isinstance(value, dict) or value.get('phase') not in PHASES:
                    continue
                stamp = _instant(value.get('recorded_at'))
                if not stamp:
                    continue
                key = (value['phase'], _day(value.get('session')),
                       _day(value.get('target_session')), value.get('account_id'))
                if key[3] is not None and not isinstance(key[3], str):
                    continue
                state = value.get('state')
                if state == 'RUNNING':
                    starts[key] = stamp.isoformat()
                effective = state
                attempt_state = current['state'] if run_id == journal.get('current') and attempt is run.get(
                    'attempts', [])[-1] else attempt.get('state')
                if state == 'RUNNING' and attempt_state != 'RUNNING':
                    effective = 'INTERRUPTED' if attempt_state == 'INTERRUPTED' else 'UNKNOWN'
                if stamp > now:
                    effective = 'UNKNOWN'
                rows.append({'phase': key[0], 'session': key[1], 'target_session': key[2],
                    'account_id': key[3], 'state': effective, 'source_state': state,
                    'recorded_at': stamp.isoformat(),
                    'started_at': starts.get(key), 'finished_at': stamp.isoformat() if state != 'RUNNING' else None,
                    'result': _result(value.get('result')), 'run_id': run_id,
                    '_sequence': (attempt_index, phase_index)})
    rows.sort(key=lambda row: (_instant(row['recorded_at']), row['run_id'] == journal.get('current'),
                              row['_sequence']))
    return rows[-MAX_PHASES:]


def _latest(rows, phase, day, *, target=None, account=None):
    matches = [row for row in rows if row['phase'] == phase and row['session'] == day
               and (target is None or row['target_session'] == target)
               and row['account_id'] in (None, account)]
    if not matches:
        return None
    latest = matches[-1]
    if account is not None and latest['account_id'] is None:
        exact = [row for row in matches if row['account_id'] == account and row['run_id'] == latest['run_id']]
        if exact:
            return exact[-1]
    return latest


def _phase_view(row, now, *, fallback='PENDING'):
    if not row:
        return _view(fallback, '尚无本业务日可确认的阶段记录')
    result, state = row['result'], row['state']
    reason = result.get('reason')
    if result.get('state') in {'CONFIG_CHANGED', 'STALE_INPUT', 'MODEL_FAILED', 'INVALID', 'INTEGRITY_ERROR'}:
        state = 'FAILED'
    elif state == 'SKIPPED':
        state = ('COMPLETE' if reason in REUSED else 'DISABLED' if reason and reason.endswith('_DISABLED')
                 else 'NOT_REQUIRED' if reason in {'NO_ACTIVE_ACCOUNTS', 'NOT_MONTH_END',
                     'ACCOUNT_NOT_ACTIVATED', 'NOT_DUE', 'NO_WORK_REQUIRED'} else 'UNKNOWN')
    elif state == 'WAITING':
        retry = _instant(result.get('next_retry_at'))
        state = 'COOLDOWN' if retry and retry > now else 'WAITING_DATA'
    elif state not in LABELS:
        state = 'UNKNOWN'
    if state == 'COMPLETE' and (result.get('completion') is False or result.get('state') in {
            'PARTIAL', 'FAILED', 'WAITING', 'WAITING_DATA', 'ERROR'}):
        state = 'UNKNOWN'
    detail = '已完成，复验复用' if state == 'COMPLETE' and reason in REUSED else ''
    if reason and state != 'COMPLETE':
        detail = '阶段原因：' + reason
    return _view(state, detail, started_at=row['started_at'], finished_at=row['finished_at'],
                 next_retry_at=result.get('next_retry_at'), reason=reason)


def _combine(rows, *, unit='户'):
    if not rows:
        return _view('NOT_REQUIRED', '没有已登记账户')
    priority = ('FAILED', 'INTERRUPTED', 'UNKNOWN', 'MISSED', 'RUNNING', 'COOLDOWN',
                'WAITING_DATA', 'WAITING_UPSTREAM', 'PENDING', 'SCHEDULED', 'DISABLED')
    state = next((state for state in priority if any(row['state'] == state for row in rows)),
                 'COMPLETE' if any(row['state'] == 'COMPLETE' for row in rows) else 'NOT_REQUIRED')
    counts = {state: sum(row['state'] == state for row in rows) for state in dict.fromkeys(
        row['state'] for row in rows)}
    detail = '；'.join(f'{LABELS.get(key, key)} {count} {unit}' for key, count in counts.items())
    starts = [row['started_at'] for row in rows if row.get('started_at')]
    finishes = [row['finished_at'] for row in rows if row.get('finished_at')]
    return _view(state, detail, started_at=min(starts) if starts else None,
                 finished_at=max(finishes) if finishes and all(row['state'] in {
                     'COMPLETE', 'NOT_REQUIRED'} for row in rows) else None)


def _account_gate(account, day):
    if account.get('state') != 'READY':
        return _view('UNKNOWN', '该账户的账本摘要无法确认')
    start = _day(account.get('simulation_start_session'))
    if not start:
        return _view('DISABLED', '账户尚未激活观察')
    if not day:
        return _view('UNKNOWN', '交易日期未知')
    if start > day:
        return _view('NOT_REQUIRED', '尚未到该账户观察起点')
    return None


def _session(account, day):
    value = account.get('sessions', {}).get(day, {})
    return value if isinstance(value, dict) else {}


def _plan(account, day, previous, rows, now, settings):
    gate = _account_gate(account, day)
    if gate:
        return gate
    phase = _latest(rows, 'plan', previous, target=day, account=account['account_id'])
    persisted = _session(account, day).get('plan')
    if persisted and persisted.get('status') in {'FROZEN', 'EMPTY'}:
        frozen = _instant(persisted.get('frozen_at'))
        if persisted.get('target_session') != day or not frozen or frozen > now or frozen >= _at(day, '09:00'):
            return _view('UNKNOWN', '冻结记录的时点无法确认')
        if phase and phase['source_state'] != 'RUNNING' and _phase_view(phase, now)['state'] in {
                'FAILED', 'UNKNOWN'}:
            return _view('UNKNOWN', '已有冻结计划，但本轮核查结果存在差异')
        return _view('COMPLETE', '已保存合法空计划' if persisted['status'] == 'EMPTY' else '原计划已冻结',
                     finished_at=frozen.isoformat(), order_count=persisted.get('order_count'))
    key = STRATEGIES.get(account.get('strategy_id'))
    if not key or not settings.raw['strategies'].get(key, {}).get('enabled'):
        return _view('DISABLED', '该账户策略未启用')
    if now >= _at(day, '09:00'):
        return _view('MISSED', '已过09:00且没有冻结计划，不补造历史交易',
                     reason=_reason(_session(account, day).get('no_plan_reason')))
    if phase:
        value = _phase_view(phase, now)
        return _view('UNKNOWN', '阶段报告完成，但未找到冻结记录') if value['state'] == 'COMPLETE' else value
    if previous and account.get('simulation_start_session') != day and (
            not account.get('settled_through') or account['settled_through'] < previous
            or not account.get('valued_through') or account['valued_through'] < previous):
        return _view('WAITING_UPSTREAM', '等待前一交易日账务和估值完成')
    return _view('PENDING', '等待在09:00前完成规划')


def _settlement(account, day, rows, now):
    phase = _latest(rows, 'settlement', day, account=account['account_id'])
    statement = _session(account, day).get('statement')
    if statement:
        if statement.get('requires_restatement') or account.get('restate_from') and day >= account['restate_from']:
            return _view('WAITING_DATA', '已发布账单待重述，历史原账单保留')
        if statement.get('accounting_status') == 'COMPLETE' and statement.get('valuation_status') == 'FINAL':
            processed = _instant(statement.get('processed_at'))
            if not processed or processed > now:
                return _view('UNKNOWN', '日结记录时点无法确认')
            if _session(account, day).get('pending_order_count', 0):
                return _view('UNKNOWN', '日结与未决订单记录存在差异')
            if phase and phase['source_state'] != 'RUNNING' and _phase_view(phase, now)['state'] in {
                    'FAILED', 'UNKNOWN'}:
                return _view('UNKNOWN', '已发布日结，但本轮核查结果存在差异')
            if not account.get('settled_through') or account['settled_through'] < day or not account.get(
                    'valued_through') or account['valued_through'] < day:
                return _view('WAITING_UPSTREAM', '该日账单已保存，前序连续账务仍待完成')
            return _view('COMPLETE', '账务完成且估值FINAL', finished_at=_iso(statement.get('processed_at')))
        return _view('WAITING_DATA', '账务或估值尚未全部完成')
    value = _phase_view(phase, now, fallback='WAITING_UPSTREAM')
    return _view('UNKNOWN', '阶段报告完成，但未找到该日日结') if value['state'] == 'COMPLETE' else value


def _scheduler_public(value):
    value = value if isinstance(value, dict) else {}
    result = {key: value.get(key) if type(value.get(key)) is bool else None
              for key in ('enabled', 'installed', 'loaded', 'file_matches', 'supported')}
    result.update(state=_reason(value.get('state')) or 'UNKNOWN',
                  query_error=_reason(value.get('query_error')),
                  label=_text(value.get('label'), 96))
    runtime = value.get('runtime') if isinstance(value.get('runtime'), dict) else {}
    result['runtime'] = {'state': runtime.get('state') if runtime.get('state') in {
        'RUNNING', 'IDLE', 'UNKNOWN'} else 'UNKNOWN',
        'pid': runtime.get('pid') if type(runtime.get('pid')) is int and runtime['pid'] > 0 else None,
        'last_exit_code': runtime.get('last_exit_code') if type(runtime.get('last_exit_code')) is int else None}
    return result


def _schedulers(settings, supplied):
    if supplied is None:
        try:
            from .launchd import status
            workflow = status(settings)
        except Exception:
            workflow = {'state': 'UNKNOWN'}
        try:
            from .dashboard_launchd import status as monitor_status
            monitor = monitor_status(settings)
        except Exception:
            monitor = {'state': 'UNKNOWN'}
    elif 'workflow' in supplied or 'monitor' in supplied:
        workflow, monitor = supplied.get('workflow', {}), supplied.get('monitor', {})
    else:
        workflow, monitor = supplied, {}
    return {'workflow': _scheduler_public(workflow), 'monitor': _scheduler_public(monitor)}


def _maintenance(settings):
    path = settings.root / 'data/state/dashboard-maintenance.json'
    if not path.exists():
        return {'state': 'INACTIVE', 'active': False}
    try:
        if path.is_symlink() or path.stat().st_size > 16_384:
            raise ValueError('不可读取维护状态')
        value = json.loads(path.read_text())
        if type(value.get('active')) is not bool:
            raise ValueError('维护状态无效')
        return {'state': 'ACTIVE' if value['active'] else 'INACTIVE', 'active': value['active']}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'state': 'UNKNOWN', 'active': None}


def _publication(settings, now):
    try:
        from china_stock_sim.publishing.state import read_publication_status
    except ModuleNotFoundError as exc:
        if exc.name == 'china_stock_sim.publishing':
            return {'state': 'NOT_CONFIGURED', 'configured': False, 'enabled': False,
                    'reason': 'LOCAL_COMPONENT_NOT_INSTALLED'}
        return {'state': 'UNKNOWN', 'reason': 'LOCAL_COMPONENT_IMPORT_FAILED'}
    except ImportError:
        return {'state': 'UNKNOWN', 'reason': 'LOCAL_COMPONENT_IMPORT_FAILED'}
    try:
        value = read_publication_status(settings.root, now=now)
        return {key: _text(value[key]) if isinstance(value[key], str) else value[key]
                for key in ('state', 'configured', 'enabled', 'last_success_at', 'last_success_slot',
                            'next_due_at',
                            'next_retry_at', 'source_sampled_at', 'published_at', 'mode', 'reason',
                            'last_error', 'scheduled_for', 'schedule', 'deployment')
                if key in value and isinstance(value[key], (str, bool, int, type(None)))}
    except Exception:
        return {'state': 'UNKNOWN'}


def build_status(settings, *, now=None, journal=None, calendar=None, ledger_facts=None,
                 process_probe=None, scheduler_info=None):
    """Read operational facts and construct today's timeline; never run business work."""
    now = aware_time(now or datetime.now(TZ)).astimezone(TZ)
    today = now.date().isoformat()
    schedule_config = settings.raw['schedule']
    window = automatic_window(now, schedule_config)
    daily_at = _at(today, schedule_config['daily_start'])
    calendar_error = None
    try:
        source = calendar if calendar is not None else MarketDataService(settings.root).calendar(
            knowledge_cutoff=now.isoformat())
        if hasattr(source, 'ready'):
            source = source.data if source.ready else []
        elif isinstance(source, dict):
            source = source.get('data', []) if source.get('state') == 'READY' else []
        cal = TradingCalendar(source)
        schedule = plan_schedule(now, source, **{key: schedule_config[key] for key in (
            'daily_start', 'plan_cutoff', 'awake_check_minutes')})
        trading = cal.days.get(today)
        following = cal.next_session(today)
        next_session = following.data if following.ready else None
        ready = schedule['state'] == 'READY' and trading is not None and next_session is not None
    except Exception:
        cal, schedule, trading, next_session, ready = None, {}, None, None, False
        calendar_error = 'CALENDAR_UNAVAILABLE'
    completed = _day(schedule.get('completed_session'))
    target = _day(schedule.get('planning_target'))
    day = today if trading else completed
    next_target = next_session if trading else target
    morning = today if trading else target
    before_eod = trading is True and now < daily_at
    requested = list(dict.fromkeys(value for value in (today, completed, day, target, next_target, morning) if value))
    try:
        facts = ledger_facts if ledger_facts is not None else read_runtime_facts(settings, requested)
        accounts = facts.get('accounts', [])[:MAX_ACCOUNTS]
    except Exception:
        facts, accounts = {'state': 'UNKNOWN'}, []
    journal_error = False
    try:
        raw = (WorkflowJournal(settings).read_current() if journal is None
               else journal.read_current() if hasattr(journal, 'read_current')
               else journal.read() if hasattr(journal, 'read') else journal)
        if (not isinstance(raw, dict) or not isinstance(raw.get('runs', {}), dict)
                or raw.get('schema_version', 1) != 1):
            raise ValueError('流程日志无效')
        current = _current_run(raw, now, process_probe or _probe)
        phases = _phase_rows(raw, current, now)
    except Exception:
        raw, phases, journal_error = {}, [], True
        current = {**_current_run({}, now, _probe), 'state': 'UNKNOWN', 'label': '流程状态无法确认'}
    active = {}
    for row in phases:
        if row['run_id'] == raw.get('current'):
            active[(row['phase'], row['session'], row['target_session'], row['account_id'])] = row
    pending = [row for row in active.values() if row['state'] == 'RUNNING']
    if current['state'] == 'RUNNING' and pending:
        row = max(pending, key=lambda value: value['recorded_at'])
        current.update(phase=row['phase'], label=TITLES[row['phase']] + '进行中',
                       phase_started_at=row['started_at'])
    elif current['state'] == 'RUNNING':
        current['label'] = '后台运行中，当前阶段尚未明确'

    public_accounts = []
    for account in accounts:
        identity = _text(account.get('account_id'), 128)
        name = _text(_session(account, today).get('display_name') or identity, 80)
        public_accounts.append({'account_id': identity, 'display_name': name,
            'strategy_id': _text(account.get('strategy_id'), 80),
            'state': 'READY' if account.get('state') == 'READY' else 'UNKNOWN',
            **{key: _day(account.get(key)) for key in ('simulation_start_session', 'settled_through',
                                                     'valued_through', 'restate_from')},
            **{key: account.get(key) if type(account.get(key)) is int else None
               for key in ('account_revision', 'report_revision', 'last_rendered_revision')}})
    nodes = []
    for node_id in ('morning_plan', 'data', 'month_end', 'input_review', 'settlement',
                    'report_daily', 'next_plan', 'report_plan', 'backup'):
        node_day, node_target = day, next_target
        per_account = []
        if node_id in {'morning_plan', 'next_plan'}:
            node_target = morning if node_id == 'morning_plan' else next_target
            previous = cal.previous_session(node_target) if cal and node_target else None
            node_day = previous.data if previous and previous.ready else None
        gate = (_view('UNKNOWN', '日历或日期范围无法确认') if not ready else
                _view('UNKNOWN', '账本摘要无法确认') if facts.get('state') not in {'READY', 'PARTIAL'} else
                _view('UNKNOWN', '流程日志无法确认') if journal_error else None)
        if gate is None and node_id != 'morning_plan' and before_eod:
            gate = _view('SCHEDULED', '等待今日18:10后的实际市场事实')
        if node_id == 'month_end' and ready:
            boundary = cal.is_month_end(day)
            if boundary.ready and not boundary.data:
                gate = _view('NOT_REQUIRED', '该业务日不是月末最后交易日')
                month = _day(schedule.get('last_completed_month_end'))
                historical = _latest(phases, 'month_end', month) if month and month != day else None
                if historical and _phase_view(historical, now)['state'] not in {'COMPLETE', 'NOT_REQUIRED'}:
                    node_day, node_target = month, historical['target_session']
                    gate = _phase_view(historical, now)
                    gate['detail'] = f'补办 {month} 月末维护；' + gate['detail']
        if node_id in {'data', 'month_end'} and not settings.raw['market']['enabled']:
            gate = _view('DISABLED', '市场采集功能未启用')
        if node_id in {'morning_plan', 'next_plan', 'input_review', 'settlement', 'report_daily', 'report_plan'}:
            for account, public in zip(accounts, public_accounts):
                value = _account_gate(account, node_target if node_id in {'morning_plan', 'next_plan'} else day)
                if value is None:
                    value = gate
                if value is None:
                    if node_id in {'morning_plan', 'next_plan'}:
                        value = _plan(account, node_target, node_day, phases, now, settings)
                    elif node_id == 'settlement':
                        value = (_settlement(account, day, phases, now) if settings.raw['execution']['enabled']
                                 else _view('DISABLED', '执行功能未启用'))
                    else:
                        phase = _latest(phases, node_id, day, target=next_target if node_id == 'report_plan'
                                        else None, account=account['account_id'])
                        value = _phase_view(phase, now, fallback='WAITING_UPSTREAM')
                        if node_id == 'input_review' and not settings.raw['execution']['enabled']:
                            value = _view('DISABLED', '执行功能未启用，不推进财务输入复查')
                        if node_id == 'report_plan' and value['state'] == 'COMPLETE' and account.get(
                                'report_revision') != account.get('last_rendered_revision'):
                            value = _view('WAITING_UPSTREAM', '当前报告修订尚未全部发布')
                value = {**value, 'account_id': public['account_id'], 'display_name': public['display_name']}
                if node_id == 'settlement':
                    value['phases'] = [{'id': phase, 'title': TITLES[phase], **(
                        gate or _account_gate(account, day) or _phase_view(
                            _latest(phases, phase, day, account=account['account_id']), now,
                            fallback='WAITING_UPSTREAM'))} for phase in (
                                'entitlements_before', 'execution', 'entitlements_after', 'close')]
                    if value['state'] == 'COMPLETE':
                        value['phases'][-1].update(_view('COMPLETE', '由已保存日结确认',
                                                       finished_at=value['finished_at']))
                per_account.append(value)
            overall = gate or _combine(per_account)
            # Account errors must stay visible even if peers are simply waiting for 18:10.
            if per_account and any(row['state'] in {'UNKNOWN', 'FAILED', 'INTERRUPTED'} for row in per_account):
                overall = _combine(per_account)
        else:
            overall = gate or _phase_view(_latest(phases, node_id, day), now)
        node = {'id': node_id, 'title': TITLES[node_id], **overall,
                'scheduled_at': _at(node_day, '18:10').isoformat() if node_day else None,
                'deadline_at': _at(node_target, '09:00').isoformat() if node_target
                    and node_id in {'morning_plan', 'next_plan'} else None,
                'session': node_day, 'target_session': node_target, 'accounts': per_account}
        if node_id == 'month_end':
            child = (overall if overall['state'] in {'SCHEDULED', 'DISABLED', 'NOT_REQUIRED'} else
                     _view('COMPLETE', '由本月完整维护回执确认') if overall['state'] == 'COMPLETE' else
                     _view('UNKNOWN', '整体维护尚未完成；没有独立子阶段完成证据'))
            node['details'] = [
                {'id': 'old_pool_data', 'title': 'D旧池维护', **(child if overall['state'] in {
                    'SCHEDULED', 'DISABLED', 'NOT_REQUIRED', 'COMPLETE'} else
                    _phase_view(_latest(phases, 'data', node_day), now, fallback='UNKNOWN'))},
                {'id': 'selection', 'title': '月末选池及历史准备', **child},
                {'id': 'new_pool_data', 'title': 'N新池显式补缺（行情仍截止D）', **child},
            ]
        if (window['quiet'] and node_id != 'morning_plan'
                and node.get('state') in {'SCHEDULED', 'PENDING', 'WAITING_UPSTREAM',
                                          'WAITING_DATA', 'COOLDOWN'}):
            resume = _instant(window['next_allowed_at'])
            retry = _instant(node.get('next_retry_at'))
            resume = max(resume, retry) if retry else resume
            state = 'COOLDOWN' if node['state'] == 'COOLDOWN' else 'SCHEDULED'
            node.update(state=state, label=LABELS[state],
                        detail=f'自动业务避让至{window["quiet_end"]}，随后首次检查继续；' + node['detail'],
                        next_retry_at=resume.isoformat())
        nodes.append(node)

    if not ready:
        summary = _view('UNKNOWN', '缺少可确认的交易日历；不按星期猜测业务日期')
    elif journal_error or facts.get('state') not in {'READY', 'PARTIAL'}:
        summary = _view('UNKNOWN', '无法确认流程或账本状态')
    elif current['state'] in {'RUNNING', 'INTERRUPTED', 'UNKNOWN'}:
        summary = _view(current['state'], f'本轮业务日 {current["session"] or "未知"}；'
                        + ('今日仍待18:10收盘处理' if before_eod else '各账户业务完成情况见节点'))
    elif not accounts:
        summary = _view('NOT_REQUIRED', '没有已登记账户，不自动开户或激活')
    elif not any(row.get('simulation_start_session') for row in accounts):
        summary = _view('DISABLED', '账户尚未激活观察')
    elif window['quiet']:
        summary = _view('SCHEDULED', f'自动业务{window["quiet_start"]}—{window["quiet_end"]}跳过；'
                        '到期工作在窗口结束后的首次检查或开机继续，不标记业务完成',
                        next_retry_at=window['next_allowed_at'])
    elif before_eod:
        summary = _view('SCHEDULED', '等待今日18:10收盘处理；历史运行完成不代表今日已日结')
    else:
        summary = _combine(nodes[1:], unit='项')
        if trading is False:
            summary['detail'] = f'非交易日，维护业务日 {day or "未知"}；' + summary['detail']
    if facts.get('accounts_truncated'):
        summary = _view('UNKNOWN', '账户数量超过本页显示上限，不能据此确认全部完成')
    maintenance = _maintenance(settings)
    if maintenance['active'] is True:
        summary = _view('MAINTENANCE', '处于明确维护窗口；下方保留真实业务状态')
    elif maintenance['state'] == 'UNKNOWN':
        summary = _view('UNKNOWN', '维护状态无法确认；下方保留已读取业务事实')
    return {'schema_version': 1, 'sampled_at': now.isoformat(), 'timezone': 'Asia/Shanghai',
        'today': today, 'calendar': {'state': 'READY' if ready else 'UNKNOWN',
            'is_trading_day': trading, 'completed_session': completed, 'planning_target': target,
            'next_session': next_session, 'next_business_at': (_at(today if trading else next_session,
                '18:10').isoformat() if trading or next_session else None),
            'next_check_at': _iso(schedule.get('next_check_at')), 'reason': calendar_error},
        'automatic_execution': window,
        'summary': summary, 'scheduler': _schedulers(settings, scheduler_info),
        'current_run': current, 'nodes': nodes, 'accounts': public_accounts,
        'freshness': {'local_stale_after_seconds': 1800}, 'maintenance': maintenance,
        'publication': _publication(settings, now),
        'limits': {'account_limit': MAX_ACCOUNTS, 'phase_limit': MAX_PHASES,
                   'accounts_truncated': bool(facts.get('accounts_truncated'))}}
