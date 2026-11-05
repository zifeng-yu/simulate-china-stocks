"""One bounded wall-clock schedule for launchd rendering and status projection."""

from datetime import datetime, time, timedelta

from china_stock_sim.common.fees import aware_time


def _slots(minutes, daily_times):
    if type(minutes) is not int or not 1 <= minutes <= 60:
        raise ValueError('清醒检查间隔须为1至60整数分钟')
    slots = set(range(0, 1440, minutes))
    extras = set()
    for daily in daily_times:
        if (not isinstance(daily, time) or daily.tzinfo is not None
                or daily.second or daily.microsecond):
            raise ValueError('日历额外时点须为无时区的整分钟时间')
        extras.add(daily.hour * 60 + daily.minute)
    return slots, extras - slots


def calendar_entries(minutes, *, daily_times=()):
    """Align each day at midnight, without overlapping calendar predicates.

    Existing 1..60-minute configuration remains valid. When the cadence does
    not divide the day, its final gap to midnight is shorter than the cadence.
    """
    slots, extras = _slots(minutes, daily_times)
    if 60 % minutes == 0:
        entries = [{'Minute': minute} for minute in range(0, 60, minutes)]
    else:
        entries = [{'Hour': slot // 60, 'Minute': slot % 60} for slot in sorted(slots)]
    entries.extend({'Hour': slot // 60, 'Minute': slot % 60} for slot in sorted(extras))
    return entries


def next_calendar_check(now, minutes, *, daily_times=()):
    """Next strictly future planned tick, not a promise of actual OS startup."""
    now = aware_time(now)
    slots, extras = _slots(minutes, daily_times)
    midnight = datetime.combine(now.date(), time(), now.tzinfo)
    for slot in sorted(slots | extras):
        candidate = midnight + timedelta(minutes=slot)
        if candidate > now:
            return candidate
    return midnight + timedelta(days=1)
