"""Read-only public facts API; importing or querying it cannot trigger collection."""

import sqlite3
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import DataResult, MarketSnapshot, aware_time, session_date
from china_stock_sim.market.freshness import collection_floor, coverage_freshness_issues
from china_stock_sim.market.quality import normalize_code
from china_stock_sim.market.repository import CorruptData, MarketRepository, timestamp


class MarketDataService:
    def __init__(self, root: Path):
        self.repository = MarketRepository(root)

    def _facts(self, kind, **kwargs) -> DataResult:
        kwargs["cutoff"] = kwargs.get("cutoff") or timestamp()
        try:
            records = self.repository.select(kind, **kwargs)
        except (CorruptData, OSError, ValueError, sqlite3.Error) as exc:
            return DataResult("CORRUPT", [], ({"kind": "CORRUPT", "reason": str(exc)},))
        data = [{**row["payload"], "_provenance": {
            key: row[key] for key in ("id", "provider", "fetched_at", "available_at", "raw_ref")
        }} for row in records]
        return DataResult("READY" if data else "MISSING", data,
                          () if data else ({"kind": "MISSING", "data_kind": kind},),
                          tuple(row["id"] for row in records))

    def calendar(self, *, knowledge_cutoff=None, start=None, end=None) -> DataResult:
        start = session_date(start) if start is not None else None
        end = session_date(end) if end is not None else None
        if start and end and start > end:
            raise ValueError("日历起始日晚于截止日")
        return self._facts("calendar", cutoff=knowledge_cutoff, start=start, end=end)

    def calendar_operation(self, operation: str, day: str, *, size=121, knowledge_cutoff=None):
        if operation not in {"window", "next_session", "previous_session", "is_month_end"}:
            raise ValueError("不支持的日历操作")
        day = session_date(day)
        if operation == "window" and (type(size) is not int or size < 1):
            raise ValueError("交易日窗口必须是正整数")
        try:
            # Indexed payloads are checksum-verified first. Raw evidence is checked only
            # for the dates actually used, including intervening non-trading days.
            records = self.repository.select("calendar", cutoff=knowledge_cutoff or timestamp(),
                                             verify=False)
            if not records:
                return DataResult("MISSING", [], ({"kind": "MISSING", "data_kind": "calendar"},))
            calendar = TradingCalendar([row["payload"] for row in records])
            result = (calendar.window(day, size) if operation == "window"
                      else getattr(calendar, operation)(day))
            issue_dates = [issue["date"] for issue in result.issues if "date" in issue]
            if operation == "window":
                first = min([day, *result.data, *issue_dates])
                if any(issue["kind"] == "CALENDAR_RANGE_EXHAUSTED" for issue in result.issues):
                    first = (date.fromisoformat(first) - timedelta(days=366)).isoformat()
                last = day
            elif operation == "is_month_end":
                first = last = day
                if calendar.days.get(day):
                    following = calendar.next_session(day)
                    last = (following.data if following.ready else next(
                        (issue["date"] for issue in following.issues if "date" in issue),
                        (date.fromisoformat(day) + timedelta(days=366)).isoformat()))
            else:
                direction = 1 if operation == "next_session" else -1
                initial = (date.fromisoformat(day) + timedelta(days=direction)).isoformat()
                boundary = result.data if result.ready else (issue_dates[0] if issue_dates else
                    (date.fromisoformat(day) + timedelta(days=366 * direction)).isoformat())
                first, last = sorted([initial, boundary])
            consumed = [row["id"] for row in records if first <= row["session"] <= last]
            self.repository.by_ids(consumed)
        except (CorruptData, OSError, ValueError, sqlite3.Error) as exc:
            return DataResult("CORRUPT", [], ({"kind": "CORRUPT", "reason": str(exc)},))
        return DataResult(result.state, result.data, result.issues, tuple(consumed))

    def prices(self, code: str, start: str, end: str, *, knowledge_cutoff=None, index=False):
        code, start, end = normalize_code(code), session_date(start), session_date(end)
        if start > end:
            raise ValueError("行情起始日晚于截止日")
        known = aware_time(knowledge_cutoff or timestamp()).astimezone(ZoneInfo("Asia/Shanghai"))
        if end > known.date().isoformat():
            return DataResult("WAITING_DATA", [], ({"kind": "FUTURE_PRICE", "date": end},))
        facts = self._facts("index" if index else "bar", code=code, start=start, end=end,
                            cutoff=knowledge_cutoff)
        if facts.state == "CORRUPT":
            return facts
        calendar_facts = self.calendar(knowledge_cutoff=knowledge_cutoff, start=start, end=end)
        if not calendar_facts.ready:
            return DataResult("WAITING_DATA", facts.data, calendar_facts.issues, facts.observation_ids)
        dates = TradingCalendar(calendar_facts.data).between(start, end)
        if not dates.ready:
            return DataResult("WAITING_DATA", facts.data, dates.issues, facts.observation_ids)
        found = {row["date"] for row in facts.data}
        issues = [{"kind": "MISSING_BAR", "code": code, "date": day}
                  for day in dates.data if day not in found]
        issues.extend({"kind": "DATA_CONFLICT", "code": code, "date": row["date"],
                       "details": row["conflicts"]}
                      for row in facts.data if row.get("conflicts"))
        status = "CONFLICT" if any(x["kind"] == "DATA_CONFLICT" for x in issues) else (
            "PARTIAL" if issues and facts.data else "MISSING" if issues else "READY")
        return DataResult(status, facts.data, tuple(issues),
                          tuple(dict.fromkeys(facts.observation_ids + calendar_facts.observation_ids)))

    def security(self, code: str, *, knowledge_cutoff=None):
        code = normalize_code(code)
        try:
            records = self.repository.select("security", code=code,
                cutoff=knowledge_cutoff or timestamp(), verify=False)
            if not records:
                return DataResult("MISSING", [], ({"kind": "MISSING", "data_kind": "security"},))
            newest = max(records, key=lambda row: (row["available_at"], row["session"], row["id"]))
            return self._selected_fact(newest)
        except (CorruptData, OSError, ValueError, sqlite3.Error) as exc:
            return DataResult("CORRUPT", [], ({"kind": "CORRUPT", "reason": str(exc)},))

    def _selected_fact(self, record):
        """Validate the chosen immutable observation; never fall back after corruption."""
        row = self.repository.by_ids([record["id"]])[0]
        payload = {**row["payload"], "_provenance": {key: row[key] for key in
                   ("id", "provider", "fetched_at", "available_at", "raw_ref")}}
        return DataResult("READY", payload, (), (row["id"],))

    def index_quote(self, code: str, session: str, *, knowledge_cutoff=None):
        """A daily benchmark quote needs both adjacent exchange sessions, never a zero guess."""
        code, session = normalize_code(code), session_date(session)
        current = self.prices(code, session, session, knowledge_cutoff=knowledge_cutoff, index=True)
        if not current.ready:
            return current
        if not current.data:
            return DataResult("WAITING_DATA", None,
                              ({"kind": "NON_TRADING_SESSION", "date": session},),
                              current.observation_ids)
        quote = {**current.data[-1], "previous_session": None, "previous_close": None,
                 "change": None, "change_percent": None,
                 "change_basis": "previous_exchange_session_close"}
        previous = self.calendar_operation("previous_session", session,
                                           knowledge_cutoff=knowledge_cutoff)
        ids = tuple(dict.fromkeys(current.observation_ids + previous.observation_ids))
        if not previous.ready:
            return DataResult("PARTIAL", quote, previous.issues, ids)
        quote["previous_session"] = previous.data
        prior = self.prices(code, previous.data, previous.data,
                            knowledge_cutoff=knowledge_cutoff, index=True)
        ids = tuple(dict.fromkeys(ids + prior.observation_ids))
        if not prior.ready or not prior.data:
            issues = prior.issues + ({"kind": "MISSING_PREVIOUS_INDEX_CLOSE", "code": code,
                                      "date": previous.data},)
            state = prior.state if prior.state in {"CORRUPT", "CONFLICT"} else "PARTIAL"
            return DataResult(state, quote, issues, ids)
        previous_close = Decimal(prior.data[-1]["close"])
        if not previous_close.is_finite() or previous_close <= 0:
            return DataResult("WAITING_DATA", quote,
                              ({"kind": "INVALID_PREVIOUS_INDEX_CLOSE", "code": code},), ids)
        change = Decimal(quote["close"]) - previous_close
        quote.update(previous_close=format(previous_close, "f"), change=format(change, "f"),
                     change_percent=format((change / previous_close * 100).quantize(
                         Decimal("0.0001"), rounding=ROUND_HALF_UP), "f"))
        return DataResult("READY", quote, (), ids)

    def events(self, code: str, *, knowledge_cutoff=None, start=None, end=None):
        # No end-date filtering here: already-known future/undated events must remain visible.
        from china_stock_sim.market.events import merge_events

        code = normalize_code(code)
        batches = self._facts("event_batch", code=code, cutoff=knowledge_cutoff)
        if batches.state == "CORRUPT":
            return batches
        if batches.ready:
            identities = list(dict.fromkeys(identity for batch in batches.data
                                            for identity in batch["members"]))
            try:
                records = self.repository.by_ids(identities)
            except (ValueError, OSError) as exc:
                return DataResult("CORRUPT", [], ({"kind": "CORRUPT", "reason": str(exc)},))
            cutoff = aware_time(knowledge_cutoff or timestamp())
            records = [row for row in records if aware_time(row["available_at"]) <= cutoff]
            facts = DataResult("READY", [row["payload"] for row in records], (),
                               tuple(row["id"] for row in records) + batches.observation_ids)
        else:
            facts = self._facts("event", code=code, cutoff=knowledge_cutoff)
        if facts.state == "MISSING":
            coverage = self._facts("coverage", code=normalize_code(code), cutoff=knowledge_cutoff)
            confirmed = [item for item in coverage.data
                         if item.get("purpose") == "events" and item.get("complete") is True]
            if confirmed:
                return DataResult("READY", [], (), coverage.observation_ids)
        if not facts.ready:
            return facts
        data = merge_events(facts.data)
        return DataResult("CONFLICT" if any(e.get("conflicts") for e in data) else "READY",
                          data, tuple({"kind": "EVENT_CONFLICT", "event_id": e["event_id"],
                                       "details": e["conflicts"]} for e in data if e.get("conflicts")),
                          facts.observation_ids)

    def event_coverage(self, code, start, end, *, knowledge_cutoff=None) -> DataResult:
        code, start, end = normalize_code(code), session_date(start), session_date(end)
        if start > end:
            raise ValueError("事件覆盖起始日晚于截止日")
        try:
            records = self.repository.select("coverage", code=code,
                cutoff=knowledge_cutoff or timestamp(), verify=False)
            relevant = [row for row in records if row["payload"].get("purpose") == "events"
                        and row["payload"].get("start", "9999") <= start
                        and row["payload"].get("end", "") >= end
                        and row["payload"].get("complete") is True]
            if relevant:
                latest = max(relevant, key=lambda row: (row["available_at"], row["session"], row["id"]))
                selected = self._selected_fact(latest)
                required_at = collection_floor(max(
                    latest["payload"]["end"],
                    latest["payload"].get("refreshed_as_of", latest["payload"]["end"])))
                issues = coverage_freshness_issues(
                    self.repository, latest["raw_ref"], required_at,
                    knowledge_cutoff=knowledge_cutoff or timestamp())
                if issues:
                    return DataResult("WAITING_DATA", selected.data, issues, selected.observation_ids)
                return selected
        except (CorruptData, OSError, ValueError, sqlite3.Error) as exc:
            return DataResult("CORRUPT", None, ({"kind": "CORRUPT", "reason": str(exc)},))
        return DataResult("WAITING_DATA", None, ({"kind": "EVENT_COVERAGE_MISSING", "code": code},))

    def event_settlement(self, code, session, *, knowledge_cutoff=None, event_ids=None,
                         operation="settlement"):
        """Check event facts for settlement; no holding eligibility or accounting is inferred.

        Daily reads include events with a milestone today and undated active events.
        The ledger passes IDs for older outstanding entitlements explicitly; unrelated
        historical incomplete events must not block every later trading day.
        With an action operation, READY means the required facts are complete, not
        that its operation_date has arrived. Explicit-zero actions are inapplicable.
        """
        from china_stock_sim.market.event_operations import check_operation, operation_readiness

        check_operation(operation)
        event_ids = _event_ids(event_ids)
        code, session = normalize_code(code), session_date(session)
        cutoff = knowledge_cutoff or timestamp()
        facts = self.events(code, knowledge_cutoff=cutoff)
        if facts.state == "CORRUPT":
            return facts
        # Conflicts outside this settlement scope remain visible in events(),
        # but only selected milestones or explicit outstanding IDs gate this read.
        wanted = set(event_ids) if event_ids is not None else None
        selected, matched = [], set()
        for event in facts.data:
            aliases = {event["event_id"], *(e["event_id"] for e in event.get("evidence", []))}
            if wanted is not None:
                if not aliases & wanted:
                    continue
                matched.update(aliases & wanted)
            else:
                if event.get("status") == "CANCELLED" or event.get("kind") == "NO_DISTRIBUTION":
                    continue
                milestones = [event.get(field) for field in (
                    "record_date", "ex_date", "payment_date", "share_listing_date")]
                if session not in milestones and event.get("ex_date") is not None:
                    continue
            checked = operation_readiness(event, cutoff, operation=operation)
            selected_event = {**event, "settlement_readiness": checked}
            if operation != "settlement":
                selected_event.update(operation=operation,
                                      operation_date=checked["operation_date"],
                                      operation_applicable=checked["applicable"])
            selected.append(selected_event)
        coverage = self.event_coverage(code, session, session, knowledge_cutoff=cutoff)
        if wanted is None and coverage.state == "CORRUPT":
            return DataResult("CORRUPT", selected, coverage.issues,
                              tuple(dict.fromkeys(facts.observation_ids
                                                  + coverage.observation_ids)))
        issues = []
        if wanted is None and not coverage.ready:
            issues.extend(coverage.issues)
        if wanted is not None:
            issues.extend({"kind": "EVENT_NOT_FOUND", "event_id": identity}
                          for identity in sorted(wanted - matched))
        issues.extend({"kind": issue, "event_id": event["event_id"]}
                      for event in selected for issue in event["settlement_readiness"]["issues"])
        state = "CONFLICT" if any(event.get("conflicts") for event in selected) else (
            "READY" if not issues else
            "PARTIAL" if any(e["settlement_readiness"]["state"] == "READY" for e in selected)
            else "WAITING_DATA")
        ids = tuple(dict.fromkeys(facts.observation_ids + coverage.observation_ids))
        return DataResult(state, selected, tuple(issues), ids)

    def pool(self, as_of: str, *, knowledge_cutoff=None):
        as_of = session_date(as_of)
        try:
            records = self.repository.select("pool", end=as_of,
                cutoff=knowledge_cutoff or timestamp(), latest=False, verify=False)
            if not records:
                return DataResult("MISSING", [], ({"kind": "MISSING", "data_kind": "pool"},))
            active = [row for row in records
                      if row["payload"].get("effective_from", row["session"]) <= as_of]
            if not active:
                return DataResult("WAITING_DATA", None, ({"kind": "POOL_NOT_EFFECTIVE"},))
            newest = max(active, key=lambda row: (row["payload"].get("effective_from", row["session"]),
                         row["session"], row["available_at"], row["id"]))
            return self._selected_fact(newest)
        except (CorruptData, OSError, ValueError, sqlite3.Error) as exc:
            return DataResult("CORRUPT", [], ({"kind": "CORRUPT", "reason": str(exc)},))

    def signal_prices(self, code: str, as_of: str, *, size: int = 121,
                      knowledge_cutoff=None) -> DataResult:
        """Read an explicit signal window without collecting or changing stored facts.

        Matches MarketDataReader.signal_prices; readiness keeps its fixed windows.
        Adjustment uses the same knowledge cutoff for prices, calendar and events.
        Coverage failures remain non-READY; event_coverage exposes their full details.
        """
        from china_stock_sim.market.signals import adjusted_series

        cutoff = knowledge_cutoff or timestamp()
        window = self.calendar_operation("window", as_of, size=size, knowledge_cutoff=cutoff)
        if not window.ready:
            return window
        bars = self.prices(code, window.data[0], window.data[-1], knowledge_cutoff=cutoff)
        if not bars.ready:
            return bars
        events = self.events(code, knowledge_cutoff=cutoff)
        if events.state == "CORRUPT":
            return events
        coverage = self.event_coverage(code, window.data[0], window.data[-1], knowledge_cutoff=cutoff)
        result = adjusted_series(bars.data, events.data, cutoff, events_complete=coverage.ready)
        ids = tuple(dict.fromkeys(bars.observation_ids + events.observation_ids
                                  + coverage.observation_ids))
        issues = tuple({"kind": issue} if isinstance(issue, str) else issue
                       for issue in result["issues"])
        return DataResult(result["state"], result["rows"], issues, ids)

    def execution_facts(self, code, session, *, knowledge_cutoff=None, rule_context=None):
        from china_stock_sim.market.signals import execution_facts

        cutoff = knowledge_cutoff or timestamp()
        bars = self.prices(code, session, session, knowledge_cutoff=cutoff)
        if not bars.ready:
            return bars
        if not bars.data:
            return DataResult("WAITING_DATA", None, ({"kind": "NON_TRADING_SESSION", "date": session},))
        security = self.security(code, knowledge_cutoff=cutoff)
        if not security.ready:
            return security
        events = self.events(code, knowledge_cutoff=cutoff)
        if events.state == "CORRUPT":
            return events
        # The execution facts builder checks conflicts for this session; a known
        # historical discrepancy must not indefinitely block unrelated sessions.
        coverage = self.event_coverage(code, session, session, knowledge_cutoff=cutoff)
        target = dict(bars.data[-1])
        previous = self.calendar_operation("previous_session", session, knowledge_cutoff=cutoff)
        ids = tuple(dict.fromkeys(bars.observation_ids + security.observation_ids
                                  + events.observation_ids + coverage.observation_ids
                                  + previous.observation_ids))
        if previous.state in {"CORRUPT", "CONFLICT"}:
            return DataResult(previous.state, None, previous.issues, ids)
        previous_prices = (self.prices(code, previous.data, previous.data, knowledge_cutoff=cutoff)
                           if previous.ready else DataResult("MISSING"))
        ids = tuple(dict.fromkeys(ids + previous_prices.observation_ids))
        if previous_prices.state in {"CORRUPT", "CONFLICT"}:
            return DataResult(previous_prices.state, None, previous_prices.issues, ids)
        if previous_prices.ready and previous_prices.data:
            target["previous_close"] = previous_prices.data[-1]["close"]
        result = execution_facts(target, security.data, events.data, cutoff,
                                 events_complete=coverage.ready, rule_context=rule_context)
        return DataResult(result["state"], result, tuple(result.get("issues", [])), ids)

    def readiness(self, code, as_of, purpose, *, knowledge_cutoff=None, rule_context=None,
                  event_ids=None, operation="settlement"):
        from china_stock_sim.market.event_operations import check_operation

        check_operation(operation)
        if purpose != "event_settlement" and (operation != "settlement" or event_ids is not None):
            raise ValueError("权益动作和事件选择仅适用于event_settlement用途")
        if purpose == "valuation":
            facts = self.prices(code, as_of, as_of, knowledge_cutoff=knowledge_cutoff)
            if facts.ready and not facts.data:
                return DataResult("WAITING_DATA", [],
                                  ({"kind": "NON_TRADING_SESSION", "date": session_date(as_of)},),
                                  facts.observation_ids)
            return facts
        if purpose == "index":
            return self.index_quote(code, as_of, knowledge_cutoff=knowledge_cutoff)
        if purpose in {"llm", "rules"}:
            return self.signal_prices(code, as_of, size=21 if purpose == "llm" else 121,
                                      knowledge_cutoff=knowledge_cutoff)
        if purpose == "execution":
            return self.execution_facts(code, as_of, knowledge_cutoff=knowledge_cutoff,
                                        rule_context=rule_context)
        if purpose == "events":
            return self.events(code, knowledge_cutoff=knowledge_cutoff)
        if purpose == "event_settlement":
            return self.event_settlement(code, as_of, knowledge_cutoff=knowledge_cutoff,
                                         event_ids=event_ids, operation=operation)
        raise ValueError("未知数据用途")

    def freeze(self, codes, as_of, *, knowledge_cutoff=None, purposes=("valuation",),
               rule_context=None, event_ids=None, operation="settlement") -> MarketSnapshot:
        from china_stock_sim.market.event_operations import check_operation

        check_operation(operation)
        codes = tuple(dict.fromkeys(normalize_code(code) for code in codes))
        purposes = tuple(purposes)
        event_ids = _event_ids(event_ids)
        if event_ids is not None and len(codes) != 1:
            raise ValueError("按事件ID冻结必须指定一个证券代码")
        if "event_settlement" not in purposes and (
                operation != "settlement" or event_ids is not None):
            raise ValueError("权益动作和事件选择仅适用于event_settlement用途")
        cutoff = aware_time(knowledge_cutoff or timestamp())
        if cutoff > aware_time(timestamp()):
            raise ValueError("不能封存未来可用时点的快照")
        results, ids = {}, []
        for code in codes:
            code = normalize_code(code)
            results[code] = {}
            for purpose in purposes:
                result = self.readiness(code, as_of, purpose, knowledge_cutoff=cutoff,
                                        rule_context=rule_context,
                                        operation=operation if purpose == "event_settlement"
                                        else "settlement",
                                        event_ids=event_ids if purpose == "event_settlement"
                                        else None)
                results[code][purpose] = result.as_dict()
                ids.extend(result.observation_ids)
        pool = self.pool(as_of, knowledge_cutoff=cutoff)
        ids.extend(pool.observation_ids)
        content = {"as_of": session_date(as_of), "knowledge_cutoff": cutoff.isoformat(),
                   "purposes": list(purposes), "pool": pool.as_dict(), "results": results}
        if operation != "settlement" or event_ids is not None:
            content["event_query"] = {"operation": operation,
                                      "event_ids": list(event_ids) if event_ids is not None else None}
        with self.repository.writer():
            return self.repository.freeze(content, ids)

    def read_snapshot(self, snapshot_id) -> MarketSnapshot:
        return self.repository.read_snapshot(snapshot_id)

    def evidence_manifest(self, snapshot_ids, *, observation_ids=()):
        """Verify and enumerate immutable files; does not collect, write or export private DB APIs."""
        from china_stock_sim.market.evidence import evidence_manifest

        return evidence_manifest(self.repository, snapshot_ids, observation_ids=observation_ids)

    def statement_input_fingerprint(self, method, kwargs):
        """Read-only invalidation identity; never collects or accepts facts by itself."""
        from china_stock_sim.market.statement_fingerprint import fingerprint

        return fingerprint(self.repository, method, kwargs)

    def revisions(self, kind, code, start=None, end=None):
        facts = self._facts(kind, code=normalize_code(code), start=start, end=end, latest=False)
        if not facts.ready:
            return facts
        previous, data = {}, []
        for row in sorted(facts.data, key=lambda r: (r["_provenance"]["available_at"],
                                                    r["_provenance"]["id"])):
            key = row.get("event_id") or row.get("date", row.get("ex_date", ""))
            before = previous.get(key)
            fields = (sorted((row.keys() | before.keys()) - {"_provenance"}) if before else [])
            changes = {field: {"before": before.get(field), "after": row.get(field)}
                       for field in fields if before.get(field) != row.get(field)}
            data.append({**row, "_revision": {
                "previous_observation_id": before["_provenance"]["id"] if before else None,
                "changed_fields": changes}})
            previous[key] = row
        return DataResult(facts.state, data, facts.issues, facts.observation_ids)

    def update(self, request, **kwargs):
        """Explicit write/network entry, deliberately separate from all read methods."""
        from china_stock_sim.market.update import MarketUpdater

        return MarketUpdater(self.repository.root, **kwargs).run(request)


def decimal_value(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _event_ids(values):
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise ValueError("event_ids必须为事件ID集合，不能是单个字符串")
    result = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise ValueError("事件ID必须为非空字符串")
    return tuple(sorted(set(result)))
