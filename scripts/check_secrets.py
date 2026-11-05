#!/usr/bin/env python3
"""Scan Git content without printing credentials or reading untracked local .env.

This is a local safeguard, not a replacement for GitHub server-side push protection.
Only paths, revision IDs and finding types are included in diagnostics.
"""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

TOKEN_PATTERN = re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_.-]{20,}")
PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
PLACEHOLDERS = {b"sk-REPLACE_WITH_YOUR_API_KEY", b"sk-YOUR_API_KEY_HERE"}
OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class ScanError(Exception):
    """A safe error message without Git stderr or file content."""


def git(*arguments: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise ScanError("Git 内容读取失败；未输出可能含敏感信息的错误原文")
    return result.stdout if result.returncode == 0 else b""


def content_findings(data: bytes) -> set[str]:
    findings = set()
    if any(match[0] not in PLACEHOLDERS for match in TOKEN_PATTERN.finditer(data)):
        findings.add("API_KEY_PATTERN")
    if PRIVATE_KEY_PATTERN.search(data):
        findings.add("PRIVATE_KEY")
    return findings


def path_findings(path: str) -> set[str]:
    # Example content is still scanned; only the filename exclusion is relaxed.
    relative = PurePosixPath(path)
    name, parts = relative.name, relative.parts
    if name != ".env.example" and (name == ".env" or name.startswith(".env.")):
        return {"LOCAL_ENV_FILE"}
    if (relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
            or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}):
        return {"PRIVATE_KEY_FILE"}
    if name in {".netrc", ".pypirc"} or parts[:1] in {("secrets",), (".ssh",)}:
        return {"LOCAL_CREDENTIAL_PATH"}
    if (parts[:3] == ("src", "china_stock_sim", "publishing")
            or parts[:2] == ("deploy", "web")
            or path == "deploy/launchd/bin/A股模拟-云端上传"
            or parts[:1] == ("configs",) and len(parts) == 2
            and name.startswith("publishing") and name.endswith(".toml")
            or parts[:1] == ("tests",) and len(parts) == 2 and (
                name.startswith(("test_static_publishing", "test_publishing"))
                and name.endswith(".py")
                or name in {"test_trading_cloud_interaction.py", "test_trading_history_cloud.py"})):
        return {"LOCAL_PUBLISHING_COMPONENT"}
    if (path == "deploy/web/nginx.conf" or parts[:1] == ("configs",) and len(parts) == 2 and (
            name == "publishing.toml" or name.startswith("local.")
            or name.endswith((".local.toml", "-local.toml")))):
        return {"LOCAL_DEPLOYMENT_CONFIG"}
    if parts[:1] in {("data",), ("reports",), ("logs",)} and name != ".gitkeep":
        return {"LOCAL_RUNTIME_DATA"}
    if (path == "deploy/launchd/current.plist"
            or parts[:2] == ("deploy", "macos") and (
                any(part.endswith(".app") for part in parts[2:])
                or len(parts) == 3 and name.endswith(".build.json"))):
        return {"GENERATED_HOST_ARTIFACT"}
    return set()


def staged_entries():
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        if stage != b"0":
            raise ScanError("索引含未解决的合并冲突，无法完成密钥检查")
        if mode == b"160000":
            continue  # A submodule is a commit reference, not file content.
        yield path.decode("utf-8", "surrogateescape"), object_id.decode("ascii")


def tree_entries(commit: str):
    for entry in git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        _, kind, object_id = metadata.split()
        if kind == b"blob":
            yield path.decode("utf-8", "surrogateescape"), object_id.decode("ascii")


def resolve_commit(reference: str) -> str:
    value = git("rev-parse", "--verify", "--end-of-options", reference + "^{commit}")
    object_id = value.decode("ascii").strip()
    if not OBJECT_ID_PATTERN.fullmatch(object_id):
        raise ScanError("无法解析待检查的提交")
    return object_id


def history_commits(reference: str) -> list[str]:
    return git("rev-list", resolve_commit(reference)).decode("ascii").splitlines()


def pre_push_commits(lines: list[str]) -> list[str]:
    commits = set()
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            raise ScanError("pre-push 输入格式不正确，拒绝放行")
        _, local_id, _, remote_id = fields
        if not all(OBJECT_ID_PATTERN.fullmatch(value) for value in (local_id, remote_id)):
            raise ScanError("pre-push 对象标识不正确，拒绝放行")
        if set(local_id) == {"0"}:
            continue  # Branch deletion transmits no new file content.
        local_commit = resolve_commit(local_id)
        arguments = ["rev-list", local_commit]
        if set(remote_id) != {"0"}:
            remote_commit = (
                git(
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    remote_id + "^{commit}",
                    check=False,
                )
                .decode("ascii")
                .strip()
            )
            if OBJECT_ID_PATTERN.fullmatch(remote_commit):
                arguments.append("^" + remote_commit)
        # A new branch, or an unavailable remote object, scans the entire history.
        commits.update(git(*arguments).decode("ascii").splitlines())
    return sorted(commits)


def scan(snapshots) -> tuple[list[dict], int]:
    cached_content = {}
    findings = []
    for revision, entries in snapshots:
        for path, object_id in entries:
            issues = path_findings(path)
            # A forbidden path is already a rejection. Do not read private
            # configuration or potentially large runtime blobs just to reject it.
            if not issues:
                if object_id not in cached_content:
                    cached_content[object_id] = content_findings(git("cat-file", "blob", object_id))
                issues = cached_content[object_id]
            if issues:
                findings.append({"revision": revision, "path": path, "types": sorted(issues)})
    return findings, len(cached_content)


def scan_worktree() -> tuple[list[dict], int]:
    """Inspect prospective working files without staging or following symlinks.

    This complements the actual-index and history modes. A tracked deletion
    has no prospective file bytes; deletion does not bypass the pre-push history
    check. Ignored untracked files never enter the candidate set.
    """
    root = Path(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    arguments = ("-C", str(root), "ls-files", "--full-name", "-z")
    paths = set(git(*arguments, "--cached", "--others", "--exclude-standard").split(b"\0"))
    deleted = set(git(*arguments, "--deleted").split(b"\0"))
    findings, checked = [], 0
    for raw_path in sorted(paths - {b""} - deleted):
        relative = PurePosixPath(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ScanError("工作区路径无效，拒绝放行")
        path = root.joinpath(*relative.parts)
        checked += 1
        issues = path_findings(str(relative))
        if not issues:
            for parent in path.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise ScanError("工作区路径包含目录符号链接，拒绝跟随读取")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                # Git stores the link text, not the target's file contents.
                data = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ScanError("工作区文件在检查期间发生变化，拒绝放行")
                    data = stream.read()
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                        raise ScanError("工作区文件在检查期间发生变化，拒绝放行")
            else:
                raise ScanError("工作区存在不支持的文件类型，拒绝放行")
            issues = content_findings(data)
        if issues:
            findings.append({"revision": "WORKTREE", "path": str(relative),
                             "types": sorted(issues)})
    return findings, checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Git 内容中的密钥；不输出命中内容")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--staged", action="store_true", help="检查整个暂存索引")
    modes.add_argument("--tracked", action="store_true", help="检查 Git 索引（适用于 CI checkout）")
    modes.add_argument("--worktree", action="store_true", help="只读检查工作区已跟踪和未忽略的新文件")
    modes.add_argument("--history", metavar="REF", help="检查某提交可达的完整历史")
    modes.add_argument("--pre-push", action="store_true", help="从标准输入读取 Git pre-push 引用")
    args = parser.parse_args(argv)
    try:
        if args.worktree:
            findings, blobs = scan_worktree()
        elif args.staged or args.tracked:
            snapshots = [("INDEX", staged_entries())]
        else:
            commits = (
                history_commits(args.history)
                if args.history
                else pre_push_commits(sys.stdin.read().splitlines())
            )
            snapshots = ((commit, tree_entries(commit)) for commit in commits)
        if not args.worktree:
            findings, blobs = scan(snapshots)
    except (OSError, ValueError, UnicodeError, ScanError) as exc:
        reason = str(exc) if isinstance(exc, ScanError) else "无法安全完成检查，拒绝放行"
        print(
            json.dumps({"status": "SCAN_ERROR", "reason": reason}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    if findings:
        # ASCII escaping also prevents control characters in filenames affecting the terminal.
        print(
            json.dumps({"status": "BLOCKED", "findings": findings}, ensure_ascii=True),
            file=sys.stderr,
        )
        return 1
    count_key = "files_checked" if args.worktree else "unique_blobs_checked"
    print(json.dumps({"status": "PASS", count_key: blobs}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
