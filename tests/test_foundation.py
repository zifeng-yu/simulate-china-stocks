import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path

import pytest

from china_stock_sim.cli import main
from china_stock_sim.config import load_settings, local_path
from china_stock_sim.ledger.store import initialize, read_snapshot, readonly_connection
from china_stock_sim.reporting.initial import write_initial_report
from china_stock_sim.scheduler.workspace import ensure_directories, writer_lock

REPOSITORY = Path(__file__).resolve().parents[1]


def file_contents(root):
    """Record only persisted files, without relying on access timestamps."""
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_config_is_isolated_and_initializes_independent_accounts(settings, project):
    assert settings.root == project.resolve()
    assert settings.database == project / "data/state/ledger.sqlite3"
    assert settings.report == project / "reports/forward_current/index.html"
    assert str(settings.timezone) == "Asia/Shanghai"
    assert settings.default_account == "LLM"
    assert {(a.id, a.strategy, a.initial_cash_fen) for a in settings.accounts} == {
        ("S1", "S1", 5_000_000),
        ("S1-LT", "S1-LT", 5_000_000),
        ("LLM", "LLM-Daily", 5_000_000),
    }
    assert not settings.database.exists()


def test_initialize_twice_preserves_opening_cash_and_timestamps(settings):
    first = initialize(settings)
    original_snapshot = read_snapshot(settings)
    original_bytes = settings.database.read_bytes()
    second = initialize(settings)
    assert first["created_accounts"] == 3
    assert second["created_accounts"] == 0
    assert read_snapshot(settings) == original_snapshot
    assert settings.database.read_bytes() == original_bytes
    assert original_snapshot["last_settled_session"] is None
    assert original_snapshot["financial_observation_days"] == 0
    for row in original_snapshot["accounts"]:
        assert row["cash_fen"] == 5_000_000
        assert row["opening_entries"] == 1
        assert row["created_at"].endswith("+08:00")
    with closing(readonly_connection(settings.database)) as connection:
        rows = connection.execute("SELECT * FROM cash_entries").fetchall()
        assert len(rows) == 3
        assert {r["kind"] for r in rows} == {"INITIAL_CAPITAL"}
        times = {r["account_id"]: r["recorded_at"] for r in rows}
        assert all(r["created_at"] == times[r["account_id"]] for r in original_snapshot["accounts"])


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE accounts SET initial_cash_fen=123456 WHERE account_id='S1'",
        "UPDATE accounts SET strategy_id='' WHERE account_id='S1'",
        "UPDATE cash_entries SET amount_fen=123456 WHERE account_id='S1'",
        "DELETE FROM cash_entries WHERE account_id='S1'",
        "INSERT INTO accounts VALUES ('EXTRA', 'S1', 'CNY', 5000000, 'unchanged')",
        "PRAGMA user_version=99",
    ],
)
def test_inconsistent_existing_ledger_is_rejected_without_overwrite(settings, statement):
    initialize(settings)
    with closing(sqlite3.connect(settings.database)) as connection:
        connection.execute(statement)
        connection.commit()
    before = settings.database.read_bytes()
    with pytest.raises(ValueError):
        initialize(settings)
    assert settings.database.read_bytes() == before
    with pytest.raises(ValueError):
        read_snapshot(settings)
    assert settings.database.read_bytes() == before


def test_unknown_database_is_not_adopted(settings):
    settings.database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(settings.database)) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('preserve me')")
        connection.commit()
    before = settings.database.read_bytes()
    with pytest.raises(ValueError, match="未识别"):
        initialize(settings)
    assert settings.database.read_bytes() == before


def test_failed_initialization_rolls_back_new_accounts(settings):
    initialize(settings)
    with closing(sqlite3.connect(settings.database)) as connection:
        connection.execute("DELETE FROM cash_entries WHERE account_id='S1'")
        connection.execute("DELETE FROM accounts WHERE account_id='S1'")
        connection.execute("UPDATE cash_entries SET amount_fen=123 WHERE account_id='LLM'")
        connection.commit()
    before = settings.database.read_bytes()
    with pytest.raises(ValueError):
        initialize(settings)
    assert settings.database.read_bytes() == before
    with closing(readonly_connection(settings.database)) as connection:
        assert connection.execute("SELECT 1 FROM accounts WHERE account_id='S1'").fetchone() is None


def test_read_snapshot_cannot_write_or_create_a_missing_database(settings, project):
    before = file_contents(project)
    with pytest.raises(FileNotFoundError):
        read_snapshot(settings)
    assert file_contents(project) == before
    assert not settings.database.parent.exists()
    initialize(settings)
    before = file_contents(project)
    assert read_snapshot(settings)["state"] == "INITIALIZED_NOT_RUNNING"
    with closing(readonly_connection(settings.database)) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM cash_entries")
    assert file_contents(project) == before


def test_doctor_missing_ledger_is_read_only(project, capsys):
    before = file_contents(project)
    assert main(["--root", str(project), "doctor"]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["foundation_ready"] is False
    assert response["forward_simulation_ready"] is False
    assert response["checks"]["ledger"]["state"] == "NOT_READY"
    assert file_contents(project) == before
    assert not (project / "data").exists()


def test_doctor_and_report_commands_do_not_pretend_to_trade(project, settings, capsys):
    assert main(["--root", str(project), "init"]) == 0
    init_result = json.loads(capsys.readouterr().out)
    assert init_result["created_accounts"] == 3
    before = settings.database.read_bytes()
    # A temporary project deliberately has no matching virtual environment.
    assert main(["--root", str(project), "doctor"]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["checks"]["ledger"]["state"] == "INITIALIZED_NOT_RUNNING"
    assert response["checks"]["initial_report"]["ready"] is True
    assert response["checks"]["using_project_venv"] is False
    assert response["forward_simulation_ready"] is False
    assert response["capabilities"]["order_matching_and_fees"] == "IMPLEMENTED"
    assert response["capabilities"]["plan_validation"] == "IMPLEMENTED"
    assert response["capabilities"]["background_scheduler"] == "IMPLEMENTED"
    assert main(["--root", str(project), "report"]) == 0
    capsys.readouterr()
    assert settings.database.read_bytes() == before


def test_doctor_reports_missing_page_without_generating_it(project, settings, capsys):
    initialize(settings)
    before = file_contents(project)
    assert main(["--root", str(project), "doctor"]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["checks"]["ledger"]["state"] == "INITIALIZED_NOT_RUNNING"
    assert response["checks"]["initial_report"] == {
        "path": str(settings.report),
        "ready": False,
    }
    assert response["foundation_ready"] is False
    assert file_contents(project) == before
    assert not settings.report.exists()


def test_report_without_init_does_not_create_a_database(project, capsys):
    before = file_contents(project)
    assert main(["--root", str(project), "report"]) == 2
    response = json.loads(capsys.readouterr().err)
    assert response["state"] == "ERROR"
    assert file_contents(project) == before


def test_cli_locates_checkout_from_outside_working_directory(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("CHINA_STOCK_SIM_")
    }
    env["PYTHONPATH"] = str(REPOSITORY / "src")
    # Preserve real checkout discovery, but isolate readiness from live enablement.
    # The real checkout can have a private .env. The path test must never read it.
    code = (
        "import china_stock_sim.cli as cli\n"
        "from copy import deepcopy\n"
        "from dataclasses import replace\n"
        "load_settings = cli.load_settings\n"
        "def disabled_settings(root):\n"
        "    settings = load_settings(root)\n"
        "    settings = replace(settings, raw=deepcopy(settings.raw))\n"
        "    for name in ('schedule', 'market', 'execution'):\n"
        "        settings.raw[name]['enabled'] = False\n"
        "    for strategy in settings.raw['strategies'].values():\n"
        "        strategy['enabled'] = False\n"
        "    return settings\n"
        "cli.load_settings = disabled_settings\n"
        "cli.model_status=lambda *a: {'configured': False, 'network_tested': False}\n"
        "raise SystemExit(cli.main(['doctor']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    # Depends only on the interpreter/checkout readiness, never the foreign cwd.
    assert result.returncode in (0, 2), result.stderr
    response = json.loads(result.stdout)
    assert Path(response["checks"]["project"]) == REPOSITORY
    assert response["forward_simulation_ready"] is False
    assert not list(tmp_path.iterdir())


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_account_json = False
        self.account_json = ""
        self.selected = []
        self.sources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "accounts":
            self.in_account_json = True
        if tag == "option" and "selected" in attrs:
            self.selected.append(attrs["value"])
        if "src" in attrs:
            self.sources.append(attrs["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_account_json = False

    def handle_data(self, data):
        if self.in_account_json:
            self.account_json += data


def test_report_embeds_actual_independent_accounts_without_fake_daily_results(settings):
    initialize(settings)
    snapshot = read_snapshot(settings)
    before = settings.database.read_bytes()
    write_initial_report(settings, snapshot)
    html = settings.report.read_text()
    parsed = ReportParser()
    parsed.feed(html)
    assert json.loads(parsed.account_json) == snapshot["accounts"]
    assert parsed.selected == ["LLM"]
    assert parsed.sources == []
    assert "最后日结交易日：尚无" in html
    assert "金融观察日：0" in html
    assert "当日盈亏：尚未日结" in html
    assert "INITIALIZED_NOT_RUNNING" in html
    assert "不合并总资产" in html
    assert "150,000" not in html
    assert "__OPTIONS__" not in html
    assert settings.database.read_bytes() == before
    assert not list(settings.report.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('mode = "paper"', 'mode = "live"'),
        ('timezone = "Asia/Shanghai"', 'timezone = "UTC"'),
        ("schema_version = 1", "schema_version = 2"),
        ("schema_version = 1", "schema_version = true"),
        ("initial_cash_fen = 5000000", "initial_cash_fen = 0"),
        ("initial_cash_fen = 5000000", "initial_cash_fen = true"),
        ('id = "LLM"', 'id = "S1"'),
        ('strategy = "LLM-Daily"', 'strategy = ""'),
        ("aggregate_accounts = false", "aggregate_accounts = true"),
        ('default_account = "LLM"', 'default_account = ""'),
        ("enabled = false", "enabled = 1"),
        ('database = "data/state/ledger.sqlite3"', 'database = "../outside.sqlite3"'),
        ('database = "data/state/ledger.sqlite3"', 'database = "data/ledger.sqlite3"'),
        ('report = "reports/forward_current/index.html"', 'report = "data/index.html"'),
    ],
)
def test_invalid_config_is_rejected_before_any_runtime_write(project, old, new):
    path = project / "configs/default.toml"
    text = path.read_text()
    if old == "enabled = false":
        text = text.replace("enabled = true", "enabled = false")
    assert old in text
    path.write_text(text.replace(old, new, 1))
    before = file_contents(project)
    with pytest.raises(ValueError):
        load_settings(project)
    assert file_contents(project) == before
    assert not (project / "data").exists()


@pytest.mark.parametrize("relative", ["/tmp/outside.sqlite3", "../escape", "data/../escape", ""])
def test_runtime_paths_cannot_escape_project(project, relative):
    with pytest.raises(ValueError):
        local_path(project, relative)


def test_runtime_path_symlink_cannot_escape_project(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        load_settings(project)
    assert not list(outside.iterdir())


def test_concurrent_init_is_rejected_then_succeeds_after_lock_release(settings, project, capsys):
    ensure_directories(settings)
    with writer_lock(settings):
        assert main(["--root", str(project), "init"]) == 2
        error = json.loads(capsys.readouterr().err)
        assert "已有进程" in error["reason"]
        assert not settings.database.exists()
    assert main(["--root", str(project), "init"]) == 0
    capsys.readouterr()
    assert main(["--root", str(project), "init"]) == 0
    capsys.readouterr()
    snapshot = read_snapshot(settings)
    assert len(snapshot["accounts"]) == 3
    assert all(row["opening_entries"] == 1 for row in snapshot["accounts"])
