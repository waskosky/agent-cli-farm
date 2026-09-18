#!/usr/bin/env python3
"""Resolve resumable Codex thread identity and inspect writer ownership."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - tmux hosts provide fcntl
    fcntl = None


MAX_METADATA_BYTES = 1024 * 1024
MAX_METADATA_LINES = 50
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def valid_thread_id(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def thread_id_from_filename(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\.jsonl)?\Z",
        path.name,
    )
    return match.group(1) if match else None


def read_session_meta(path: Path) -> dict[str, Any] | None:
    consumed = 0
    try:
        with path.open("rb") as handle:
            for _ in range(MAX_METADATA_LINES):
                line = handle.readline(MAX_METADATA_BYTES - consumed + 1)
                if not line:
                    return None
                consumed += len(line)
                if consumed > MAX_METADATA_BYTES:
                    return None
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else None
    except OSError:
        return None
    return None


def is_subagent_meta(payload: dict[str, Any]) -> bool:
    source = payload.get("source")
    return (isinstance(source, dict) and "subagent" in source) or valid_thread_id(
        payload.get("parent_thread_id")
    )


def resumable_id_from_path(path: Path) -> str | None:
    filename_id = thread_id_from_filename(path)
    payload = read_session_meta(path)
    if payload is None:
        return filename_id

    thread_id = payload.get("id")
    if not valid_thread_id(thread_id):
        thread_id = filename_id

    if is_subagent_meta(payload):
        for field in ("session_id", "parent_thread_id"):
            root_id = payload.get(field)
            if valid_thread_id(root_id):
                return root_id
        return None

    if valid_thread_id(thread_id):
        return thread_id
    session_id = payload.get("session_id")
    return session_id if valid_thread_id(session_id) else None


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def resolve_thread_id(thread_id: str) -> str | None:
    if not valid_thread_id(thread_id):
        return None
    sessions_root = codex_home() / "sessions"
    if sessions_root.is_dir():
        try:
            candidates = sorted(
                sessions_root.rglob(f"*{thread_id}.jsonl"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            candidates = []
        for path in candidates:
            if thread_id_from_filename(path) != thread_id:
                continue
            resolved = resumable_id_from_path(path)
            if resolved is not None:
                return resolved
    return thread_id


def thread_id_from_pid(pid: int) -> str | None:
    """Resolve paginated sessions even when no rollout descriptor stays open.

    Only held writer locks count. A stale lock filename, argv, cwd, or newest
    transcript alone cannot establish ownership. Ambiguous roots fail closed.
    """
    proc = Path(os.environ.get("CODEX_PROC_ROOT", "/proc")) / str(pid)
    roots: set[str] = set()
    try:
        descriptors = list((proc / "fd").iterdir())
    except OSError:
        return None
    for descriptor in descriptors:
        try:
            target = descriptor.resolve(strict=True)
            if target.parent != (codex_home() / "thread-writer-locks").resolve():
                continue
            if target.suffix != ".lock" or not valid_thread_id(target.stem):
                continue
            info = (proc / "fdinfo" / descriptor.name).read_text()
            if not any(line.startswith("lock:") for line in info.splitlines()):
                continue
            resolved = resolve_thread_id(target.stem)
            if resolved:
                roots.add(resolved)
        except OSError:
            continue
    if len(roots) > 1:
        raise ValueError("Codex process holds multiple independent conversation writers")
    return roots.pop() if roots else None


def wait_until_writable(thread_id: str, timeout_seconds: float) -> bool:
    if fcntl is None:
        return True
    lock_path = codex_home() / "thread-writer-locks" / f"{thread_id}.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with lock_path.open("r+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    return True
        except FileNotFoundError:
            return True
        except OSError:
            # Codex still performs the authoritative ownership check.
            return True

        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve resumable Codex thread identity and writer ownership."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_path = subparsers.add_parser("resolve-path")
    resolve_path.add_argument("path", type=Path)

    resolve_id = subparsers.add_parser("resolve-id")
    resolve_id.add_argument("thread_id")

    resolve_pid = subparsers.add_parser("resolve-pid")
    resolve_pid.add_argument("pid", type=int)

    wait_writable = subparsers.add_parser("wait-writable")
    wait_writable.add_argument("thread_id")
    wait_writable.add_argument("timeout", type=float, nargs="?", default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "resolve-pid":
        try:
            resolved = thread_id_from_pid(args.pid) if args.pid > 0 else None
        except ValueError:
            return 3
        if resolved is None:
            return 1
        sys.stdout.write(resolved)
        return 0
    if args.command == "resolve-path":
        resolved = resumable_id_from_path(args.path)
        if resolved is None:
            return 1
        sys.stdout.write(resolved)
        return 0

    if args.command == "resolve-id":
        resolved = resolve_thread_id(args.thread_id)
        if resolved is None:
            print("Invalid Codex thread ID.", file=sys.stderr)
            return 2
        sys.stdout.write(resolved)
        return 0

    if not valid_thread_id(args.thread_id) or not math.isfinite(args.timeout) or args.timeout < 0:
        print("Invalid Codex writer-wait arguments.", file=sys.stderr)
        return 2
    if wait_until_writable(args.thread_id, args.timeout):
        return 0
    print("Codex thread already has an active writer.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
