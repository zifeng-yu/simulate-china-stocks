"""Current data CLI, pool lifecycle and preparation import integration tests."""

import json
import socket
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from china_stock_sim.market import cli as data_cli
from china_stock_sim.market import pools
from china_stock_sim.market.preparation import Preparation, repair_preparation_cache
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.storage import digest, read_json, write_json
from china_stock_sim.market.update import import_preparation

REPOSITORY = Path(__file__).resolve().parents[1]
AS_OF = "2024-02-29"
CLOCK = datetime.fromisoformat("2024-02-29T20:00:00+08:00")
CODES = ("SH600000", "SZ000001")


def days(start, end):
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    result = []
    while current <= last:
        result.append(
            {"date": current.isoformat(), "is_trading_day": str(int(current.weekday() < 5))}
        )
        current += timedelta(days=1)
    return result


def response(rows, *, fetched="2024-02-01T18:30:00+08:00", provider="fixture"):
    return {
        "provider": provider,
        "adapter_version": "1",
        "fetched_at": fetched,
        "fields": sorted(set().union(*(row.keys() for row in rows))) if rows else [],
        "rows": rows,
    }


class PreparationSource:
    provider_id = "baostock"
    adapter_version = "1"

    def __init__(self, conflict=False):
        self.conflict = conflict
        self.calls = []
        self.fetched_at = CLOCK.isoformat()

    @staticmethod
    def bar(code, day):
        return {
            "code": code,
            "date": day,
            "open": "10.00",
            "high": "11.00",
            "low": "9.00",
            "close": "10.00",
            "preclose": "10.00",
            "volume": "10000",
            "amount": "100000.00",
            "tradestatus": "1",
            "isST": "0",
            "adjustflag": "3",
        }

    def fetch(self, query, params):
        self.calls.append((query, dict(params)))
        if query == "calendar":
            rows = [
                {"calendar_date": row["date"], "is_trading_day": row["is_trading_day"]}
                for row in days(params["start_date"], params["end_date"])
            ]
            fields = ["calendar_date", "is_trading_day"]
        elif query == "securities":
            rows = [
                {
                    "code": code,
                    "code_name": "正常股票",
                    "ipoDate": "2000-01-01",
                    "outDate": "",
                    "type": "1",
                    "status": "1",
                }
                for code in CODES
            ]
            fields = list(rows[0])
        elif query == "session_bars":
            rows = [self.bar(code, params["date"]) for code in CODES]
            fields = list(rows[0])
        elif query == "history":
            rows = [
                self.bar(params["code"], row["date"])
                for row in days(params["start_date"], params["end_date"])
                if row["is_trading_day"] == "1"
            ]
            if self.conflict and params["code"] == "SH600000":
                rows[-1]["close"] = "10.20"
            fields = list(rows[0])
        elif query == "dividends":
            rows, fields = [], ["code", "dividOperateDate", "dividCashPsBeforeTax"]
        elif query == "adjustments":
            rows, fields = (
                [],
                [
                    "code",
                    "dividOperateDate",
                    "foreAdjustFactor",
                    "backAdjustFactor",
                    "adjustFactor",
                ],
            )
        else:
            raise AssertionError(query)
        return {
            "provider": "baostock",
            "adapter_version": "1",
            "fetched_at": self.fetched_at,
            "fields": fields,
            "rows": rows,
        }


@pytest.fixture
def root(tmp_path):
    project = (tmp_path / "data_only").resolve()
    (project / "configs").mkdir(parents=True)
    (project / "configs/preparation.toml").write_bytes(
        (REPOSITORY / "configs/preparation.toml").read_bytes()
    )
    return project


def prepare(root, *, conflict=False):
    source = PreparationSource(conflict)
    job = Preparation(root, AS_OF, provider=source, clock=lambda: CLOCK, download=True)
    state = job.run()
    assert state["state"] == ("PARTIAL" if conflict else "PREPARATION_COMPLETE")
    return job, state


def seed_pool(root, *, pool_date="2024-02-01"):
    repository = MarketRepository(root)
    calendar = days("2024-02-01", "2024-03-05")
    repository.put("calendar", calendar, response(calendar), key_fields=["date"])
    pool = {
        "code": "",
        "date": pool_date,
        "effective_from": pool_date,
        "selected": [{"code": "SH600000", "name": "原池股票"}],
        "excluded": [],
        "frozen_at": pool_date + "T18:30:00+08:00",
    }
    repository.put("pool", [pool], response([pool], fetched=pool["frozen_at"]))
    return repository


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize(
    "command",
    [
        ["data-status"],
        ["data-query", "--code", "SH600000", "--as-of", AS_OF],
        ["data-calendar", "next_session", "--date", AS_OF],
        ["data-revisions", "--code", "SH600000"],
        ["data-doctor"],
    ],
)
@pytest.mark.parametrize("entry", ["standalone", "application"])
def test_read_only_data_commands_need_no_account_model_or_network(
    root, command, entry, monkeypatch, capsys
):
    from china_stock_sim import cli as application_cli

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only data command accessed account/model/network execution")

    for name in (
        "load_settings",
        "load_model_settings",
        "model_status",
        "initialize",
        "read_snapshot",
    ):
        monkeypatch.setattr(application_cli, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    before = files(root)
    main = data_cli.main if entry == "standalone" else application_cli.main
    code = main(["--root", str(root), *command])
    output = json.loads(capsys.readouterr().out)
    assert code in (0, 2)
    assert output.get("state") != "ERROR"
    assert files(root) == before
    assert not (root / "configs/default.toml").exists()
    assert not (root / ".env").exists()


@pytest.mark.parametrize(
    "failure", [RuntimeError("broken renderer"), TypeError("bad template value")]
)
def test_application_report_failure_does_not_change_successful_data_result(
    root, monkeypatch, capsys, failure
):
    from china_stock_sim import cli as application_cli
    from china_stock_sim.reporting import preparation as reporting

    prepared = {"state": "PREPARATION_COMPLETE", "price_ready": True, "run_id": "fixture"}
    monkeypatch.setattr(
        data_cli,
        "prepare_pool",
        lambda *args, **kwargs: {
            "state": "POOL_PREPARED",
            "preparation": dict(prepared),
        },
    )

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(reporting, "write_preparation_report", broken)
    code = application_cli.main(["--root", str(root), "prepare-data", "--as-of", AS_OF])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    output = json.loads(captured.out)
    assert output["state"] == "POOL_PREPARED"
    assert output["preparation"] == prepared
    assert output["report_error"] == str(failure)


def test_data_status_is_saved_progress_not_consumer_readiness(root, capsys):
    saved = {
        "state": "PREPARATION_COMPLETE",
        "price_ready": True,
        "signal_ready": False,
        "trading_ready": False,
        "updated_at": CLOCK.isoformat(),
    }
    write_json(root / "data/state/preparation/latest.json", saved)
    before = files(root)
    assert data_cli.main(["--root", str(root), "data-status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status_scope"] == "saved_progress_not_readiness"
    assert output["preparation"] == saved
    assert not MarketDataService(root).readiness("SH600000", AS_OF, "valuation").ready
    assert files(root) == before


@pytest.mark.parametrize("command", [["data-doctor"], ["prepare-data", "--as-of", AS_OF]])
def test_conflicting_data_settings_fail_before_starting_preparation(root, command, capsys):
    (root / "configs/default.toml").write_text("[market]\nmax_pool_size = 99\n", encoding="utf-8")
    code = data_cli.main(["--root", str(root), *command])
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["state"] == "ERROR"
    assert "数据配置不一致" in output["reason"]
    assert not (root / "data").exists()


def test_non_month_end_keeps_pool_without_preparing(root, monkeypatch):
    seed_pool(root)
    before = files(root)
    monkeypatch.setattr(
        pools, "Preparation", lambda *args, **kwargs: pytest.fail("non-month-end preparation")
    )
    result = pools.prepare_pool(root, "2024-02-28")
    assert result["state"] == "UNCHANGED"
    assert result["active_pool"]["data"]["selected"][0]["name"] == "原池股票"
    assert files(root) == before


def test_month_end_successor_is_effective_next_session_and_previous_day_keeps_old_pool(root):
    repository = seed_pool(root)
    result = pools.prepare_pool(
        root, AS_OF, provider=PreparationSource(), clock=lambda: CLOCK, download=True
    )
    assert result["state"] == "POOL_PREPARED"
    assert result["effective_from"] == "2024-03-01"
    service = MarketDataService(root)
    previous = service.pool(AS_OF)
    following = service.pool("2024-03-01")
    assert previous.ready and previous.data["selected"][0]["name"] == "原池股票"
    assert following.ready and len(following.data["selected"]) == 2
    assert following.data["effective_from"] == "2024-03-01"
    assert len(repository.select("pool", latest=False)) == 2


def test_failed_month_end_preparation_retains_previous_pool(root, monkeypatch):
    repository = seed_pool(root)
    before = repository.select("pool", latest=False)

    class FailedPreparation:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return {"state": "PARTIAL", "price_ready": False, "pool": {"selected": []}}

    monkeypatch.setattr(pools, "Preparation", FailedPreparation)
    result = pools.prepare_pool(root, AS_OF, download=True)
    assert result["state"] == "KEEP_PREVIOUS"
    assert MarketDataService(root).pool("2024-03-01").data["selected"][0]["name"] == "原池股票"
    assert repository.select("pool", latest=False) == before


def test_forced_successor_does_not_hide_old_pool_with_same_selection_date(root):
    repository = seed_pool(root, pool_date=AS_OF)
    previous = MarketDataService(root).pool(AS_OF)
    assert previous.ready
    result = pools.prepare_pool(
        root, AS_OF, provider=PreparationSource(), clock=lambda: CLOCK, download=True, force=True
    )
    assert result["state"] == "POOL_PREPARED"
    assert result["effective_from"] == "2024-03-01"
    service = MarketDataService(root)
    old = service.pool(AS_OF)
    assert old.ready
    assert old.data["selected"] == previous.data["selected"]
    assert len(service.pool("2024-03-01").data["selected"]) == 2
    assert len(repository.select("pool", latest=False)) == 2


@pytest.mark.parametrize("remove_conflict_raw", [False, True])
def test_import_skips_conflicting_stock_even_when_its_source_request_succeeded(
    root, remove_conflict_raw
):
    _, state = prepare(root, conflict=True)
    assert any(
        item["query"] == "history"
        and item["params"]["code"] == "SH600000"
        and item["status"] == "OK"
        for item in state["requests"].values()
    )
    if remove_conflict_raw:
        conflicted = next(
            item
            for item in state["requests"].values()
            if item["query"] == "history" and item["params"]["code"] == "SH600000"
        )
        (root / conflicted["raw_reference"]).unlink()
    result = import_preparation(root, as_of=AS_OF)
    assert result["state"] == "IMPORTED"
    repository = MarketRepository(root)
    assert repository.select("bar", code="SH600000") == []
    assert len(repository.select("bar", code="SZ000001")) == 121
    assert repository.select("pool") == []


def test_distribution_conflict_is_imported_as_unusable_fact_without_blocking_prices(root):
    class DistributionSource(PreparationSource):
        def fetch(self, query, params):
            raw = super().fetch(query, params)
            if query == "dividends" and params == {"code": "SH600000", "year": 2024}:
                row = {"code": "SH600000", "dividPlanAnnounceDate": "2024-02-01",
                       "dividPlanDate": "2024-02-20", "dividRegistDate": "2024-02-27",
                       "dividOperateDate": "2024-02-28", "dividPayDate": "2024-02-28",
                       "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.3",
                       "dividStocksPs": "0", "dividReserveToStockPs": "0",
                       "dividCashStock": "10派2元"}
                raw.update(rows=[row], fields=list(row))
            return raw

    state = Preparation(root, AS_OF, download=True, provider=DistributionSource(),
                        clock=lambda: CLOCK).run()
    assert state["price_ready"] and state["clues_downloaded"]
    imported = import_preparation(root, as_of=AS_OF)
    assert imported["state"] == "IMPORTED"
    assert len(imported["event_conflicts"]) == 1
    assert imported["event_conflicts"][0]["code"] == "SH600000"
    assert imported["event_conflicts"][0]["raw_reference"].startswith("data/raw/market/")
    service = MarketDataService(root)
    assert service.pool(AS_OF).ready
    assert service.events("SH600000").state == "CONFLICT"
    for code in CODES:
        prices = service.prices(code, state["sessions"][0], AS_OF)
        assert prices.ready and len(prices.data) == 121
@pytest.mark.parametrize(
    "damage", ["artifact_missing", "artifact_content", "source_identity", "frozen_dependency"]
)
def test_import_validates_artifact_and_frozen_source_dependencies(root, damage):
    preparation, state = prepare(root)
    artifact = preparation.output_dir / "SH600000.json"
    if damage == "artifact_missing":
        artifact.unlink()
    elif damage == "artifact_content":
        value = read_json(artifact)
        value["bars"][-1]["close"] = "10.20"
        write_json(artifact, value)
    else:
        query = "history" if damage == "source_identity" else "session_bars"
        item = next(item for item in state["requests"].values() if item["query"] == query)
        path = root / item["raw_reference"]
        envelope = read_json(path)
        if damage == "source_identity":
            envelope["identity"]["vintage"] = "unrelated-task"
        else:
            envelope["response"]["rows"][0]["close"] = "10.20"
            envelope["sha256"] = digest(envelope["response"])
        write_json(path, envelope)
    with pytest.raises((CorruptData, ValueError, FileNotFoundError)):
        import_preparation(root, as_of=AS_OF)
    assert MarketRepository(root).select("pool") == []
    assert not MarketRepository(root).path.exists()


def test_import_accepts_explicit_repair_when_facts_match_but_acquisition_time_changed(root):
    _, state = prepare(root)
    batch = next(item for item in state["requests"].values() if item["query"] == "session_bars")
    path = root / batch["raw_reference"]
    envelope = read_json(path)
    envelope["response"]["rows"].clear()
    write_json(path, envelope)
    source = PreparationSource()
    source.fetched_at = (CLOCK + timedelta(seconds=120)).isoformat()
    repaired = repair_preparation_cache(
        root, AS_OF, provider=source, clock=lambda: CLOCK + timedelta(seconds=120)
    )
    assert repaired["state"] == "PREPARATION_COMPLETE"
    result = import_preparation(root, as_of=AS_OF)
    assert result["state"] == "IMPORTED"
    assert len(MarketRepository(root).select("bar")) == 242


def test_imported_pool_snapshot_recursively_checks_every_selection_source(root):
    prepare(root)
    import_preparation(root, as_of=AS_OF)
    repository = MarketRepository(root)
    pool = repository.select("pool")[0]
    raw = read_json(root / pool["raw_ref"])
    assert len(raw["dependencies"]) == 22
    assert all(reference.startswith("data/raw/market/") for reference in raw["dependencies"])
    snapshot = repository.freeze({"pool": pool["id"]}, [pool["id"]])
    dependency = root / raw["dependencies"][0]
    content = read_json(dependency)
    content["rows"] = []
    write_json(dependency, content)
    with pytest.raises(CorruptData):
        repository.read_snapshot(snapshot["snapshot_id"])


@pytest.mark.parametrize("damage", ["accepted_history", "pool_identity", "dropped_dependency"])
def test_import_rejects_changed_acceptance_or_frozen_pool_claims(root, damage):
    preparation, state = prepare(root)
    if damage == "accepted_history":
        history = next(row for row in state["requests"].values() if row["query"] == "history")
        history["accepted_content_sha256"] = "0" * 64
        write_json(root / "data/state/preparation/latest.json", state)
    else:
        path = preparation.state_dir / "pool.json"
        envelope = read_json(path)
        if damage == "pool_identity":
            envelope["pool"]["identity"]["provider"] = "other-source"
        else:
            key = next(
                key
                for key, value in envelope["pool"]["dependencies"].items()
                if value["identity"]["query"] == "session_bars"
            )
            del envelope["pool"]["dependencies"][key]
            envelope["pool"]["source_requests"].remove(key)
        envelope["sha256"] = digest(envelope["pool"])
        write_json(path, envelope)
    with pytest.raises(CorruptData):
        import_preparation(root, as_of=AS_OF)
    assert not MarketRepository(root).path.exists()
