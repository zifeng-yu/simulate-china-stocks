"""Offline integration tests: synthetic source responses, real local cache/state.

Fixture calendars deliberately mark weekdays as sessions. Production must use
the supplied calendar flags; no weekday fallback is exercised or allowed there.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from china_stock_sim.market.preparation import (
    Preparation,
    data_status,
    repair_preparation_cache,
    validate_as_of,
)
from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.storage import ResponseCache, digest, read_json, write_json

AS_OF = "2026-10-27"
REPOSITORY = Path(__file__).resolve().parents[1]
BAR_FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "tradestatus",
    "isST",
]
ADJUSTMENT_FIELDS = [
    "code",
    "dividOperateDate",
    "foreAdjustFactor",
    "backAdjustFactor",
    "adjustFactor",
]


def calendar_fixture(start: str, end: str) -> list[dict]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return [
        {"calendar_date": day.isoformat(), "is_trading_day": str(int(day.weekday() < 5))}
        for day in days
    ]


class Clock:
    def __init__(self):
        self.value = datetime.fromisoformat("2026-10-28T20:00:00+08:00")

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeProvider:
    provider_id = "baostock"
    adapter_version = "1"

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.failures = {}
        self.missing_history = set()
        self.missing_calendar = set()
        self.missing_clue_fields = False
        self.missing_security_fields = False
        self.bad_bulk_row = False
        self.duplicate_bulk_row = False
        self.unknown_bulk_row = False
        self.young_security = False
        self.name = "测试股票"
        self.codes = ("SH600000", "SZ000001")

    @property
    def sessions(self):
        end = date.fromisoformat(AS_OF)
        rows = calendar_fixture((end - timedelta(days=550)).isoformat(), AS_OF)
        return [row["calendar_date"] for row in rows if row["is_trading_day"] == "1"][-121:]

    @staticmethod
    def bar(code, session):
        return {
            "date": session,
            "code": code[:2].lower() + "." + code[2:],
            "open": "10.00",
            "high": "10.50",
            "low": "9.50",
            "close": "10.10",
            "preclose": "10.00",
            "volume": "10000",
            "amount": "100100.25",
            "adjustflag": "3",
            "tradestatus": "1",
            "isST": "0",
        }

    def fetch(self, query, params):
        self.calls.append((query, dict(params)))
        failures = self.failures.get(query, [])
        if failures:
            raise failures.pop(0)
        if query == "calendar":
            fields = ["calendar_date", "is_trading_day"]
            rows = [
                row
                for row in calendar_fixture(params["start_date"], params["end_date"])
                if row["calendar_date"] not in self.missing_calendar
            ]
        elif query == "securities":
            fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
            if self.missing_security_fields:
                fields.remove("type")
            rows = [
                {
                    "code": code[:2].lower() + "." + code[2:],
                    "code_name": self.name,
                    "ipoDate": "2000-01-01",
                    "outDate": "",
                    "type": "1",
                    "status": "1",
                }
                for code in self.codes
            ]
            if self.young_security:
                rows[0]["ipoDate"] = self.sessions[-10]
        elif query == "session_bars":
            fields = BAR_FIELDS
            rows = [self.bar(code, params["date"]) for code in self.codes]
            if self.bad_bulk_row:
                rows[0]["amount"] = "NaN"
            if self.duplicate_bulk_row:
                rows.append(dict(rows[0]))
            if self.unknown_bulk_row:
                rows.append({**rows[0], "code": "not-a-code", "amount": "invalid"})
        elif query == "history":
            fields = BAR_FIELDS
            rows = [
                self.bar(params["code"], session)
                for session in self.sessions
                if (params["code"], session) not in self.missing_history
            ]
        elif query in {"adjustments", "dividends"}:
            fields = ADJUSTMENT_FIELDS if query == "adjustments" else ["code", "dividOperateDate"]
            if self.missing_clue_fields:
                fields = ["code"]
            rows = []
        else:
            raise AssertionError(f"Unexpected source query: {query}")
        return {
            "fields": fields,
            "rows": rows,
            "provider": self.provider_id,
            "adapter_version": self.adapter_version,
            "fetched_at": self.clock().isoformat(),
        }


@pytest.fixture
def prepared_project(project, monkeypatch):
    (project / "configs/preparation.toml").write_bytes(
        (REPOSITORY / "configs/preparation.toml").read_bytes()
    )
    # Presentation is tested separately; keep all data/cache/state I/O real here.
    import sys

    monkeypatch.setitem(
        sys.modules,
        "china_stock_sim.reporting.preparation",
        SimpleNamespace(
            write_preparation_report=lambda root, state: None,
        ),
    )
    return project


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def provider(clock):
    return FakeProvider(clock)


def run(project, provider, clock, **options):
    preparation = Preparation(
        project,
        AS_OF,
        provider=provider,
        clock=clock,
        download=options.pop("download", True),
        **options,
    )
    return preparation, preparation.run()


def query_status(state, query, code=None):
    matching = [
        value
        for value in state["requests"].values()
        if value["query"] == query and (code is None or value["params"].get("code") == code)
    ]
    assert len(matching) == 1
    return matching[0]


def test_offline_plan_does_not_download_or_create_accounts(prepared_project, provider, clock):
    assert data_status(prepared_project)["state"] == "NOT_STARTED"
    _, state = run(prepared_project, provider, clock, download=False)
    assert provider.calls == []
    assert state["state"] == "WAITING_DATA"
    assert query_status(state, "calendar")["status"] == "WAITING_DOWNLOAD"
    assert state["price_ready"] is False
    assert state["network_tested"] is False
    assert not list(prepared_project.rglob("*.sqlite*"))
    assert not (prepared_project / ".env").exists()


def test_complete_preparation_freezes_pool_and_resumes_without_requests(
    prepared_project,
    provider,
    clock,
    monkeypatch,
):
    from china_stock_sim.ledger import store
    from china_stock_sim.models import factory

    def forbidden(*args, **kwargs):
        pytest.fail("Data preparation must not initialize accounts or invoke a model")

    monkeypatch.setattr(store, "initialize", forbidden)
    monkeypatch.setattr(factory, "create_model_client", forbidden)
    preparation, state = run(prepared_project, provider, clock)
    assert state["state"] == "PREPARATION_COMPLETE"
    assert state["price_ready"] is True
    assert state["clues_downloaded"] is True
    assert state["signal_ready"] is False
    assert state["trading_ready"] is False
    assert state["corporate_actions_status"] == "CLUES_ONLY"
    assert Counter(query for query, _ in provider.calls) == {
        "calendar": 1,
        "securities": 1,
        "session_bars": 20,
        "history": 2,
        "adjustments": 2,
        "dividends": 2,
    }
    assert state["pool"]["selected_count"] == 2
    frozen_pool = (preparation.state_dir / "pool.json").read_bytes()
    for code in provider.codes:
        normalized = read_json(preparation.output_dir / f"{code}.json")
        assert len(normalized["bars"]) == 121
        assert normalized["sessions"] == provider.sessions
        assert normalized["price_basis"] == "unadjusted"
        assert normalized["trading_ready"] is False
        assert read_json(preparation.output_dir / f"{code}-clues.json")["status"] == "CLUES_ONLY"
    assert not list(prepared_project.rglob("*.sqlite*"))
    assert not (prepared_project / ".env").exists()

    provider.name = "不得重新选池或替换冻结证券名称"
    original_calls = list(provider.calls)
    clock.advance(120)
    resumed, after = run(prepared_project, provider, clock)
    assert provider.calls == original_calls
    assert resumed.run_id == preparation.run_id
    assert after["state"] == "PREPARATION_COMPLETE"
    assert after["requests_this_run"] == 0
    assert (preparation.state_dir / "pool.json").read_bytes() == frozen_pool
    assert all(row["name"] == "测试股票" for row in after["pool"]["selected"])
    assert data_status(prepared_project)["run_id"] == preparation.run_id


def test_request_budgets_resume_only_unfinished_requests(prepared_project, provider, clock):
    preparation, state = run(prepared_project, provider, clock, max_requests=3)
    assert len(provider.calls) == 3
    assert state["state"] == "WAITING_DATA"
    assert not (preparation.state_dir / "pool.json").exists()
    assert any(value["status"] == "BUDGET_PAUSED" for value in state["requests"].values())
    for _ in range(6):
        previous = len(provider.calls)
        preparation, state = run(prepared_project, provider, clock, max_requests=5)
        assert len(provider.calls) - previous <= 5
        if state["state"] == "PREPARATION_COMPLETE":
            break
    assert state["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == 28
    identities = [(query, digest(params)) for query, params in provider.calls]
    assert len(set(identities)) == len(identities)


def test_time_budget_stops_and_later_run_finishes(prepared_project, provider, clock, monkeypatch):
    from china_stock_sim.market import preparation as module

    with monkeypatch.context() as patch:
        patch.setattr(module.time, "monotonic", lambda: 0.0 if len(provider.calls) < 2 else 10.0)
        _, state = run(prepared_project, provider, clock, budget_seconds=5)
    assert len(provider.calls) == 2
    assert state["state"] == "WAITING_DATA"
    assert state["price_ready"] is False
    _, completed = run(prepared_project, provider, clock)
    assert completed["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == 28


def test_missing_exact_history_day_cannot_be_ready_and_only_failed_request_retries(
    prepared_project,
    provider,
    clock,
):
    missing = provider.sessions[42]
    provider.missing_history.add(("SH600000", missing))
    preparation, state = run(prepared_project, provider, clock)
    assert state["state"] == "PARTIAL"
    assert state["price_ready"] is False
    assert state["trading_ready"] is False
    failure = query_status(state, "history", "SH600000")
    assert failure["status"] == "FAILED"
    assert failure["error_category"] == "UNPUBLISHED"
    assert missing in failure["error"]
    assert not (preparation.output_dir / "SH600000.json").exists()
    assert (preparation.output_dir / "SZ000001.json").exists()
    attempts = list((preparation.state_dir / "attempts").glob("*.json"))
    assert len(attempts) == 1
    assert len(read_json(attempts[0])["rejected_response"]["rows"]) == 120
    previous = len(provider.calls)
    provider.missing_history.clear()
    clock.advance(60)
    _, completed = run(prepared_project, provider, clock)
    assert completed["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == previous + 1
    assert provider.calls[-1][0] == "history"
    assert query_status(completed, "history", "SH600000")["attempts"] == 2


def test_source_failure_obeys_cooldown_before_retry(prepared_project, provider, clock):
    provider.failures["calendar"] = [ProviderError("fixture temporary failure", retryable=True)]
    _, first = run(prepared_project, provider, clock)
    assert first["state"] == "WAITING_DATA"
    assert len(provider.calls) == 1
    failed = query_status(first, "calendar")
    assert failed["attempts"] == 1
    assert failed["retryable"] is True
    assert datetime.fromisoformat(failed["next_retry_at"]) == clock() + timedelta(seconds=60)
    _, immediate = run(prepared_project, provider, clock)
    assert len(provider.calls) == 1
    assert query_status(immediate, "calendar")["attempts"] == 1
    clock.advance(59)
    run(prepared_project, provider, clock)
    assert len(provider.calls) == 1
    clock.advance(1)
    _, completed = run(prepared_project, provider, clock)
    assert completed["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == 29
    assert query_status(completed, "calendar")["attempts"] == 2
    assert "next_retry_at" not in query_status(completed, "calendar")


def test_nonretryable_source_error_can_recover_on_explicit_download_after_cooldown(
    prepared_project,
    provider,
    clock,
):
    provider.failures["calendar"] = [ProviderError("fixture configuration error", retryable=False)]
    run(prepared_project, provider, clock)
    clock.advance(59)
    _, state = run(prepared_project, provider, clock)
    assert len(provider.calls) == 1
    assert state["state"] == "WAITING_DATA"
    assert query_status(state, "calendar")["retryable"] is False
    clock.advance(1)
    _, recovered = run(prepared_project, provider, clock, download=True)
    assert recovered["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == 29
    assert query_status(recovered, "calendar")["attempts"] == 2


@pytest.mark.parametrize("bad_kind", ["invalid", "duplicate"])
def test_one_bad_stock_in_bulk_is_excluded_without_blocking_other_stock(
    prepared_project,
    provider,
    clock,
    bad_kind,
):
    provider.bad_bulk_row = bad_kind == "invalid"
    provider.duplicate_bulk_row = bad_kind == "duplicate"
    _, state = run(prepared_project, provider, clock)
    assert state["state"] == "PREPARATION_COMPLETE"
    assert state["price_ready"] is True
    assert [row["code"] for row in state["pool"]["selected"]] == ["SZ000001"]
    rejected = {row["code"]: row for row in state["pool"]["excluded"]}
    assert "缺失" in rejected["SH600000"]["reason"]
    assert rejected["SH600000"]["missing_dates"] == provider.sessions[-20:]
    assert len(state["universe_data_quality"]) == 20
    for day in state["universe_data_quality"]:
        assert day["valid_count"] == 1
        assert day["missing_codes"] == ["SH600000"]
        assert day["issues"][0]["code"] == "SH600000"
    assert [params["code"] for query, params in provider.calls if query == "history"] == [
        "SZ000001"
    ]


def test_unknown_bulk_code_is_recorded_without_blocking_valid_stocks(
    prepared_project,
    provider,
    clock,
):
    provider.unknown_bulk_row = True
    _, state = run(prepared_project, provider, clock)
    assert state["state"] == "PREPARATION_COMPLETE"
    assert state["pool"]["selected_count"] == 2
    for day in state["universe_data_quality"]:
        assert day["missing_codes"] == []
        assert day["issues"] == [{"code": "not-a-code", "reason": "忽略无法识别的代码"}]


def test_young_security_bad_bulk_data_does_not_affect_eligible_stock(
    prepared_project,
    provider,
    clock,
):
    provider.young_security = True
    provider.bad_bulk_row = True
    _, state = run(prepared_project, provider, clock)
    assert state["state"] == "PREPARATION_COMPLETE"
    assert [row["code"] for row in state["pool"]["selected"]] == ["SZ000001"]
    assert "121" in state["pool"]["excluded"][0]["reason"]
    for day in state["universe_data_quality"]:
        assert day["eligible_count"] == 1
        assert day["valid_count"] == 1
        assert day["issues"] == []


@pytest.mark.parametrize(
    "now,requested,valid",
    [
        ("2026-10-28T18:09:59+08:00", "2026-10-28", False),
        ("2026-10-28T18:10:00+08:00", "2026-10-28", True),
        ("2026-10-28T00:01:00+08:00", "2026-10-27", True),
        ("2026-10-28T23:59:59+08:00", "2026-10-29", False),
        ("2026-10-28T10:09:59+00:00", "2026-10-28", False),
    ],
)
def test_cutoff_respects_shanghai_1810_and_rejects_future(now, requested, valid):
    if valid:
        assert validate_as_of(requested, datetime.fromisoformat(now)).isoformat() == requested
    else:
        with pytest.raises(ValueError, match="18:10"):
            validate_as_of(requested, datetime.fromisoformat(now))


def test_invalid_cutoff_is_rejected_before_io(prepared_project, provider, clock):
    with pytest.raises(ValueError, match="未来行情"):
        Preparation(prepared_project, "2026-10-29", provider=provider, clock=clock, download=True)
    assert provider.calls == []
    assert not (prepared_project / "data").exists()


def test_raw_cache_corruption_is_reported_without_silent_redownload(
    prepared_project,
    provider,
    clock,
):
    _, state = run(prepared_project, provider, clock)
    reference = query_status(state, "calendar")["raw_reference"]
    path = prepared_project / reference
    envelope = read_json(path)
    envelope["response"]["rows"].pop()
    write_json(path, envelope)  # Keep the old content hash to simulate disk corruption.
    corrupted = path.read_bytes()
    previous = len(provider.calls)
    _, resumed = run(prepared_project, provider, clock)
    assert resumed["state"] == "WAITING_DATA"
    assert resumed["price_ready"] is False
    assert query_status(resumed, "calendar")["status"] == "CACHE_INVALID"
    assert "缓存校验失败" in query_status(resumed, "calendar")["error"]
    assert len(provider.calls) == previous
    assert path.read_bytes() == corrupted


def test_frozen_pool_corruption_cannot_trigger_reselection(prepared_project, provider, clock):
    preparation, _ = run(prepared_project, provider, clock)
    path = preparation.state_dir / "pool.json"
    envelope = read_json(path)
    envelope["pool"]["selected"].clear()
    write_json(path, envelope)
    corrupted = path.read_bytes()
    previous = len(provider.calls)
    _, state = run(prepared_project, provider, clock)
    assert state["state"] == "NEEDS_DATA_REVIEW"
    assert state["price_ready"] is False
    assert "冻结股票池校验失败" in state["error"]
    assert len(provider.calls) == previous
    assert path.read_bytes() == corrupted


def test_calendar_missing_any_date_is_not_filled_from_weekdays(prepared_project, provider, clock):
    provider.missing_calendar.add("2026-10-25")  # Even a non-session calendar row is required.
    _, state = run(prepared_project, provider, clock)
    assert state["state"] == "WAITING_DATA"
    assert state["price_ready"] is False
    assert "交易日历存在缺日" in query_status(state, "calendar")["error"]
    assert [query for query, _ in provider.calls] == ["calendar"]


def test_empty_clues_need_valid_source_fields(prepared_project, provider, clock):
    provider.missing_clue_fields = True
    _, state = run(prepared_project, provider, clock)
    assert state["price_ready"] is True
    assert state["clues_downloaded"] is False
    assert state["state"] == "PARTIAL"
    assert state["trading_ready"] is False
    assert "必要字段" in query_status(state, "adjustments", "SH600000")["error"]


def test_security_source_schema_is_checked_before_pool_selection(
    prepared_project,
    provider,
    clock,
):
    provider.missing_security_fields = True
    preparation, state = run(prepared_project, provider, clock)
    assert state["state"] == "WAITING_DATA"
    assert state["price_ready"] is False
    assert "必要字段" in query_status(state, "securities")["error"]
    assert [query for query, _ in provider.calls] == ["calendar", "securities"]
    attempts = list((preparation.state_dir / "attempts").glob("*.json"))
    assert len(attempts) == 1
    assert "type" not in read_json(attempts[0])["rejected_response"]["fields"]


def test_response_cache_verifies_both_identity_and_content(tmp_path):
    cache = ResponseCache(tmp_path)
    identity = {"query": "calendar", "params": {"end_date": AS_OF}}
    response = {"fields": [], "rows": []}
    assert cache.load(identity) is None
    cache.save(identity, response)
    assert cache.load(identity) == response
    path = tmp_path / f"{digest(identity)}.json"
    envelope = read_json(path)
    envelope["identity"]["query"] = "other"
    write_json(path, envelope)
    with pytest.raises(ValueError, match="缓存校验失败"):
        cache.load(identity)


def test_reporting_failure_cannot_interrupt_data_preparation(
    prepared_project, provider, clock, monkeypatch
):
    import sys

    def broken_renderer(*args, **kwargs):
        pytest.fail("The independent data layer must never call a report renderer")

    monkeypatch.setattr(
        sys.modules["china_stock_sim.reporting.preparation"],
        "write_preparation_report",
        broken_renderer,
    )
    _, state = run(prepared_project, provider, clock)
    assert state["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == 28


def test_overlapping_batch_and_history_conflict_isolated_to_one_security(
    prepared_project,
    provider,
    clock,
    monkeypatch,
):
    original = provider.fetch

    def conflicting(query, params):
        response = original(query, params)
        if query == "history" and params["code"] == "SH600000":
            response["rows"][-1]["close"] = "10.20"
        return response

    monkeypatch.setattr(provider, "fetch", conflicting)
    preparation, state = run(prepared_project, provider, clock)
    assert state["state"] == "PARTIAL"
    stocks = {row["code"]: row for row in state["securities"]}
    assert stocks["SH600000"]["state"] == "CONFLICT"
    assert stocks["SH600000"]["price_ready"] is False
    assert stocks["SZ000001"]["price_ready"] is True
    assert not (preparation.output_dir / "SH600000.json").exists()
    evidence = read_json(prepared_project / stocks["SH600000"]["conflict_reference"])
    assert evidence["conflicts"] == [
        {
            "code": "SH600000",
            "date": AS_OF,
            "fields": {"close": {"batch": "10.10", "history": "10.20"}},
        }
    ]
    assert len(evidence["batch_requests"]) == 20
    assert state["requests"][evidence["history_request"]]["status"] == "OK"
    before = len(provider.calls)
    _, again = run(prepared_project, provider, clock)
    assert again["state"] == "PARTIAL"
    assert len(provider.calls) == before


@pytest.mark.parametrize("unknown", [False, True])
def test_empty_eligibility_stops_before_bulk_and_distinguishes_unknown_metadata(
    prepared_project,
    provider,
    clock,
    monkeypatch,
    unknown,
):
    original = provider.fetch

    def no_eligible(query, params):
        response = original(query, params)
        if query == "securities":
            for row in response["rows"]:
                if unknown:
                    row["status"] = "unknown"
                else:
                    row["ipoDate"] = AS_OF
        return response

    monkeypatch.setattr(provider, "fetch", no_eligible)
    preparation, state = run(prepared_project, provider, clock)
    assert len(provider.calls) == 2
    assert state["price_ready"] is False
    if unknown:
        assert state["state"] == "NEEDS_DATA_REVIEW"
        assert state["error_category"] == "FORMAT"
        assert not (preparation.state_dir / "pool.json").exists()
    else:
        assert state["state"] == "NO_ELIGIBLE_SECURITIES"
        assert state["pool"]["selected"] == []
        assert len(state["pool"]["excluded"]) == 2
        assert len(state["pool"]["dependencies"]) == 2
        _, resumed = run(prepared_project, provider, clock)
        assert resumed["state"] == "NO_ELIGIBLE_SECURITIES"
        assert len(provider.calls) == 2


def damage_bulk_cache(project, state):
    key, request = next(
        (key, value) for key, value in state["requests"].items() if value["query"] == "session_bars"
    )
    path = project / request["raw_reference"]
    envelope = read_json(path)
    envelope["response"]["rows"].clear()
    write_json(path, envelope)
    return key, path


def test_frozen_pool_dependencies_are_verified_and_repair_preserves_original_bytes(
    prepared_project,
    provider,
    clock,
):
    preparation, initial = run(prepared_project, provider, clock)
    frozen = (preparation.state_dir / "pool.json").read_bytes()
    key, raw = damage_bulk_cache(prepared_project, initial)
    corrupted = raw.read_bytes()
    before = len(provider.calls)
    _, broken = run(prepared_project, provider, clock)
    assert broken["state"] == "WAITING_DATA"
    assert broken["price_ready"] is False
    assert broken["requests"][key]["status"] == "CACHE_INVALID"
    assert broken["requests"][key]["error_category"] == "CACHE"
    assert len(provider.calls) == before
    clock.advance(120)  # A new fetched_at is allowed when the source facts are identical.
    repaired = repair_preparation_cache(prepared_project, AS_OF, provider=provider, clock=clock)
    assert repaired["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == before + 1
    assert (preparation.state_dir / "pool.json").read_bytes() == frozen
    repair = repaired["requests"][key]["repairs"][-1]
    quarantine = preparation.cache.directory / repair["quarantine_reference"]
    assert (quarantine / "original.json").read_bytes() == corrupted
    assert read_json(quarantine / "repair.json")["missing"] is False
    assert read_json(raw)["response"]["rows"]


def test_repair_changed_source_content_records_conflict_without_reselecting_pool(
    prepared_project,
    provider,
    clock,
    monkeypatch,
):
    preparation, initial = run(prepared_project, provider, clock)
    frozen = (preparation.state_dir / "pool.json").read_bytes()
    key, raw = damage_bulk_cache(prepared_project, initial)
    original = provider.fetch

    def revised(query, params):
        response = original(query, params)
        if query == "session_bars":
            response["rows"][0]["close"] = "10.20"
        return response

    monkeypatch.setattr(provider, "fetch", revised)
    changed = repair_preparation_cache(prepared_project, AS_OF, provider=provider, clock=clock)
    assert changed["state"] == "WAITING_DATA"
    assert changed["price_ready"] is False
    assert changed["requests"][key]["status"] == "CONFLICT"
    assert changed["requests"][key]["error_category"] == "CONFLICT"
    assert (prepared_project / changed["requests"][key]["revision_reference"]).exists()
    assert not raw.exists()
    assert (preparation.state_dir / "pool.json").read_bytes() == frozen


def test_missing_frozen_dependency_requires_explicit_repair(prepared_project, provider, clock):
    _, state = run(prepared_project, provider, clock)
    key, request = next(
        (key, value) for key, value in state["requests"].items() if value["query"] == "session_bars"
    )
    (prepared_project / request["raw_reference"]).unlink()
    before = len(provider.calls)
    _, missing = run(prepared_project, provider, clock)
    assert missing["requests"][key]["status"] == "CACHE_INVALID"
    assert len(provider.calls) == before
    repaired = repair_preparation_cache(prepared_project, AS_OF, provider=provider, clock=clock)
    assert repaired["state"] == "PREPARATION_COMPLETE"
    assert len(provider.calls) == before + 1
    assert repaired["requests"][key]["repairs"][-1]["missing"] is True


def test_successful_response_cache_cannot_be_silently_overwritten(tmp_path):
    cache = ResponseCache(tmp_path)
    identity = {"query": "calendar"}
    cache.save(identity, {"rows": []})
    with pytest.raises(ValueError, match="不能原地覆盖"):
        cache.save(identity, {"rows": [{"changed": "yes"}]})
    assert cache.load(identity) == {"rows": []}


@pytest.mark.parametrize(
    "failure,category",
    [
        (ProviderError("source timeout", retryable=True), "NETWORK"),
        (ProviderError("missing sdk", retryable=False), "CONFIGURATION"),
        (ValueError("bad source format"), "FORMAT"),
    ],
)
def test_source_failure_categories_are_persisted(
    prepared_project, provider, clock, failure, category
):
    provider.failures["calendar"] = [failure]
    preparation, state = run(prepared_project, provider, clock)
    assert query_status(state, "calendar")["error_category"] == category
    record = read_json(next((preparation.state_dir / "attempts").glob("*.json")))
    assert record["outcome"]["error_category"] == category


def test_interrupted_attempt_is_recorded_and_numbering_continues(prepared_project, provider, clock):
    provider.failures["calendar"] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        run(prepared_project, provider, clock)
    saved = data_status(prepared_project)
    assert query_status(saved, "calendar")["status"] == "RUNNING"
    preparation, resumed = run(prepared_project, provider, clock)
    assert resumed["state"] == "PREPARATION_COMPLETE"
    assert query_status(resumed, "calendar")["attempts"] == 2
    records = [read_json(path) for path in (preparation.state_dir / "attempts").glob("*.json")]
    assert any(row["outcome"]["error_category"] == "INTERRUPTED" for row in records)


def test_attempt_evidence_restores_cooldown_if_manifest_is_missing(
    prepared_project, provider, clock
):
    provider.failures["calendar"] = [ProviderError("temporary", retryable=True)]
    preparation, _ = run(prepared_project, provider, clock)
    preparation.manifest_path.unlink()
    _, waiting = run(prepared_project, provider, clock)
    assert query_status(waiting, "calendar")["attempts"] == 1
    assert len(provider.calls) == 1
    clock.advance(60)
    _, completed = run(prepared_project, provider, clock)
    assert completed["state"] == "PREPARATION_COMPLETE"
    assert query_status(completed, "calendar")["attempts"] == 2


def test_missing_frozen_pool_is_not_silently_reselected_even_in_repair_mode(
    prepared_project,
    provider,
    clock,
):
    preparation, _ = run(prepared_project, provider, clock)
    (preparation.state_dir / "pool.json").unlink()
    before = len(provider.calls)
    state = repair_preparation_cache(prepared_project, AS_OF, provider=provider, clock=clock)
    assert state["state"] == "NEEDS_DATA_REVIEW"
    assert "冻结股票池文件缺失" in state["error"]
    assert len(provider.calls) == before
    assert not (preparation.state_dir / "pool.json").exists()


def test_repair_changed_accepted_history_is_a_local_conflict_and_keeps_previous_artifact(
    prepared_project,
    provider,
    clock,
    monkeypatch,
):
    preparation, initial = run(prepared_project, provider, clock)
    request = query_status(initial, "history", "SH600000")
    raw_path = prepared_project / request["raw_reference"]
    raw_path.write_text("damaged source file", encoding="utf-8")
    artifact = preparation.output_dir / "SH600000.json"
    previous = artifact.read_bytes()
    original = provider.fetch

    def revised(query, params):
        response = original(query, params)
        if query == "history" and params["code"] == "SH600000":
            response["rows"][0]["close"] = "10.20"
        return response

    monkeypatch.setattr(provider, "fetch", revised)
    state = repair_preparation_cache(prepared_project, AS_OF, provider=provider, clock=clock)
    assert state["state"] == "PARTIAL"
    stocks = {row["code"]: row for row in state["securities"]}
    assert stocks["SH600000"]["state"] == "CONFLICT"
    assert stocks["SH600000"]["price_ready"] is False
    assert stocks["SZ000001"]["price_ready"] is True
    assert artifact.read_bytes() == previous
    assert not raw_path.exists()


def test_provider_explicit_format_category_overrides_retryability(
    prepared_project,
    provider,
    clock,
):
    failure = ProviderError("invalid provider schema", retryable=False)
    failure.category = "FORMAT"
    provider.failures["calendar"] = [failure]
    _, state = run(prepared_project, provider, clock)
    assert query_status(state, "calendar")["error_category"] == "FORMAT"
