"""Native readiness guards and explicit installer isolation; never real launchd writes."""

import argparse
import fcntl
import json
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from china_stock_sim.desktop import indicator, indicator_cli

SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 20, 0, tzinfo=SHANGHAI)


def payload(now=NOW, state="READY"):
    return {
        "schema_version": 1,
        "state": state,
        "label": "可关机" if state == "READY" else "尚未就绪",
        "business_session": "2026-11-04",
        "target_session": "2026-11-05",
        "sampled_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "accounts": [
            {
                "account_id": name,
                "ready": state == "READY",
                "summary": "日结与计划已保存",
                "reasons": [],
            }
            for name in ("S1", "S1-LT", "LLM")
        ],
        "reasons": [],
        "optional_work": ["备份仍进行中"],
    }


class Runner:
    def __init__(self):
        self.commands, self.loaded, self.disabled, self.failure = [], False, False, None

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        rc, out = 0, ""
        if command[:2] == ["/usr/bin/xcrun", "swiftc"]:
            if "--version" in command:
                out = "Swift test compiler"
            else:
                path = Path(command[command.index("-o") + 1])
                path.write_bytes(b"controlled-test-binary")
                path.chmod(0o755)
        elif command[0] == "/bin/launchctl":
            operation = command[1]
            if operation == "print":
                rc, out = (0, "state = running\n pid = 4242\n") if self.loaded else (113, "")
            elif operation == "print-disabled":
                identifier = next(
                    (
                        c[-1].split("/")[-1]
                        for c in reversed(self.commands)
                        if c[0:2] == ["/bin/launchctl", "print"]
                    ),
                    "",
                )
                out = '"' + identifier + '" => ' + ("true" if self.disabled else "false")
            elif operation == "enable":
                self.disabled = False
            elif operation == "bootstrap":
                if self.failure == "bootstrap":
                    rc = 5
                else:
                    self.loaded = True
            elif operation == "bootout":
                if self.failure == "bootout":
                    rc = 5
                else:
                    self.loaded = False
        if command[0] == "/usr/bin/codesign" and self.failure == "signature":
            rc = 1
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "隔离 项目"
    source = root / "deploy/macos"
    source.mkdir(parents=True)
    for name in indicator.SOURCES:
        shutil.copyfile(indicator.PROJECT / "deploy/macos" / name, source / name)
    interpreter = root / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(indicator.platform, "system", lambda: "Darwin")
    return root


def options(root, runner):
    return {"runner": runner, "agent_directory": root / "isolated-agents", "test_mode": True}


def test_status_is_read_only_and_cannot_build_install_or_run_business(project):
    runner = Runner()
    before = {str(p.relative_to(project)) for p in project.rglob("*")}
    result = indicator.status(project, **options(project, runner))
    assert result["state"] == "BUILD_REQUIRED"
    assert before == {str(p.relative_to(project)) for p in project.rglob("*")}
    assert all(c[1] in {"print", "print-disabled"} for c in runner.commands)


def test_build_has_one_production_bundle_no_test_input_or_service_side_effect(project):
    runner = Runner()
    result = indicator.build(project, runner=runner)
    app = Path(result["app_path"])
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["LSUIElement"] is True and "SCSFixturePath" not in info
    assert info["SCSPythonPath"] == str(project / ".venv/bin/python")
    assert result["manifest"]["mode"] == "PRODUCTION_READ_ONLY"
    assert not any(c[0] == "/bin/launchctl" for c in runner.commands)
    assert indicator.status(project, **options(project, runner))["artifact_matches"]
    assert not list((project / "deploy/macos").glob(".indicator-build-*"))


def test_install_idempotence_enable_and_uninstall_touch_only_owned_indicator(project):
    runner = Runner()
    indicator.build(project, runner=runner)
    directory = project / "isolated-agents"
    directory.mkdir()
    for name in ("workflow", "homepage", "upload"):
        (directory / (name + ".plist")).write_bytes(name.encode())
    first = indicator.install(project, **options(project, runner))
    assert first["state"] == "INSTALLED" and first["loaded"] and first["enabled"]
    assert indicator.install(project, **options(project, runner))["state"] == "ALREADY_INSTALLED"
    assert sum(c[1] == "bootstrap" for c in runner.commands if c[0] == "/bin/launchctl") == 1
    runner.disabled = True
    assert indicator.install(project, **options(project, runner))["enabled"]
    result = indicator.uninstall(project, **options(project, runner))
    assert result["state"] == "UNINSTALLED" and not result["loaded"]
    assert all(
        (directory / (name + ".plist")).read_text() == name
        for name in ("workflow", "homepage", "upload")
    )
    assert Path(first["app_path"]).exists()


def test_service_contract_is_named_single_display_process_not_business_schedule(project):
    value = indicator.render(project)["plist"]
    assert value["RunAtLoad"] and value["KeepAlive"] == {"SuccessfulExit": False}
    assert value["LimitLoadToSessionType"] == "Aqua"
    assert value["AssociatedBundleIdentifiers"] == [value["Label"]]
    assert len(value["ProgramArguments"]) == 1 and value["ProgramArguments"][0].endswith(
        indicator.NAME
    )
    assert "StartInterval" not in value and "StartCalendarInterval" not in value
    assert "python" not in value["ProgramArguments"][0]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_unknown_plist_is_not_overwritten_or_booted_out(project, operation):
    runner = Runner()
    indicator.build(project, runner=runner)
    path = project / "isolated-agents" / (indicator.label(project) + ".plist")
    path.parent.mkdir()
    path.write_bytes(
        plistlib.dumps({"Label": indicator.label(project), "ProgramArguments": ["/other/app"]})
    )
    old = path.read_bytes()
    with pytest.raises(indicator.IndicatorError, match="UNKNOWN_PLIST"):
        getattr(indicator, operation)(project, **options(project, runner))
    assert path.read_bytes() == old
    assert not any(
        c[1] in {"bootstrap", "bootout"} for c in runner.commands if c[0] == "/bin/launchctl"
    )


def test_unknown_loaded_label_without_our_plist_cannot_be_claimed(project):
    runner = Runner()
    indicator.build(project, runner=runner)
    runner.loaded = True
    with pytest.raises(indicator.IndicatorError, match="UNKNOWN_LOADED_SERVICE"):
        indicator.install(project, **options(project, runner))


def test_symlink_and_external_test_directory_are_rejected(project, tmp_path):
    with pytest.raises(indicator.IndicatorError, match="UNSAFE_AGENT_DIRECTORY"):
        indicator.status(
            project, runner=Runner(), agent_directory=tmp_path / "elsewhere", test_mode=True
        )
    (project / "isolated-agents").symlink_to(tmp_path)
    with pytest.raises(indicator.IndicatorError, match="UNSAFE_SYMLINK"):
        indicator.status(project, **options(project, Runner()))


def test_running_instance_blocks_build_and_failed_signature_preserves_existing_app(project):
    runner = Runner()
    original = indicator.build(project, runner=runner)
    executable = Path(original["binary_path"])
    with (project / "data/state/shutdown-indicator/instance.lock").open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(indicator.IndicatorError, match="INDICATOR_RUNNING"):
            indicator.build(project, runner=runner)
    runner.failure = "signature"
    with pytest.raises(indicator.IndicatorError, match="APP_SIGNATURE_FAILED"):
        indicator.build(project, runner=runner)
    assert executable.read_bytes() == b"controlled-test-binary"


def test_install_failure_rolls_back_only_new_owned_plist(project):
    runner = Runner()
    indicator.build(project, runner=runner)
    runner.failure = "bootstrap"
    result = indicator.install(project, **options(project, runner))
    assert result["state"] == "INSTALL_FAILED"
    assert not Path(result["plist_path"]).exists()


def test_fixture_bundle_requires_isolated_marker_and_cannot_replace_production(project):
    fixture = project / "fixture.json"
    fixture.write_text("{}")
    runner = Runner()
    with pytest.raises(indicator.IndicatorError, match="TEST_ROOT_REQUIRED"):
        indicator.build(project, runner=runner, fixture_path=fixture, test_mode=True)
    (project / ".shutdown-indicator-test-root").write_text("isolated native UI fixture\n")
    result = indicator.build(project, runner=runner, fixture_path=fixture, test_mode=True)
    assert (
        result["manifest"]["bundle_id"].endswith(".test")
        and result["manifest"]["mode"] == "TEST_FIXTURE"
    )
    assert "-测试.app" in result["app_path"]
    with pytest.raises(indicator.IndicatorError, match="BUILD_REQUIRED"):
        indicator.install(project, **options(project, runner))


def test_cli_has_only_explicit_four_operations_without_fixture_or_now_flags():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    indicator_cli.register_commands(commands)
    for command in indicator_cli.COMMANDS:
        assert parser.parse_args([command]).command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["shutdown-indicator-install", "--fixture", "fake.json"])


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    if platform.system() != "Darwin" or not shutil.which("xcrun"):
        pytest.skip("Actual macOS Swift runtime required")
    folder = tmp_path_factory.mktemp("native-indicator")
    binary = folder / "indicator-selftest"
    command = [
        "/usr/bin/xcrun",
        "swiftc",
        "-swift-version",
        "5",
        "-parse-as-library",
        "-D",
        "INDICATOR_SELFTEST",
        "-framework",
        "AppKit",
        *[str(indicator.PROJECT / "deploy/macos" / name) for name in indicator.SOURCES],
        "-o",
        str(binary),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return binary


def validate(native, tmp_path, value, now=NOW):
    file = tmp_path / "status.json"
    file.write_text(json.dumps(value))
    result = subprocess.run(
        [str(native), "--validate", str(file), str(now.timestamp())],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, json.loads(result.stdout)


@pytest.mark.parametrize("state", ["READY", "WORKING", "WAITING", "BLOCKED", "UNKNOWN"])
def test_native_accepts_valid_typed_business_states(native, tmp_path, state):
    code, value = validate(native, tmp_path, payload(state=state))
    assert code == 0 and value["state"] == state


@pytest.mark.parametrize(
    "defect,expected",
    [
        ("schema", "INVALID_SCHEMA"),
        ("state", "INVALID_SCHEMA"),
        ("future", "INVALID_CLOCK"),
        ("naive", "INVALID_CLOCK"),
        ("long", "INVALID_CLOCK"),
        ("expired", "EXPIRED"),
        ("missing", "INCOMPLETE_READY"),
        ("duplicate", "INVALID_ACCOUNTS"),
        ("unready", "INCOMPLETE_READY"),
        ("date", "INCOMPLETE_READY"),
        ("reverse", "INCOMPLETE_READY"),
        ("future_d", "INVALID_TARGET_DAY"),
    ],
)
def test_native_does_not_trust_green_label_with_invalid_evidence(
    native, tmp_path, defect, expected
):
    value = payload()
    if defect == "schema":
        value["schema_version"] = 2
    elif defect == "state":
        value["state"] = "FAKE_GREEN"
    elif defect == "future":
        value["sampled_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif defect == "naive":
        value["sampled_at"] = NOW.replace(tzinfo=None).isoformat()
    elif defect == "long":
        value["expires_at"] = (NOW + timedelta(minutes=30)).isoformat()
    elif defect == "expired":
        value["expires_at"] = NOW.isoformat()
    elif defect == "missing":
        value["accounts"].pop()
    elif defect == "duplicate":
        value["accounts"][1]["account_id"] = "S1"
    elif defect == "unready":
        value["accounts"][0]["ready"] = False
    elif defect == "date":
        value["business_session"] = None
    elif defect == "reverse":
        value["target_session"] = value["business_session"]
    elif defect == "future_d":
        value["business_session"], value["target_session"] = "2026-11-05", "2026-11-06"
    code, result = validate(native, tmp_path, value)
    assert code == 2 and result["state"] == "UNKNOWN" and result["issue"] == expected


@pytest.mark.parametrize("clock", [(8, 59, 40), (18, 9, 40), (23, 59, 40)])
def test_native_green_expiry_must_not_cross_daily_boundaries(native, tmp_path, clock):
    now = NOW.replace(hour=clock[0], minute=clock[1], second=clock[2])
    code, result = validate(native, tmp_path, payload(now), now)
    assert code == 2 and result["issue"] == "CROSSES_DAY_BOUNDARY"


def test_native_weekend_has_no_trading_cutoff_but_still_expires_at_midnight(native, tmp_path):
    for hour, minute, expected in [(8, 59, 0), (18, 9, 0), (23, 59, 2)]:
        now = NOW.replace(day=7, hour=hour, minute=minute, second=40)
        value = payload(now)
        value["business_session"], value["target_session"] = "2026-11-06", "2026-11-09"
        code, result = validate(native, tmp_path, value, now)
        assert code == expected
        if expected == 0:
            assert result["state"] == "READY"
        else:
            assert result["issue"] == "CROSSES_DAY_BOUNDARY"


def test_native_daytime_label_is_gray_unknown_and_wake_gate_rejects_stale_callbacks(
    native, tmp_path
):
    value = payload(state="UNKNOWN")
    value["label"] = "待晚间"
    assert validate(native, tmp_path, value)[1]["label"] == "待晚间"
    value["label"] = "可关机"
    assert validate(native, tmp_path, value)[1]["label"] == "待核查"
    result = subprocess.run([str(native), "--gate"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and all(json.loads(result.stdout).values())


def probe(native, tmp_path, body):
    script = tmp_path / "fake-probe"
    script.write_text("#!" + sys.executable + "\n" + body)
    script.chmod(0o755)
    started = time.monotonic()
    result = subprocess.run(
        [str(native), "--probe", str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=12,
    )
    return result.returncode, json.loads(result.stdout), time.monotonic() - started


def test_native_probe_accepts_stdout_without_using_shell_or_ignoring_exit(native, tmp_path):
    value = payload(datetime.now(SHANGHAI), state="WAITING")
    code, result, _ = probe(native, tmp_path, "print(" + repr(json.dumps(value)) + ")\n")
    assert code == 0 and result["state"] == "WAITING"


@pytest.mark.parametrize(
    "body,expected",
    [
        ("import time\ntime.sleep(30)\n", "TIMEOUT"),
        ('print("x" * (256*1024+1))\n', "OUTPUT_LIMIT"),
        ('print("not-json")\n', "INVALID_JSON"),
    ],
)
def test_native_probe_timeout_size_and_bad_json_fail_non_green(native, tmp_path, body, expected):
    code, value, seconds = probe(native, tmp_path, body)
    assert code == 2 and value["state"] == "UNKNOWN" and value["issue"] == expected
    assert seconds < 11


def test_native_failure_keeps_safe_stack_but_excludes_exception_content(native, tmp_path):
    text = 'Traceback (most recent call last):\n  File "/project/safe.py", line 4, in probe\n    secret="DO_NOT_LOG"\nRuntimeError: DO_NOT_LOG\nRuntimeError\n'
    body = "import sys\nsys.stderr.write(" + repr(text) + ')\nprint("{}")\nsys.exit(2)\n'
    code, value, _ = probe(native, tmp_path, body)
    assert code == 2 and value["issue"] == "PROBE_FAILED"
    assert any("safe.py" in line for line in value["diagnostic_stack"])
    assert "RuntimeError" in value["diagnostic_stack"] and "DO_NOT_LOG" not in json.dumps(value)


def test_native_diagnostics_are_bounded_and_unchanged_state_only_replaces_latest(native, tmp_path):
    result = subprocess.run(
        [str(native), "--diagnostics", str(tmp_path)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0 and json.loads(result.stdout)["duplicate_event_not_appended"]
    directory = tmp_path / "logs/shutdown-indicator"
    assert all(
        (directory / name).stat().st_size <= 1024 * 1024
        for name in ("events.jsonl", "events.previous.jsonl")
    )
    assert json.loads((directory / "latest.json").read_text())["latest_only"] is True
