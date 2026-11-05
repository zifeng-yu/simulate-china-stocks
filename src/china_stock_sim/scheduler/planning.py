"""Pure Shanghai wall-clock planning over explicit exchange-calendar facts."""

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from china_stock_sim.common.fees import aware_time
from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.scheduler.calendar_schedule import next_calendar_check

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("调度时间须为HH:MM")
    return time.fromisoformat(value)


def automatic_window(now, schedule):
    """A trigger gate, independent of calendar completion and retry progress."""
    now = aware_time(now).astimezone(SHANGHAI)
    start, end = schedule.get('quiet_start'), schedule.get('quiet_end')
    if start is None and end is None:
        return {'state': 'READY', 'quiet': False, 'next_allowed_at': now.isoformat()}
    start, end = _clock(start), _clock(end)
    if start >= end:
        raise ValueError('业务避让窗口须为同一日内开始早于结束')
    quiet = start <= now.time() < end
    return {'state': 'SKIPPED' if quiet else 'READY', 'quiet': quiet,
            'reason': 'SCHEDULE_QUIET_WINDOW' if quiet else None,
            'quiet_start': start.strftime('%H:%M'), 'quiet_end': end.strftime('%H:%M'),
            'next_allowed_at': (datetime.combine(now.date(), end, SHANGHAI)
                                if quiet else now).isoformat()}


def plan_schedule(now, calendar_rows, *, daily_start="18:10", plan_cutoff="09:00",
                  awake_check_minutes=15):
    """No IO, weekday inference, synthetic history, or account side effects."""
    now = aware_time(now).astimezone(SHANGHAI)
    start, cutoff = _clock(daily_start), _clock(plan_cutoff)
    if type(awake_check_minutes) is not int or not 1 <= awake_check_minutes <= 60:
        raise ValueError("清醒检查间隔须为1至60整数分钟")
    calendar = TradingCalendar(calendar_rows)
    today = now.date().isoformat()
    next_check = next_calendar_check(now, awake_check_minutes, daily_times=(start,))
    daily_time = datetime.combine(now.date(), start, SHANGHAI)
    result = {"state": "WAITING_CALENDAR", "timezone": "Asia/Shanghai",
              "checked_at": now.isoformat(), "completed_session": None,
              "planning_target": None, "planning_deadline": None, "plan_allowed": False,
              "month_end": False, "last_completed_month_end": None,
              "trading_sessions": [], "next_check_at": next_check.isoformat(), "issues": []}
    if today not in calendar.days:
        result["issues"] = [{"kind": "MISSING_CALENDAR", "date": today}]
        return result
    if calendar.days[today] and now >= daily_time:
        completed = today
    else:
        previous = calendar.previous_session(today)
        if not previous.ready:
            result["issues"] = list(previous.issues)
            return result
        completed = previous.data
    result["completed_session"] = completed
    known = calendar.between(min(calendar.days), completed)
    result["trading_sessions"] = known.data
    if not known.ready:
        result["issues"] = list(known.issues)
        return result
    following = calendar.next_session(completed)
    if not following.ready:
        result["issues"] = list(following.issues)
        return result
    target = following.data
    deadline = datetime.combine(datetime.fromisoformat(target).date(), cutoff, SHANGHAI)
    result.update(state="READY", planning_target=target, planning_deadline=deadline.isoformat(),
                  plan_allowed=now < deadline)
    result["month_end"] = calendar.is_month_end(completed).data is True
    for session in reversed(known.data):
        boundary = calendar.is_month_end(session)
        if boundary.ready and boundary.data:
            result["last_completed_month_end"] = session
            break
    return result
