import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCANNER_PATH = Path(__file__).resolve().parents[1] / "scripts/check_secrets.py"
SPEC = importlib.util.spec_from_file_location("check_secrets", SCANNER_PATH)
SCANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCANNER)
FAKE_KEY = "sk-" + "ws-" + "X" * 40 + "." + "Y" * 30
ZERO = "0" * 40


def run_git(repo, *args):
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Secret scanner test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Secret scanner test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, "Test fixture Git command failed"
    return result.stdout.strip()


def scanner(repo, *args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCANNER_PATH), *args], cwd=repo, input=stdin,
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "git_fixture"
    repo.mkdir()
    run_git(repo, "init", "-q")
    return repo


@pytest.mark.parametrize("key", [FAKE_KEY, "sk-" + "A" * 40])
def test_content_detector_finds_keys_without_returning_content(key):
    result = SCANNER.content_findings(("API_KEY=" + key).encode())
    assert result == {"API_KEY_PATTERN"}
    assert key not in repr(result)


def test_examples_can_be_committed_but_real_key_in_example_is_blocked(git_repo):
    example = git_repo / ".env.example"
    example.write_text("API_KEY=sk-REPLACE_WITH_YOUR_API_KEY\n")
    run_git(git_repo, "add", ".env.example")
    assert scanner(git_repo, "--staged").returncode == 0
    example.write_text("API_KEY=" + FAKE_KEY + "\n")
    run_git(git_repo, "add", ".env.example")
    result = scanner(git_repo, "--staged")
    assert result.returncode == 1
    assert "API_KEY_PATTERN" in result.stderr
    assert FAKE_KEY not in result.stderr + result.stdout


@pytest.mark.parametrize("name", [".env", ".env.local", "nested/.env.production"])
def test_local_env_filename_is_blocked_even_without_a_recognized_key(git_repo, name):
    target = git_repo / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("LOCAL_ONLY=yes\n")
    run_git(git_repo, "add", name)
    result = scanner(git_repo, "--staged")
    assert result.returncode == 1
    assert "LOCAL_ENV_FILE" in result.stderr


def test_untracked_local_env_is_not_read_and_staged_content_is_used(git_repo):
    (git_repo / ".env").write_text("API_KEY=" + FAKE_KEY)
    document = git_repo / "README.txt"
    document.write_text("safe staged content")
    run_git(git_repo, "add", "README.txt")
    document.write_text("uncommitted content " + FAKE_KEY)
    assert scanner(git_repo, "--staged").returncode == 0
    run_git(git_repo, "add", "README.txt")
    result = scanner(git_repo, "--staged")
    assert result.returncode == 1
    assert FAKE_KEY not in result.stdout + result.stderr


def test_new_branch_push_checks_deleted_secrets_in_earlier_commits(git_repo):
    secret = git_repo / "mistake.txt"
    secret.write_text(FAKE_KEY)
    run_git(git_repo, "add", "mistake.txt")
    run_git(git_repo, "commit", "-qm", "temporary test fixture")
    run_git(git_repo, "rm", "-q", "mistake.txt")
    run_git(git_repo, "commit", "-qm", "remove test fixture")
    tip = run_git(git_repo, "rev-parse", "HEAD")
    result = scanner(
        git_repo, "--pre-push", stdin=f"refs/heads/main {tip} refs/heads/main {ZERO}\n",
    )
    assert result.returncode == 1
    assert "mistake.txt" in result.stderr
    assert FAKE_KEY not in result.stdout + result.stderr
    assert scanner(git_repo, "--history", "HEAD").returncode == 1


def test_existing_branch_push_checks_new_commits_and_deletion_is_allowed(git_repo):
    (git_repo / "readme.txt").write_text("safe")
    run_git(git_repo, "add", "readme.txt")
    run_git(git_repo, "commit", "-qm", "safe base")
    base = run_git(git_repo, "rev-parse", "HEAD")
    (git_repo / "configuration.txt").write_text(FAKE_KEY)
    run_git(git_repo, "add", "configuration.txt")
    run_git(git_repo, "commit", "-qm", "unsafe fixture")
    tip = run_git(git_repo, "rev-parse", "HEAD")
    result = scanner(
        git_repo, "--pre-push", stdin=f"refs/heads/main {tip} refs/heads/main {base}\n",
    )
    assert result.returncode == 1
    deletion = scanner(
        git_repo, "--pre-push", stdin=f"(delete) {ZERO} refs/heads/main {tip}\n",
    )
    assert deletion.returncode == 0


def test_unavailable_remote_commit_falls_back_to_complete_history(git_repo):
    (git_repo / "configuration.txt").write_text(FAKE_KEY)
    run_git(git_repo, "add", "configuration.txt")
    run_git(git_repo, "commit", "-qm", "unsafe fixture")
    tip = run_git(git_repo, "rev-parse", "HEAD")
    result = scanner(
        git_repo, "--pre-push",
        stdin=f"refs/heads/main {tip} refs/heads/main {'f' * 40}\n",
    )
    assert result.returncode == 1
    assert FAKE_KEY not in result.stdout + result.stderr


def test_malformed_push_input_fails_closed_without_echoing_input(git_repo):
    result = scanner(git_repo, "--pre-push", stdin=FAKE_KEY)
    assert result.returncode == 2
    assert FAKE_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("algorithm", ["", "RSA ", "DSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
def test_supported_private_key_headers_are_detected_without_exposing_payload(algorithm):
    value = ("-----BEGIN " + algorithm + "PRIVATE KEY-----\nPRIVATE_PAYLOAD").encode()
    assert SCANNER.content_findings(value) == {"PRIVATE_KEY"}


def test_worktree_checks_untracked_content_without_staging(git_repo):
    (git_repo / "new.txt").write_text(FAKE_KEY)
    before = run_git(git_repo, "ls-files", "--stage")
    result = scanner(git_repo, "--worktree")
    assert result.returncode == 1
    assert "new.txt" in result.stderr and "API_KEY_PATTERN" in result.stderr
    assert FAKE_KEY not in result.stdout + result.stderr
    assert run_git(git_repo, "ls-files", "--stage") == before == ""
    # Empty-index success is deliberately not confused with prospective-file success.
    staged = scanner(git_repo, "--staged")
    assert staged.returncode == 0 and json.loads(staged.stdout)["unique_blobs_checked"] == 0


def test_worktree_uses_current_tracked_bytes_and_scans_entire_repo_from_subdirectory(git_repo):
    document = git_repo / "tracked.txt"
    document.write_text("safe staged value")
    run_git(git_repo, "add", "tracked.txt")
    document.write_text(FAKE_KEY)
    nested = git_repo / "nested"
    nested.mkdir()
    result = scanner(nested, "--worktree")
    assert result.returncode == 1 and "tracked.txt" in result.stderr
    assert scanner(git_repo, "--staged").returncode == 0
    assert FAKE_KEY not in result.stdout + result.stderr


def test_worktree_ignored_private_file_is_never_read(git_repo):
    (git_repo / ".gitignore").write_text(".env\nignored.key\n")
    secret = git_repo / ".env"
    secret.write_text(FAKE_KEY)
    secret.chmod(0)
    try:
        result = scanner(git_repo, "--worktree")
        assert result.returncode == 0
        assert json.loads(result.stdout)["files_checked"] == 1
    finally:
        secret.chmod(0o600)


@pytest.mark.parametrize("path", [
    "keys/client.pem", "keys/client.key", "client.p12", "client.pfx", "store.jks",
    "store.keystore", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pypirc",
    "secrets/credential.txt", ".ssh/config", "configs/publishing.toml", "configs/local.toml",
    "configs/dev.local.toml", "configs/dev-local.toml", "configs/local.json",
    "data/state/ledger.sqlite3", "reports/current.html", "logs/run.json",
    "deploy/web/nginx.conf", "deploy/launchd/current.plist",
    "src/china_stock_sim/publishing/sync.py", "configs/publishing.example.toml",
    "configs/publishing-staging.toml", "deploy/web/nginx.example.conf",
    "deploy/launchd/bin/A股模拟-云端上传", "tests/test_static_publishing.py",
    "tests/test_publishing_schedule.py", "tests/test_trading_cloud_interaction.py",
    "tests/test_trading_history_cloud.py",
    "deploy/macos/app.app/Contents/Info.plist",
    "deploy/macos/app.build.json",
])
def test_force_added_private_or_host_artifact_is_blocked_even_with_benign_bytes(git_repo, path):
    (git_repo / ".gitignore").write_text(path + "\n")
    target = git_repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("benign fixture")
    run_git(git_repo, "add", "-f", path)
    for mode in ("--staged", "--worktree"):
        result = scanner(git_repo, mode)
        assert result.returncode == 1
        assert path in {finding["path"] for finding in json.loads(result.stderr)["findings"]}
        assert "benign fixture" not in result.stdout + result.stderr


def test_private_path_rejected_without_loading_blob_contents(monkeypatch):
    def forbidden_read(*args, **kwargs):
        pytest.fail("Private or large runtime blob must not be loaded")

    monkeypatch.setattr(SCANNER, "git", forbidden_read)
    findings, checked = SCANNER.scan([("INDEX", [("data/state/ledger.sqlite3", "a" * 40)])])
    assert checked == 0 and findings[0]["types"] == ["LOCAL_RUNTIME_DATA"]


def test_gitkeep_and_safe_templates_remain_scanned(git_repo):
    for name in ["data/.gitkeep", "reports/.gitkeep", "logs/.gitkeep",
                 "configs/models.toml", ".env.example"]:
        path = git_repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    assert scanner(git_repo, "--worktree").returncode == 0
    (git_repo / "data/.gitkeep").write_text(FAKE_KEY)
    result = scanner(git_repo, "--worktree")
    assert result.returncode == 1 and "API_KEY_PATTERN" in result.stderr
    assert FAKE_KEY not in result.stdout + result.stderr


def test_worktree_reads_symlink_text_without_reading_external_target(git_repo, tmp_path):
    outside = tmp_path / "private-outside.txt"
    outside.write_text(FAKE_KEY)
    (git_repo / "linked.txt").symlink_to(outside)
    result = scanner(git_repo, "--worktree")
    assert result.returncode == 0 and json.loads(result.stdout)["files_checked"] == 1
    # The link itself is Git content, so a credential accidentally in that text is caught.
    (git_repo / "unsafe-link").symlink_to(FAKE_KEY)
    result = scanner(git_repo, "--worktree")
    assert result.returncode == 1 and "unsafe-link" in result.stderr
    assert FAKE_KEY not in result.stdout + result.stderr


def test_worktree_read_failure_fails_closed_without_exception_contents(monkeypatch, capsys):
    def failed_read():
        raise OSError(FAKE_KEY)

    monkeypatch.setattr(SCANNER, "scan_worktree", failed_read)
    assert SCANNER.main(["--worktree"]) == 2
    captured = capsys.readouterr()
    assert "SCAN_ERROR" in captured.err
    assert FAKE_KEY not in captured.out + captured.err


def test_worktree_tracked_deletion_has_no_prospective_bytes_but_index_still_checked(git_repo):
    path = git_repo / "tracked.txt"
    path.write_text(FAKE_KEY)
    run_git(git_repo, "add", "tracked.txt")
    path.unlink()
    result = scanner(git_repo, "--worktree")
    assert result.returncode == 0 and json.loads(result.stdout)["files_checked"] == 0
    assert scanner(git_repo, "--staged").returncode == 1
