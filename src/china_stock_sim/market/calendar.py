"""Calendar operations use published source rows, never weekday inference."""

from datetime import date, timedelta

from china_stock_sim.market.contracts import DataResult, session_date


class TradingCalendar:
    def __init__(self, rows: list[dict]):
        self.days = {}
        for row in rows:
            day = session_date(row.get("date", row.get("calendar_date")))
            flag = row.get("is_trading_day")
            if str(flag) not in {"0", "1"} or day in self.days:
                raise ValueError("交易日历重复或状态无效")
            self.days[day] = str(flag) == "1"

    def between(self, start: str, end: str) -> DataResult:
        start, end = session_date(start), session_date(end)
        if start > end:
            raise ValueError("日历起始日晚于截止日")
        current, last = date.fromisoformat(start), date.fromisoformat(end)
        missing, sessions = [], []
        while current <= last:
            day = current.isoformat()
            if day not in self.days:
                missing.append(day)
            elif self.days[day]:
                sessions.append(day)
            current += timedelta(days=1)
        return DataResult("WAITING_DATA" if missing else "READY", sessions,
                          tuple({"kind": "MISSING_CALENDAR", "date": day} for day in missing))

    def adjacent(self, day: str, direction: int) -> DataResult:
        current = date.fromisoformat(session_date(day))
        for _ in range(366):
            current += timedelta(days=direction)
            key = current.isoformat()
            if key not in self.days:
                return DataResult("WAITING_DATA", None, ({"kind": "MISSING_CALENDAR", "date": key},))
            if self.days[key]:
                return DataResult("READY", key)
        return DataResult("WAITING_DATA", None, ({"kind": "CALENDAR_RANGE_EXHAUSTED"},))

    def next_session(self, day: str) -> DataResult:
        return self.adjacent(day, 1)

    def previous_session(self, day: str) -> DataResult:
        return self.adjacent(day, -1)

    def is_month_end(self, day: str) -> DataResult:
        key = session_date(day)
        if key not in self.days:
            return DataResult("WAITING_DATA", None, ({"kind": "MISSING_CALENDAR", "date": key},))
        if not self.days[key]:
            return DataResult("READY", False)
        following = self.next_session(key)
        return (DataResult("READY", following.data[:7] != key[:7]) if following.ready else following)

    def window(self, end: str, size: int) -> DataResult:
        end = session_date(end)
        if type(size) is not int or size < 1:
            raise ValueError("交易日窗口必须是正整数")
        current, sessions = end, []
        if current not in self.days:
            return DataResult("WAITING_DATA", [], ({"kind": "MISSING_CALENDAR", "date": end},))
        if self.days[current]:
            sessions.append(current)
        while len(sessions) < size:
            previous = self.previous_session(current)
            if not previous.ready:
                return DataResult("WAITING_DATA", sorted(sessions), previous.issues)
            current = previous.data
            sessions.append(current)
        return DataResult("READY", sorted(sessions))
