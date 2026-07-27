#!/usr/bin/env python3
"""Record the active coding-agent session on its managed tmux pane."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MAX_INPUT_BYTES = 1024 * 1024
PANE_RE = re.compile(r"%[0-9]+\Z")
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
PROVIDER_EXECUTABLES = {
    "codex": {"codex", "codex.exe", "codex.js"},
    "claude": {"claude", "claude.exe"},
    "gemini": {"gemini", "gemini.exe", "gemini.js"},
}
HOOK_EVENTS = {"SessionStart", "UserPromptSubmit"}


def process_info(pid: int) -> tuple[int, list[str]] | None:
    proc_dir = Path("/proc") / str(pid)
    try:
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
        after_name = stat_text.rsplit(")", 1)[1].split()
        parent_pid = int(after_name[1])
        cmdline = (proc_dir / "cmdline").read_bytes().split(b"\0")
        tokens = [value.decode("utf-8", errors="replace") for value in cmdline if value]
        comm = (proc_dir / "comm").read_text(encoding="utf-8").strip()
        return parent_pid, [comm, *tokens]
    except (IndexError, OSError, ValueError):
        pass

    try:
        result = subprocess.run(
            [
                "ps",
                "-p",
                str(pid),
                "-o",
                "ppid=",
                "-o",
                "comm=",
                "-o",
                "args=",
            ],
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
        fields = result.stdout.strip().split(maxsplit=2)
        if result.returncode != 0 or len(fields) < 2:
            return None
        parent_pid = int(fields[0])
        tokens = [fields[1], *fields[2].split()] if len(fields) == 3 else [fields[1]]
        return parent_pid, tokens
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def provider_ancestor_pid(provider: str) -> int | None:
    expected = PROVIDER_EXECUTABLES[provider]
    pid = os.getppid()
    visited: set[int] = set()
    for _ in range(64):
        if pid <= 1 or pid in visited:
            return None
        visited.add(pid)
        info = process_info(pid)
        if info is None:
            return None
        parent_pid, tokens = info
        if any(Path(token).name.lower() in expected for token in tokens):
            return pid
        pid = parent_pid
    return None


def set_tmux_option(pane: str, option: str, value: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "set-option", "-p", "-t", pane, option, value],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def unset_tmux_option(pane: str, option: str) -> None:
    try:
        subprocess.run(
            ["tmux", "set-option", "-p", "-u", "-t", pane, option],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def record_session() -> None:
    if os.environ.get("CODEXFARM_MANAGED") != "1":
        return

    pane = os.environ.get("TMUX_PANE", "")
    provider = os.environ.get("CODEXFARM_PROVIDER", "")
    if not PANE_RE.fullmatch(pane) or provider not in PROVIDER_EXECUTABLES:
        return

    raw_payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw_payload) > MAX_INPUT_BYTES:
        return
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    session_id = payload.get("session_id")
    event_name = payload.get("hook_event_name")
    if (
        not isinstance(session_id, str)
        or not UUID_RE.fullmatch(session_id)
        or event_name not in HOOK_EVENTS
    ):
        return

    provider_pid = provider_ancestor_pid(provider)
    if provider_pid is None:
        return

    unset_tmux_option(pane, "@codexfarm_session_id")
    metadata = [
        ("@codexfarm_provider", provider),
        ("@codexfarm_session_source", f"hook:{event_name}"),
        ("@codexfarm_session_seen_at", str(int(time.time()))),
        ("@codexfarm_session_pid", str(provider_pid)),
    ]
    for option, value in metadata:
        if not set_tmux_option(pane, option, value):
            return
    set_tmux_option(pane, "@codexfarm_session_id", session_id)


def main() -> int:
    try:
        record_session()
    except Exception:
        # Hooks must never interrupt the interactive provider.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
