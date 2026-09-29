#!/usr/bin/env python3
"""Retry dead, exact Codex resumes that failed during SQLite initialization."""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
PREFIX = "tmux set-window-option -q remain-on-exit on >/dev/null 2>&1 || true; exec "


def tmux(*args: str) -> str:
    return subprocess.check_output(["tmux", *args], text=True, stderr=subprocess.DEVNULL).strip()


def exact_resume(command: str, session_id: str) -> bool:
    """Accept only our known launch grammar; never execute captured shell text."""
    if not UUID.fullmatch(session_id):
        return False
    # tmux quotes pane_start_command when it contains spaces.
    if command.startswith('"') and command.endswith('"'):
        command = command[1:-1]
    if command.startswith(PREFIX):
        command = command[len(PREFIX) :]
    elif command.startswith("exec "):
        command = command[5:]
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args or Path(args.pop(0)).name != "codex":
        return False
    if args[:2] == ["-c", "check_for_update_on_startup=false"]:
        args = args[2:]
    return args == ["resume", session_id]


def sqlite_startup_failure(output: str) -> bool:
    # Narrow panes wrap words, including "sqlite" and "locked".
    compact = re.sub(r"\s+", "", output).lower()
    fatal = compact.rsplit("error:", 1)[-1] if "error:" in compact else ""
    fatal = re.sub(r"paneisdead\(status[^)]*\)$", "", fatal)
    return fatal.startswith("failedtoinitializesqlitelocaldbat") and fatal.endswith(
        "databaseislocked"
    )


def recover(session: str) -> int:
    executable = shutil.which("codex")
    if not executable:
        return 0
    helper = Path(__file__).with_name("codex-session-meta.py")
    if not helper.is_file():
        return 0
    failures = 0
    # A board may link farm windows; unique pane IDs avoid repeated retries.
    for pane in dict.fromkeys(
        tmux("list-panes", "-s", "-t", "=" + session, "-F", "#{pane_id}").splitlines()
    ):
        try:

            def field(fmt: str, pane: str = pane) -> str:
                return tmux("display-message", "-p", "-t", pane, fmt)

            if field("#{pane_dead}:#{pane_dead_status}") != "1:1":
                continue
            if field("#{@codexfarm_provider}") != "codex" or not field("#{@codexfarm_name}"):
                continue
            session_id = field("#{@codexfarm_session_id}")
            command = field("#{pane_start_command}")
            if not exact_resume(command, session_id):
                continue
            if not sqlite_startup_failure(tmux("capture-pane", "-p", "-t", pane, "-S", "-100")):
                continue
            writable = subprocess.run(
                [sys.executable, str(helper), "wait-writable", session_id, "2"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if writable.returncode:
                print(
                    f"Leaving {pane} unchanged: its conversation still has an active writer.",
                    file=sys.stderr,
                )
                failures += 1
                continue
            # No -k: tmux refuses to replace a pane that became live meanwhile.
            # Recheck identity too, in case another recovery already completed.
            if (
                field("#{pane_dead}:#{pane_dead_status}") != "1:1"
                or field("#{pane_start_command}") != command
            ):
                continue
            launch = PREFIX + shlex.join(
                [
                    executable,
                    "-c",
                    "check_for_update_on_startup=false",
                    "resume",
                    session_id,
                ]
            )
            tmux("respawn-pane", "-t", pane, launch)
            print(
                f"Retried Codex pane {pane} after a SQLite startup lock (exact saved conversation).",
                file=sys.stderr,
            )
        except (OSError, subprocess.CalledProcessError):
            print(
                f"Could not recover pane {pane}; leaving it available for inspection.",
                file=sys.stderr,
            )
            failures += 1
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    args = parser.parse_args()
    try:
        return recover(args.session)
    except (OSError, subprocess.CalledProcessError):
        print("Unable to inspect startup failures; attaching without recovery.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
