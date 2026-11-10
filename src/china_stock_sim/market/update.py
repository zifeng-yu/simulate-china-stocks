"""Explicit bounded collection, incremental updates, source fallbacks and pool maintenance."""

import os
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import DataRequest, aware_time
from china_stock_sim.market.freshness import collection_floor, coverage_freshness_issues
from china_stock_sim.market.preparation import validate_as_of
from china_stock_sim.market.providers import BaoStockProvider, ProviderError
from china_stock_sim.market.quality import normalize_bars, normalize_code, normalize_date
from china_stock_sim.market.repository import KINDS, CorruptData, MarketRepository, timestamp
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.settings import load_market_settings
from china_stock_sim.market.storage import digest, read_json

_SOURCE_FAILURE_THRESHOLD = 3
_SOURCE_COOLDOWN_SECONDS = 60


def _retry_failures(previous: dict, repository: MarketRepository, provider) -> int:
    """Keep legacy failure backoff conservative without counting known successes."""
    if "retry_failures" in previous:
        return previous["retry_failures"]
    if previous.get("state") == "OK":
        return 0
    # Old pauses copied the last OK row without making a provider attempt. A real
    # failed attempt replaced that row and never retained its success reference.
    if (previous.get("state") == "PAUSED"
            and previous.get("pause_reason") in {
                "SOURCE_COOLDOWN", "SOURCE_UNAVAILABLE", "BUDGET_PAUSED"}
            and previous.get("raw_reference") and previous.get("finished_at")
            and not any(field in previous for field in (
                "error", "category", "rejected_raw_reference", "server_retry_at"))):
        try:
            raw = repository.read_validated_raw(previous["raw_reference"])
            if (raw["provider"] == getattr(provider, "provider_id", None)
                    and raw["adapter_version"] == previous["identity"]["adapter"]
                    and aware_time(raw["fetched_at"]) <= aware_time(previous["finished_at"])
                    <= aware_time(previous["started_at"])):
                return 0
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # Legacy failed/interrupted records cannot distinguish successes from failures.
    # Preserve their former retry tier until a validated success resets the streak.
    return previous.get("attempts", 0)


def normalize_prices(response: dict, code: str, start: str, end: str) -> list[dict]:
    """Price-only adapters explicitly preserve unknown trading/quantity fields."""
    result, seen = [], set()
    for raw in response["rows"]:
        session = normalize_date(raw["date"])
        if normalize_code(raw["code"]) != code or not start <= session <= end or session in seen:
            raise ValueError("价格响应代码、日期或唯一键不符")
        seen.add(session)
        row = {"code": code, "date": session, "adjustflag": 3}
        for field in ("open", "high", "low", "close"):
            number = Decimal(str(raw[field]))
            if not number.is_finite() or number <= 0:
                raise ValueError("价格必须是正有限数值")
            row[field] = format(number, "f")
        if not Decimal(row["low"]) <= min(Decimal(row["open"]), Decimal(row["close"])) <= max(
                Decimal(row["open"]), Decimal(row["close"])) <= Decimal(row["high"]):
            raise ValueError("价格范围矛盾")
        for field in ("preclose", "amount", "volume", "tradestatus", "isST"):
            row[field] = raw.get(field)
        row["quantity_precision"] = response.get("quantity_precision", {})
        row["capabilities"] = response.get("capabilities", ["unadjusted_prices"])
        result.append(row)
    if not result:
        raise ValueError("UNPUBLISHED: 源暂未返回请求日期的价格")
    return sorted(result, key=lambda r: r["date"])


def provider_registry(timeout: int) -> dict:
    from china_stock_sim.market.providers.eastmoney import EastmoneyProvider
    from china_stock_sim.market.providers.tencent import TencentProvider

    return {"baostock": BaoStockProvider(timeout=timeout),
            "tencent": TencentProvider(timeout=timeout),
            "eastmoney": EastmoneyProvider(timeout=timeout)}


def _event_semantics(payload):
    result = dict(payload)
    result.pop("available_at", None)
    result.pop("normalization", None)
    return result


def _put_normalized_events(repository, records, response, normalized_at):
    """Reuse a source's latest interpretation; never backdate a changed one."""
    reference = repository.save_raw(response)
    # Restrict verification to this immutable source. Unrelated historical raw
    # damage must not make a valid new source impossible to ingest.
    with repository.connection() as connection:
        identities = [] if connection is None else [row[0] for row in connection.execute(
            "SELECT id FROM observations WHERE kind IN ('event','event_batch') AND raw_ref=? "
            "ORDER BY available_at DESC,rowid DESC", (reference,))]
    observations = repository.by_ids(identities) if identities else []
    previous = {}
    for item in observations:
        if item["kind"] == "event":
            previous.setdefault(item["payload"]["event_id"], item)
    now = aware_time(normalized_at)
    pending, members = [], []
    for record in records:
        prior = previous.get(record["event_id"])
        if prior is not None and _event_semantics(prior["payload"]) == _event_semantics(record):
            members.append(prior["id"])
            continue
        if observations:
            if now <= max(aware_time(item["available_at"]) for item in observations):
                raise CorruptData("权益解释变更的规范化时间必须晚于已有同源记录")
            record = {**record, "available_at": now.isoformat()}
        pending.append(record)
        members.append(None)
    inserted = iter(repository.put("event", pending, response) if pending else [])
    return [next(inserted) if identity is None else identity for identity in members], observations


def _put_normalized_event_batch(repository, payload, response, observations, normalized_at):
    """Keep repaired batches intact and publish new members no earlier than known."""
    previous = [item for item in observations if item["kind"] == "event_batch"
                and item["payload"].get("code") == payload["code"]
                and item["payload"].get("batch_key") == payload["batch_key"]]
    if previous and _event_semantics(previous[0]["payload"]) == _event_semantics(payload):
        return previous[0]["id"]
    available = aware_time(response["fetched_at"])
    if previous:
        now = aware_time(normalized_at)
        prior_time = aware_time(previous[0]["available_at"])
        if (now < prior_time or (now == prior_time
                and payload["members"] != previous[0]["payload"]["members"])):
            raise CorruptData("权益批次变更的规范化时间必须晚于已有同源批次")
        available = max(available, now)
    members = repository.by_ids(payload["members"])
    available = max([available, *(aware_time(item["available_at"]) for item in members)])
    if available > aware_time(response["fetched_at"]):
        payload = {**payload, "available_at": available.isoformat()}
    return repository.put("event_batch", [payload], response,
                          key_fields=["code", "batch_key"])[0]


def _put_event_coverage(repository, payload, source_responses, *, assembled_at, repair=False):
    """Reuse only the latest exact proof; source acquisitions remain immutable facts."""
    now = aware_time(assembled_at)
    key_fields = ["code", "purpose", "start", "end"]
    key = digest({field: payload.get(field, "") for field in key_fields})
    with repository.connection() as connection:
        latest = None if connection is None else connection.execute(
            "SELECT * FROM observations WHERE kind='coverage' AND record_key=? "
            "ORDER BY available_at DESC,rowid DESC LIMIT 1", (key,)).fetchone()
    prior = None
    if latest is not None:
        # Verify the latest proof and its entire source closure, never fall back
        # to an older equal version when a newer proof is different or damaged.
        prior = repository._decode(latest, verify=False)
        if aware_time(prior["available_at"]) > now:
            raise CorruptData("权益覆盖组装时间不能早于已有同范围证明")
        try:
            prior = repository.by_ids([prior["id"]])[0]
        except CorruptData:
            if not repair:
                raise
            # Explicit repair has reacquired both sources this run. It can
            # append a valid replacement proof, never reuse the damaged one.
            prior = None
    references = [repository.save_raw(response) for response in source_responses]
    required_at = collection_floor(max(payload["end"], payload["refreshed_as_of"]))
    for reference in references:
        repository.validate_raw(reference)
        if coverage_freshness_issues(repository, reference, required_at, knowledge_cutoff=now):
            raise CorruptData("权益覆盖来源未满足当前新鲜度或知识时点")
    contract = {"provider": "distribution_sources", "adapter_version": "1",
                "dependencies": references}
    if prior is not None:
        raw = repository.read_validated_raw(prior["raw_ref"])
        if (prior["payload"] == payload
                and {name: value for name, value in raw.items() if name != "fetched_at"} == contract):
            return prior["id"]
    # The first proof of these exact inputs becomes known when assembled. Do
    # not backdate it to its source leaves, or refresh an identical old proof.
    response = {**contract, "fetched_at": now.isoformat()}
    return repository.put("coverage", [payload], response, key_fields=key_fields)[0]


class MarketUpdater:
    def __init__(self, root: Path, *, providers=None, download=True, refresh=False, repair=False,
                 max_requests=None, budget_seconds=None, clock=None):
        settings = load_market_settings(root)
        self.root, self.config = settings.root, settings.config
        self.repository = MarketRepository(self.root)
        self.service = MarketDataService(self.root)
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self.providers = providers if providers is not None else provider_registry(
            self.config["request_timeout_seconds"])
        self.benchmarks = settings.benchmarks
        self.download, self.refresh, self.repair = download, refresh, repair
        if repair and not download:
            raise ValueError("缓存修复需要显式允许下载")
        self.maximum = max_requests if max_requests is not None else self.config["max_requests_per_run"]
        self.budget = budget_seconds if budget_seconds is not None else self.config["max_run_seconds"]
        if type(self.maximum) is not int or not 1 <= self.maximum <= 1000:
            raise ValueError("请求预算应为 1 到 1000")
        if type(self.budget) is not int or not 5 <= self.budget <= 7200:
            raise ValueError("时间预算应为 5 到 7200 秒")

    def _state(self):
        self.job.update(requests_this_run=self.calls, updated_at=self.clock().isoformat())
        self.job["source_cooldowns"] = {
            source: {"consecutive_network_failures": self.source_failures.get(source, 0),
                     "next_retry_at": retry_at.isoformat()}
            for source, retry_at in self.source_retry_at.items()
        }
        self.repository.save_state("jobs", self.job_id, self.job)

    def _resolve_blocked_requests(self, key, identity):
        """Valid new/cached evidence resolves blocked keys from older adapters."""
        for old_key, old in self.repository.states("requests").items():
            old_identity = old.get("identity", {})
            if (old_key != key and old.get("state") in {"FAILED", "RUNNING", "PAUSED"}
                    and all(old_identity.get(field) == identity[field]
                            for field in ("source", "query", "params"))):
                old.update(state="SUPERSEDED", superseded_by=key,
                           resolved_at=self.clock().isoformat())
                self.repository.save_state("requests", old_key, old)

    def _request(self, source, query, params, consume, *, reuse_today=False, min_fetched_at=None):
        identity = {"source": source, "adapter": getattr(self.providers.get(source), "adapter_version", "1"),
                    "query": query, "params": params}
        key = digest(identity)
        previous = self.repository.states("requests").get(key, {})
        retry_failures = _retry_failures(previous, self.repository, self.providers.get(source))
        if key in self.seen:
            return self.seen[key]
        if previous.get("server_retry_at") and aware_time(previous["server_retry_at"]) > self.clock():
            self.job["issues"].append({"kind": "COOLDOWN", "origin": "server", **identity,
                                       "next_retry_at": previous["server_retry_at"]})
            return None
        if (reuse_today and not self.refresh and previous.get("state") == "OK"
                and previous.get("raw_reference")):
            try:
                reference = previous["raw_reference"]
                cached = self.repository.read_validated_raw(reference)
                fetched_day = aware_time(cached["fetched_at"]).astimezone(
                    ZoneInfo("Asia/Shanghai")).date()
                if (fetched_day == self.clock().astimezone(ZoneInfo("Asia/Shanghai")).date()
                        and (min_fetched_at is None or min_fetched_at <= aware_time(
                            cached["fetched_at"]) <= self.clock())):
                    self.seen[key] = consume(cached)
                    self._resolve_blocked_requests(key, identity)
                    return self.seen[key]
            except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
                # A corrupt or incompatible cached response is never accepted as successful.
                # Explicit repair quarantines it; the normal bounded request can obtain new facts.
                pass
        if not self.download:
            self.job["issues"].append({"kind": "WAITING_DOWNLOAD", **identity})
            return None
        if source not in self.providers:
            self.job["issues"].append({"kind": "SOURCE_UNAVAILABLE", "source": source})
            self.repository.save_state("requests", key, {**previous, "identity": identity,
                "state": "PAUSED", "pause_reason": "SOURCE_UNAVAILABLE",
                "started_at": self.clock().isoformat(), "attempts": previous.get("attempts", 0),
                "retry_failures": retry_failures})
            return None
        source_retry_at = self.source_retry_at.get(source)
        if source_retry_at is not None and source_retry_at > self.clock():
            retry_at = max(source_retry_at, aware_time(previous["next_retry_at"])) if (
                previous.get("next_retry_at")) else source_retry_at
            self.job["issues"].append({"kind": "SOURCE_COOLDOWN", **identity,
                                       "next_retry_at": retry_at.isoformat()})
            self.repository.save_state("requests", key, {**previous, "identity": identity,
                "state": "PAUSED", "pause_reason": "SOURCE_COOLDOWN", "retryable": True,
                "next_retry_at": retry_at.isoformat(), "started_at": self.clock().isoformat(),
                "attempts": previous.get("attempts", 0), "retry_failures": retry_failures})
            return None
        if self.calls >= self.maximum or time.monotonic() - self.started >= self.budget:
            self.job["issues"].append({"kind": "BUDGET_PAUSED", **identity})
            # Pausing is persistent unfinished work, even if old facts exist.
            # It consumes no provider attempt and retains successful evidence.
            self.repository.save_state("requests", key, {**previous, "identity": identity,
                "state": "PAUSED", "pause_reason": "BUDGET_PAUSED",
                "started_at": self.clock().isoformat(), "attempts": previous.get("attempts", 0),
                "retry_failures": retry_failures})
            return None
        if (not self.refresh and not self.repair and previous.get("next_retry_at")
                and aware_time(previous["next_retry_at"]) > self.clock()):
            self.job["issues"].append({"kind": "COOLDOWN", "next_retry_at": previous["next_retry_at"],
                                       **identity})
            return None
        self.calls += 1
        state = {"identity": identity, "attempts": previous.get("attempts", 0) + 1,
                 "retry_failures": retry_failures,
                 "state": "RUNNING", "started_at": self.clock().isoformat()}
        response = None
        try:
            self.repository.save_state("requests", key, state)
            self._state()
            provider = self.providers[source]
            if hasattr(provider, "timeout"):
                provider.timeout = max(0.1, min(self.config["request_timeout_seconds"],
                                               self.budget - (time.monotonic() - self.started)))
            response = provider.fetch(query, params)
            reference = self.repository.save_raw(response)
            if min_fetched_at is not None and not (
                    min_fetched_at <= aware_time(response["fetched_at"]) <= self.clock()):
                raise ValueError("UNPUBLISHED: 权益来源取得时间未满足本轮收盘后新鲜度")
            result = consume(response)
            state.update(state="OK", retry_failures=0,
                         finished_at=self.clock().isoformat(), raw_reference=reference)
            self.seen[key] = result
            self.source_failures[source] = 0
            self.source_retry_at.pop(source, None)
            # A restored/upgraded adapter may have a different request key. Its
            # successful response resolves older blocked attempts for this same
            # source/query/params, without deleting the prior error or evidence.
            self._resolve_blocked_requests(key, identity)
        except KeyboardInterrupt:
            state.update(state="PAUSED", pause_reason="INTERRUPTED",
                         interrupted_at=self.clock().isoformat())
            self.repository.save_state("requests", key, state)
            self._state()
            raise
        except (ProviderError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            category = getattr(exc, "category", None) or ("NETWORK" if isinstance(exc, ProviderError) and exc.retryable else
                        "CONFIGURATION" if isinstance(exc, ProviderError) else
                        "UNPUBLISHED" if str(exc).startswith("UNPUBLISHED") else "FORMAT")
            if category == "NETWORK":
                self.source_failures[source] = self.source_failures.get(source, 0) + 1
                if self.source_failures[source] >= _SOURCE_FAILURE_THRESHOLD:
                    self.source_retry_at[source] = self.clock() + timedelta(
                        seconds=_SOURCE_COOLDOWN_SECONDS)
            else:
                self.source_failures[source] = 0
                self.source_retry_at.pop(source, None)
            retryable = category in {"NETWORK", "UNPUBLISHED"}
            state["retry_failures"] = retry_failures + 1 if retryable else 0
            delay = (self.config["retry_delays_seconds"][min(state["retry_failures"] - 1,
                       len(self.config["retry_delays_seconds"]) - 1)] if retryable else 3600)
            delay = max(delay, getattr(exc, "retry_after_seconds", 0) or 0)
            state.update(state="FAILED", error=str(exc), category=category, retryable=retryable,
                         next_retry_at=(self.clock() + timedelta(seconds=delay)).isoformat())
            if getattr(exc, "retry_after_seconds", None):
                state["server_retry_at"] = (self.clock() + timedelta(
                    seconds=exc.retry_after_seconds)).isoformat()
            if response:
                state["rejected_raw_reference"] = self.repository.save_raw(response)
            self.job["issues"].append({"kind": category, "reason": str(exc), **identity,
                "next_retry_at": state["next_retry_at"],
                **({"server_retry_at": state["server_retry_at"]}
                   if state.get("server_retry_at") else {})})
            self.seen[key] = None
        self.repository.save_state("requests", key, state)
        self._state()
        return self.seen[key]

    def _repair_objects(self):
        from china_stock_sim.config import local_path

        repairs, checked = [], set()

        def inspect(reference):
            if reference in checked:
                return
            checked.add(reference)
            path = local_path(self.root, reference)
            try:
                content = read_json(path)
                if digest(content) != path.stem:
                    raise CorruptData("原件摘要不符")
            except (OSError, ValueError):
                repairs.append(self.repository.quarantine(reference, reason="explicit repair"))
                return
            for dependency in content.get("dependencies", []):
                inspect(dependency)

        for kind in sorted(KINDS):
            for row in self.repository.select(kind, verify=False, latest=False):
                inspect(row["raw_ref"])
        self.job["repairs"] = repairs

    def _calendar(self, request):
        start = (date.fromisoformat(request.as_of) - timedelta(days=max(550, request.history_sessions * 2))).isoformat()
        end = (date.fromisoformat(request.as_of) + timedelta(days=40)).isoformat()
        existing = self.service.calendar(knowledge_cutoff=self.clock(), start=start, end=end)
        rows = existing.data if existing.ready else []
        coverage = TradingCalendar(rows).between(start, end)
        if coverage.ready and not self.refresh:
            return TradingCalendar(rows)
        # Request one bounded calendar span; daily security data remain independently incremental.
        def consume(response):
            raw = response["rows"]
            normalized = [{"date": normalize_date(row["calendar_date"]),
                           "is_trading_day": row["is_trading_day"]} for row in raw]
            calendar = TradingCalendar(normalized)
            if not normalized or any(not start <= row["date"] <= end for row in normalized):
                raise ValueError("交易日历返回空值或越界日期")
            # A source can publish only part of the requested future range; preserve what is known.
            self.repository.put("calendar", normalized, response, key_fields=["date"])
            return calendar
        self._request("baostock", "calendar", {"start_date": start, "end_date": end}, consume)
        facts = self.service.calendar(knowledge_cutoff=self.clock(), start=start, end=end)
        return TradingCalendar(facts.data if facts.ready else [])

    def _securities(self, as_of):
        from china_stock_sim.market.universe import normalize_securities

        existing = self.repository.select("security", cutoff=self.clock(), verify=False)
        today = self.clock().date().isoformat()
        if existing and max(row["fetched_at"][:10] for row in existing) >= today and not self.refresh:
            try:
                for reference in {row["raw_ref"] for row in existing}:
                    self.repository.validate_raw(reference)
                return
            except CorruptData:
                pass
        def consume(response):
            if not response["rows"]:
                raise ValueError("UNPUBLISHED: 证券信息为空")
            records = normalize_securities(response["rows"], today)
            for record in records:
                record["date"] = today
            self.repository.put("security", records, response)
            return records
        self._request("baostock", "securities", {}, consume)

    def _prices(self, code, sessions, *, index=False):
        kind = "index" if index else "bar"
        existing = self.repository.select(kind, code=code, start=sessions[0], end=sessions[-1],
                                          cutoff=self.clock(), verify=False)
        price_dates, complete_dates = set(), set()
        for row in existing:
            try:
                self.repository.validate_raw(row["raw_ref"])
                price_dates.add(row["session"])
                if index or all(row["payload"].get(field) is not None
                                for field in ("preclose", "volume", "amount", "tradestatus", "isST")):
                    complete_dates.add(row["session"])
            except CorruptData:
                pass
        missing = sessions if self.refresh else [day for day in sessions if day not in complete_dates]
        if not missing:
            return
        # Source limits are calendar-day limits, so split without assuming 252 sessions/year.
        chunks = []
        for day in missing:
            if not chunks or (date.fromisoformat(day) - date.fromisoformat(chunks[-1][0])).days > 500:
                chunks.append([])
            chunks[-1].append(day)
        for chunk in chunks:
            start, end = chunk[0], chunk[-1]
            params = {"code": code, "start_date": start, "end_date": end}
            def consume(response, full=False, first=start, last=end):
                if not response["rows"]:
                    raise ValueError("UNPUBLISHED: 请求区间日线尚未发布")
                records = (normalize_bars(response["rows"], expected_code=code,
                                          allowed_dates=[d for d in sessions if first <= d <= last])
                           if full else normalize_prices(response, code, first, last))
                self.repository.put(kind, records, response)
                return records
            result = None
            if not index:
                result = self._request("baostock", "history", params, lambda r: consume(r, True))
            if result is not None:
                price_dates.update(row["date"] for row in result)
                complete_dates.update(row["date"] for row in result)
            needed = [day for day in chunk if day not in price_dates]
            if index and self.refresh:
                needed = chunk
            if needed:
                fallback = {"code": code, "start_date": needed[0], "end_date": needed[-1]}
                def fallback_consume(response, first=needed[0], last=needed[-1]):
                    records = normalize_prices(response, code, first, last)
                    # A gap-filling response may also contain already accepted dates.
                    # Keep both sources and surface a price contradiction instead of silently ignoring it.
                    accepted = {r["session"]: r for r in self.repository.select(
                        kind, code=code, start=first, end=last, cutoff=self.clock())}
                    conflicts = []
                    for row in records:
                        old = accepted.get(row["date"])
                        if old and row["date"] in complete_dates and not index:
                            differences = {field: {"primary": old["payload"][field],
                                                   "backup": row[field]}
                                           for field in ("open", "high", "low", "close")
                                           if Decimal(old["payload"][field]) != Decimal(row[field])}
                            if differences:
                                combined = {"provider": "price_conflict", "adapter_version": "1",
                                    "fetched_at": response["fetched_at"],
                                    "dependencies": [old["raw_ref"], self.repository.save_raw(response)]}
                                self.repository.put(kind, [{**old["payload"], "conflicts": [{
                                    "kind": "CROSS_SOURCE_PRICE_CONFLICT", "fields": differences,
                                    "primary": old["provider"], "backup": response["provider"]}]}], combined)
                                conflicts.append(row["date"])
                    if conflicts:
                        self.job["issues"].append({"kind": "DATA_CONFLICT", "code": code,
                                                   "dates": conflicts})
                    # Never downgrade an existing complete BaoStock observation just to fill a gap.
                    records = [row for row in records
                               if row["date"] not in complete_dates or (index and self.refresh)]
                    self.repository.put(kind, records, response)
                    return records
                self._request("tencent", "prices", fallback, fallback_consume)

    def _events(self, code, sessions):
        from china_stock_sim.market.events import (
            normalize_baostock_events,
            normalize_eastmoney_events,
        )

        start, end = sessions[0], sessions[-1]
        # Even an old pending entitlement is refreshed as of the current request,
        # not merely as of the end of its historical event range.
        freshness_floor = collection_floor(self.job["as_of"])
        coverage = self.service.event_coverage(code, start, end, knowledge_cutoff=self.clock())
        if coverage.ready and not self.download and not self.refresh:
            stale = coverage_freshness_issues(
                self.repository, coverage.data["_provenance"]["raw_ref"], freshness_floor,
                knowledge_cutoff=self.clock())
            if stale:
                self.job["issues"].extend({"code": code, **issue} for issue in stale)
                return False
            # Cache-only evaluation can reuse validated historical coverage, but
            # must not hide a later failed/in-progress refresh behind that old proof.
            coverage_time = aware_time(coverage.data["_provenance"]["fetched_at"])
            for previous in self.repository.states("requests").values():
                identity = previous.get("identity", {})
                params = identity.get("params", {})
                relevant = params.get("code") == code and (
                    identity.get("query") == "events" or (
                        identity.get("query") == "dividends"
                        and int(start[:4]) <= params.get("year", -1) <= int(end[:4])))
                if (relevant and previous.get("state") in {"FAILED", "RUNNING", "PAUSED"}
                        and aware_time(previous["started_at"]) >= coverage_time):
                    self.job["issues"].append({"kind": "EVENT_REFRESH_INCOMPLETE", "code": code,
                        "start": start, "end": end, "request": identity,
                        "next_retry_at": previous.get("next_retry_at")})
                    return False
            return True
        all_ok, evidence = True, []
        for year in range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1):
            def consume(response, event_year=year):
                required = {"code", "dividOperateDate", "dividCashPsBeforeTax"}
                if not required.issubset(response["fields"]):
                    raise ValueError("分红源字段缺失")
                records = normalize_baostock_events(response["rows"], code, response["fetched_at"],
                                                    raw_reference=self.repository.save_raw(response))
                normalized_at = self.clock()
                members, observations = _put_normalized_events(
                    self.repository, records, response, normalized_at)
                _put_normalized_event_batch(self.repository, {"code": code, "date": end,
                    "batch_key": f"baostock:{event_year}", "members": members,
                    "scope": "reported_distributions", "year": event_year}, response,
                    observations, normalized_at)
                return response
            response = self._request("baostock", "dividends", {"code": code, "year": year},
                                     consume, reuse_today=not self.repair,
                                     min_fetched_at=freshness_floor)
            if response is None:
                all_ok = False
            else:
                evidence.append(response)
        def future(response):
            records = normalize_eastmoney_events(response["rows"], code, response["fetched_at"],
                                                 raw_reference=self.repository.save_raw(response))
            normalized_at = self.clock()
            members, observations = _put_normalized_events(
                self.repository, records, response, normalized_at)
            _put_normalized_event_batch(self.repository, {"code": code, "date": end,
                "batch_key": "eastmoney:all", "members": members,
                "scope": "reported_distributions"}, response, observations, normalized_at)
            return response
        announced = self._request("eastmoney", "events", {"code": code}, future,
                                  reuse_today=not self.repair,
                                  min_fetched_at=freshness_floor)
        # Complete refers to this stated source/category scope, never all possible corporate events.
        if all_ok and announced is not None:
            _put_event_coverage(self.repository, {"code": code, "date": end, "purpose": "events",
                "start": start, "end": end, "complete": True,
                "refreshed_as_of": self.job["as_of"],
                "scope": "published_cash_bonus_capital_distributions",
                "complex_action_absence_proved": False}, [*evidence, announced],
                assembled_at=self.clock(), repair=self.repair)
            return True
        return False

    def run(self, request: DataRequest):
        validate_as_of(request.as_of, self.clock())
        self.calls, self.seen, self.started = 0, {}, time.monotonic()
        self.source_failures, self.source_retry_at = {}, {}
        self.job_id = digest({"request": asdict(request), "at": self.clock().isoformat(),
                              "pid": os.getpid(), "started": self.started})
        self.job = {"job_id": self.job_id, "state": "RUNNING", "as_of": request.as_of,
                    "state_scope": "requested_fact_collection_not_trading_readiness",
                    "requested_codes": list(request.ordered_codes), "started_at": self.clock().isoformat(),
                    "pid": os.getpid(), "issues": [], "securities": [], "indices": [],
                    "event_conflicts": [],
                    "pending_event_ranges": [asdict(item) for item in request.pending_event_ranges]}
        with (self.repository.writer(), self.repository.validation_scope() as validation,
              self.service.repository.validation_scope(validation)):
            # Holding the exclusive writer lock proves no earlier market writer
            # is active. Preserve its evidence, but do not leave stale RUNNING flags.
            for table in ("jobs", "requests"):
                for identity, old in self.repository.states(table).items():
                    if old.get("state") == "RUNNING" and not (
                            table == "jobs" and identity == self.job_id):
                        old.update(state="PAUSED", pause_reason="INTERRUPTED_PREVIOUS_RUN",
                                   recovered_at=self.clock().isoformat())
                        self.repository.save_state(table, identity, old)
            self._state()
            try:
                if self.repair:
                    self._repair_objects()
                sessions = []
                calendar_issues = []
                if request.price_codes or request.include_indices:
                    calendar = self._calendar(request)
                    window = calendar.window(request.as_of, request.history_sessions)
                    if not window.ready:
                        calendar_issues = list(window.issues)
                        self.job["issues"].extend(window.issues)
                        if not request.pending_event_ranges:
                            self.job["state"] = "WAITING_DATA"
                            return self.job
                    else:
                        sessions = window.data
                self.job["sessions"] = sessions
                if request.price_codes and sessions:
                    self._securities(request.as_of)
                for code in request.ordered_codes:
                    wants_prices = code in request.price_codes
                    has_prices = wants_prices and bool(sessions)
                    if has_prices:
                        self._prices(code, sessions)
                    ranges = [item for item in request.pending_event_ranges if item.code == code]
                    event_refresh_complete = True
                    security = {"code": code, "prices": "NOT_REQUESTED", "issues": [],
                                "events": "NOT_REQUESTED", "event_coverage": "NOT_REQUESTED",
                                "event_issues": [], "pending_event_ranges": []}
                    if wants_prices and not sessions:
                        security.update(prices="WAITING_DATA", issues=calendar_issues,
                                        event_coverage=("WAITING_DATA" if request.include_events
                                                        else "NOT_REQUESTED"))
                    if has_prices:
                        facts = self.service.prices(
                            code, sessions[0], sessions[-1], knowledge_cutoff=self.clock())
                        security.update(prices=facts.state, issues=list(facts.issues))
                    if request.include_events:
                        if has_prices:
                            event_refresh_complete = self._events(code, sessions)
                            coverage = self.service.event_coverage(
                                code, sessions[0], sessions[-1], knowledge_cutoff=self.clock())
                            security.update(event_coverage=coverage.state,
                                            event_issues=list(coverage.issues))
                        for item in ranges:
                            refreshed = self._events(code, [item.start, item.end])
                            coverage = self.service.event_coverage(
                                code, item.start, item.end, knowledge_cutoff=self.clock())
                            event_refresh_complete = event_refresh_complete and refreshed
                            security["pending_event_ranges"].append({**asdict(item),
                                "state": "READY" if refreshed and coverage.ready else "WAITING_DATA",
                                "refresh_complete": refreshed,
                                "coverage": coverage.as_dict()})
                            security["event_issues"].extend(coverage.issues)
                        events = self.service.events(code, knowledge_cutoff=self.clock())
                        security.update(events=events.state,
                                        event_refresh_complete=event_refresh_complete)
                        security["event_issues"].extend(events.issues)
                        self.job["event_conflicts"].extend(
                            {"code": code, "event_id": issue["event_id"],
                             "details": issue["details"]}
                            for issue in events.issues if issue.get("kind") == "EVENT_CONFLICT"
                        )
                    self.job["securities"].append(security)
                    self._state()
                if request.include_indices:
                    previous_index_session = (calendar.previous_session(sessions[-1])
                                              if sessions else None)
                    index_sessions = ([previous_index_session.data, sessions[-1]]
                                      if previous_index_session and previous_index_session.ready
                                      else sessions[-1:])
                    for code in self.benchmarks:
                        if not index_sessions:
                            self.job["indices"].append({"code": code, "prices": "WAITING_DATA",
                                                       "issues": calendar_issues})
                            continue
                        self._prices(code, index_sessions, index=True)
                        result = self.service.index_quote(
                            code,
                            sessions[-1],
                            knowledge_cutoff=self.clock(),
                        )
                        self.job["indices"].append({"code": code, "prices": result.state,
                                                    "issues": list(result.issues)})
                # Success describes the requested facts, not the separate trading application.
                missing = any(row["prices"] not in {"READY", "NOT_REQUESTED"}
                              for row in self.job["securities"])
                event_missing = request.include_events and any(
                    row["event_coverage"] not in {"READY", "NOT_REQUESTED"}
                    or row["events"] not in {"READY", "CONFLICT"}
                    or not row["event_refresh_complete"]
                    or any(item["state"] != "READY" for item in row["pending_event_ranges"])
                    for row in self.job["securities"]
                )
                self.job["indices_complete"] = all(
                    r["prices"] == "READY" for r in self.job["indices"]
                )
                self.job["state"] = ("PARTIAL" if missing or event_missing
                                     or not self.job["indices_complete"] else "COMPLETE")
                return self.job
            except KeyboardInterrupt:
                self.job.update(state="PAUSED", pause_reason="INTERRUPTED",
                                interrupted_at=self.clock().isoformat())
                raise
            except Exception as exc:
                self.job.update(state="ERROR", error=str(exc))
                raise
            finally:
                self._state()


def import_preparation(
    root: Path, *, as_of: str | None = None, activate_pool=True, effective_from=None
) -> dict:
    """Validate the consumed preparation facts before importing any observations.

    A saved progress flag is not evidence by itself. Prices require the matching
    artifact and accepted source response; a pool also requires every frozen
    selection dependency. Unusable stocks are omitted before opening their files.
    """
    from china_stock_sim.config import local_path
    from china_stock_sim.market.events import normalize_baostock_events
    from china_stock_sim.market.quality import validate_window
    from china_stock_sim.market.storage import response_content_digest
    from china_stock_sim.market.universe import normalize_securities

    repository = MarketRepository(root)
    state = read_json(local_path(repository.root, "data/state/preparation/latest.json"))
    if as_of and state["requested_as_of"] != as_of:
        raise ValueError("最近准备日期与导入日期不符")
    run_id, identity = state["run_id"], state["identity"]
    if (
        identity.get("as_of") != state["requested_as_of"]
        or run_id != state["requested_as_of"] + "-" + digest(identity)[:16]
    ):
        raise CorruptData("首次准备任务身份不符")
    requests = state["requests"]
    ready = {}
    for item in state.get("securities", []):
        if item.get("price_ready") is not True or item.get("state") == "CONFLICT":
            continue
        code = normalize_code(item["code"])
        if code in ready:
            raise CorruptData("首次准备就绪证券重复")
        ready[code] = item
    sources = {}

    def checked_source(key):
        if key in sources:
            return sources[key]
        item = requests[key]
        expected = {
            "provider": identity["provider"],
            "adapter_version": identity["adapter_version"],
            "query": item["query"],
            "params": item["params"],
            "vintage": run_id,
        }
        reference = f"data/raw/baostock/{key}.json"
        if (
            item["status"] != "OK"
            or digest(expected) != key
            or item.get("raw_reference") != reference
        ):
            raise CorruptData("首次准备源请求身份或状态不符")
        envelope = read_json(local_path(repository.root, reference))
        raw = envelope["response"]
        raw_digest, content_digest = digest(raw), response_content_digest(raw)
        if (
            envelope.get("identity") != expected
            or envelope.get("sha256") != raw_digest
            or item.get("raw_sha256") != raw_digest
            or item.get("content_sha256") != content_digest
            or raw.get("provider") != expected["provider"]
            or raw.get("adapter_version") != expected["adapter_version"]
        ):
            raise CorruptData("首次准备原件身份或内容校验失败")
        accepted = item.get("accepted_content_sha256")
        if accepted is not None and accepted != content_digest:
            raise CorruptData("首次准备原件与已确认内容不符")
        aware_time(raw["fetched_at"])
        sources[key] = raw
        return raw

    pool = None
    if state.get("pool"):
        pool_reference = f"data/state/preparation/{run_id}/pool.json"
        if state["pool"].get("full_pool_reference") != pool_reference:
            raise CorruptData("候选池引用不属于当前准备任务")
        envelope = read_json(local_path(repository.root, pool_reference))
        pool = envelope["pool"]
        if (
            digest(pool) != envelope.get("sha256")
            or pool.get("identity") != identity
            or pool.get("sessions") != state.get("sessions")
            or not isinstance(pool.get("dependencies"), dict)
            or not pool["dependencies"]
            or set(pool.get("source_requests", [])) != set(pool["dependencies"])
        ):
            raise CorruptData("候选池身份、内容或依赖集合校验失败")
        for field in ("selected", "frozen_at", "sessions", "source_requests", "dependencies"):
            if state["pool"].get(field) != pool.get(field):
                raise CorruptData("候选池文件与已保存的冻结记录不一致")
        for key, dependency in pool["dependencies"].items():
            raw = checked_source(key)
            expected = {
                "provider": identity["provider"],
                "adapter_version": identity["adapter_version"],
                "query": requests[key]["query"],
                "params": requests[key]["params"],
                "vintage": run_id,
            }
            if (
                dependency.get("identity") != expected
                or requests[key]["query"] not in {"calendar", "securities", "session_bars"}
                or dependency.get("content_sha256") != response_content_digest(raw)
            ):
                raise CorruptData("冻结候选池的源依赖内容不符")
        selected = [normalize_code(item["code"]) for item in pool["selected"]]
        if len(selected) != len(set(selected)) or not set(ready).issubset(selected):
            raise CorruptData("就绪证券与冻结候选池不一致")
    elif ready:
        raise CorruptData("就绪行情缺少冻结候选池依据")

    prepared_bars = {}
    for code, item in ready.items():
        reference = f"data/normalized/preparation/{run_id}/{code}.json"
        if item.get("artifact_reference") != reference:
            raise CorruptData("标准化行情引用不属于当前准备任务")
        artifact = read_json(local_path(repository.root, reference))
        sessions = state["sessions"]
        if (
            digest(artifact) != item.get("artifact_sha256")
            or artifact.get("code") != code
            or artifact.get("sessions") != sessions
            or artifact.get("price_ready") is not True
            or artifact.get("price_basis") != "unadjusted"
            or not sessions
            or sessions[-1] != state["effective_as_of"]
        ):
            raise CorruptData("标准化行情内容或窗口校验失败")
        params = {"code": code, "start_date": sessions[0], "end_date": sessions[-1]}
        key = digest(
            {
                "provider": identity["provider"],
                "adapter_version": identity["adapter_version"],
                "query": "history",
                "params": params,
                "vintage": run_id,
            }
        )
        if artifact.get("source_requests") != [key] or not requests[key].get(
            "accepted_content_sha256"
        ):
            raise CorruptData("标准化行情缺少匹配的已确认来源")
        raw = checked_source(key)
        bars = normalize_bars(artifact["bars"], expected_code=code, allowed_dates=sessions)
        if not validate_window(bars, code, sessions)["complete"] or bars != normalize_bars(
            raw["rows"], expected_code=code, allowed_dates=sessions
        ):
            raise CorruptData("标准化行情与已确认原件不一致")
        prepared_bars[key] = bars

    # Validate and normalize every consumed source before the repository write phase.
    entries, event_conflicts = [], []
    for key, item in requests.items():
        if item["status"] != "OK":
            continue
        query, params = item["query"], item["params"]
        if query == "history" and key not in prepared_bars:
            continue
        if query == "dividends" and normalize_code(params["code"]) not in ready:
            continue
        if query not in {"calendar", "securities", "history", "dividends"}:
            continue
        raw = checked_source(key)
        if query == "calendar":
            records = [
                {"date": row["calendar_date"], "is_trading_day": row["is_trading_day"]}
                for row in raw["rows"]
            ]
            TradingCalendar(records)
            entries.append(("calendar", records, raw, ["date"]))
        elif query == "history":
            entries.append(("bar", prepared_bars[key], raw, None))
        elif query == "securities":
            records = normalize_securities(raw["rows"], state["effective_as_of"])
            for record in records:
                record["date"] = raw["fetched_at"][:10]
            entries.append(("security", records, raw, None))
        else:
            records = normalize_baostock_events(
                raw["rows"], params["code"], raw["fetched_at"],
                raw_reference=f"data/raw/market/{digest(raw)}.json")
            event_conflicts.extend(
                {"code": record["code"], "event_id": record["event_id"],
                 "issues": record["conflicts"], "raw_reference": record["raw_reference"]}
                for record in records if record.get("conflicts"))
            entries.append(("event", records, raw, None))

    activate = activate_pool and pool is not None and state.get("price_ready") is True
    if activate and set(normalize_code(row["code"]) for row in pool["selected"]) != set(ready):
        raise CorruptData("不能启用含未就绪证券的候选池")
    effective = normalize_date(effective_from or state["effective_as_of"])
    if effective < state["effective_as_of"]:
        raise ValueError("候选池生效日期不能早于准备截止日")
    count = 0
    with repository.writer():
        for kind, records, raw, key_fields in entries:
            if kind == "event":
                _put_normalized_events(repository, records, raw, timestamp())
            else:
                repository.put(kind, records, raw, key_fields=key_fields)
            count += len(records)
        if activate:
            dependencies = [
                repository.save_raw(sources[key]) for key in sorted(pool["dependencies"])
            ]
            raw = {
                "provider": "pool_selection",
                "adapter_version": "1",
                "fetched_at": pool["frozen_at"],
                "pool": pool,
                "source_run": run_id,
                "dependencies": dependencies,
            }
            repository.put(
                "pool",
                [
                    {
                        "date": state["effective_as_of"],
                        "code": "",
                        "selected": pool["selected"],
                        "excluded": pool["excluded"],
                        "frozen_at": pool["frozen_at"],
                        "effective_from": effective,
                    }
                ],
                raw,
            )
    return {"state": "IMPORTED", "records": count, "event_conflicts": event_conflicts}
