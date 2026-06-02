#!/usr/bin/env python3
"""
Background tmux status annotator for Codex sessions.
Classifies managed tmux windows as RUN/READY/ERR and prefixes the window title.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import os
import re
import subprocess
import sys
import time
from typing import List, Optional


DEFAULT_SESSION_PATTERN = os.environ.get("CODEX_ANNOTATOR_SESSION_REGEX", r"^codex")
DEFAULT_RUNNING_PATTERN = os.environ.get(
    "CODEX_ANNOTATOR_RUNNING_REGEX", r"(codex|node|ssh)"
)
DEFAULT_INTERVAL = float(os.environ.get("CODEX_ANNOTATOR_INTERVAL", "1.0"))
DEFAULT_ENABLED = os.environ.get("CODEX_ANNOTATOR_ENABLED", "1").lower() not in {
    "0",
    "false",
    "no",
}
DEFAULT_CAPTURE_LINES = os.environ.get("CODEX_ANNOTATOR_CAPTURE_LINES", "200")
STATE_DIR = os.environ.get(
    "XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")
)
LOCKFILE = os.environ.get(
    "CODEX_ANNOTATOR_LOCKFILE", os.path.join(STATE_DIR, "codexfarm", "annotator.lock")
)
SESSION_REGISTRY = os.environ.get(
    "CODEX_ANNOTATOR_SESSION_REGISTRY",
    os.path.join(STATE_DIR, "codexfarm", "managed_sessions"),
)
IGNORE_PREFIX = os.environ.get("CODEX_ANNOTATOR_IGNORE_PREFIX", "!")
MEMORY_FLAG_PATTERN = r"\*[0-9]+(?:\.[0-9]+)?\+MB\*\*"

ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
CODEX_IDLE_PROMPT_PATTERN = r"(?:\u276f|\u203a|codex>)"
CODEX_IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{CODEX_IDLE_PROMPT_PATTERN}\s*$)\s*\Z"
CODEX_WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
CODEX_ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
CODEX_PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
CODEX_USER_PREFIX_PATTERN = r"^You\b"
CODEX_ASSISTANT_PREFIX_PATTERN = r"^(?:assistant|codex|agent)\s*:"

CLAUDE_IDLE_PROMPT_PATTERN = r">(?:\s|\u00a0)"
CLAUDE_IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{CLAUDE_IDLE_PROMPT_PATTERN}\s*$)\s*\Z"
CLAUDE_WAITING_PROMPT_PATTERN = r"\u276f.*\d+\."
CLAUDE_PROCESSING_PATTERN = (
    r"[\u2736\u2722\u273d\u273b\u00b7\u2733].*\u2026.*\(esc to interrupt.*\)"
)


def parse_capture_lines(value: str) -> int:
    try:
        return max(50, int(value))
    except ValueError:
        return 200


CAPTURE_LINES = parse_capture_lines(DEFAULT_CAPTURE_LINES)


@dataclasses.dataclass
class SessionInfo:
    sid: str
    current_name: str
    base_name: str
    state: Optional[str] = None


@dataclasses.dataclass
class WindowInfo:
    sid: str
    wid: str
    current_name: str
    base_name: str
    state: Optional[str] = None


@dataclasses.dataclass
class PaneInfo:
    pid: str
    current_command: str
    dead: bool


def log(msg: str, *, verbose: bool) -> None:
    if verbose:
        sys.stderr.write(msg.rstrip() + "\n")
        sys.stderr.flush()


def strip_status_prefix(name: str) -> str:
    """Remove annotator prefixes to get stable base name."""
    name = re.sub(MEMORY_FLAG_PATTERN, "", name).strip()
    previous = None
    while previous != name:
        previous = name
        name = re.sub(r"^\*(READY|RUN|ERR)\*\s*", "", name, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", name).strip()


def memory_flag_prefix(name: str) -> str:
    match = re.search(MEMORY_FLAG_PATTERN, name)
    return match.group(0) if match else ""


def run_tmux(cmd: List[str], *, verbose: bool) -> Optional[str]:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        log("tmux not found; annotator exiting", verbose=verbose)
        return None
    except subprocess.CalledProcessError as exc:
        log(
            f"tmux command failed ({' '.join(cmd)})",
            verbose=verbose,
        )
        return None


def list_sessions(verbose: bool) -> List[SessionInfo]:
    output = run_tmux(
        ["tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}"],
        verbose=verbose,
    )
    sessions: List[SessionInfo] = []
    if not output:
        return sessions
    for line in output.splitlines():
        if "\t" not in line:
            continue
        sid, name = line.split("\t", 1)
        base = strip_status_prefix(name)
        sessions.append(
            SessionInfo(
                sid=sid.strip(),
                current_name=name.strip(),
                base_name=base.strip(),
            )
        )
    return sessions


def list_windows(session: SessionInfo, *, verbose: bool) -> List[WindowInfo]:
    output = run_tmux(
        ["tmux", "list-windows", "-t", session.sid, "-F", "#{window_id}\t#{window_name}"],
        verbose=verbose,
    )
    windows: List[WindowInfo] = []
    if not output:
        return windows
    for line in output.splitlines():
        if "\t" not in line:
            continue
        wid, name = line.split("\t", 1)
        base = strip_status_prefix(name)
        windows.append(
            WindowInfo(
                sid=session.sid,
                wid=wid.strip(),
                current_name=name.strip(),
                base_name=base.strip(),
            )
        )
    return windows


def window_pane_states(wid: str, *, verbose: bool) -> Optional[List[PaneInfo]]:
    output = run_tmux(
        [
            "tmux",
            "list-panes",
            "-t",
            wid,
            "-F",
            "#{pane_id}\t#{pane_current_command}\t#{pane_dead}",
        ],
        verbose=verbose,
    )
    if not output:
        return None
    panes: List[PaneInfo] = []
    for line in output.splitlines():
        if line.count("\t") < 2:
            continue
        pid, cmd, dead = line.split("\t", 2)
        panes.append(
            PaneInfo(
                pid=pid.strip(),
                current_command=cmd.strip(),
                dead=dead.strip() in {"1", "true", "True"},
            )
        )
    return panes


def strip_ansi(text: str) -> str:
    return re.sub(ANSI_CODE_PATTERN, "", text)


def tail_lines(text: str, lines: int = 50) -> str:
    return "\n".join(text.splitlines()[-lines:])


def capture_pane_output(pane_id: str, *, verbose: bool) -> Optional[str]:
    return run_tmux(
        ["tmux", "capture-pane", "-t", pane_id, "-e", "-p", "-S", f"-{CAPTURE_LINES}"],
        verbose=verbose,
    )


def is_codex_command(cmd: str) -> bool:
    return bool(re.search(r"\bcodex\b", cmd, re.IGNORECASE))


def is_claude_command(cmd: str) -> bool:
    return bool(re.search(r"\bclaude\b", cmd, re.IGNORECASE))


def classify_codex_output(output: Optional[str]) -> str:
    if not output:
        return "ERR"
    clean = strip_ansi(output)
    tail_output = tail_lines(clean)
    last_user = None
    for match in re.finditer(CODEX_USER_PREFIX_PATTERN, clean, re.IGNORECASE | re.MULTILINE):
        last_user = match

    output_after_last_user = clean[last_user.start() :] if last_user else clean
    assistant_after_last_user = bool(
        last_user
        and re.search(
            CODEX_ASSISTANT_PREFIX_PATTERN,
            output_after_last_user,
            re.IGNORECASE | re.MULTILINE,
        )
    )

    if last_user is not None:
        if not assistant_after_last_user:
            if re.search(
                CODEX_WAITING_PROMPT_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            ):
                return "READY"
            if re.search(
                CODEX_ERROR_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            ):
                return "ERR"
    else:
        if re.search(CODEX_WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
            return "READY"
        if re.search(CODEX_ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
            return "ERR"

    if re.search(CODEX_IDLE_PROMPT_AT_END_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return "READY"

    if re.search(CODEX_PROCESSING_PATTERN, tail_output, re.IGNORECASE):
        return "RUN"
    return "READY"


def classify_claude_output(output: Optional[str]) -> str:
    if not output:
        return "ERR"
    clean = strip_ansi(output)
    tail_output = tail_lines(clean)
    if re.search(CLAUDE_WAITING_PROMPT_PATTERN, tail_output, re.MULTILINE):
        return "READY"
    if re.search(CLAUDE_PROCESSING_PATTERN, tail_output, re.MULTILINE):
        return "RUN"
    if re.search(CLAUDE_IDLE_PROMPT_AT_END_PATTERN, tail_output, re.MULTILINE):
        return "READY"
    return "READY"


def aggregate_window_state(states: List[str]) -> str:
    if not states:
        return "ERR"
    if "RUN" in states:
        return "RUN"
    if "ERR" in states:
        return "ERR"
    return "READY"


def classify_pane(
    pane: PaneInfo,
    running_regex: re.Pattern,
    *,
    verbose: bool,
) -> Optional[str]:
    if pane.dead:
        return None
    if is_codex_command(pane.current_command):
        output = capture_pane_output(pane.pid, verbose=verbose)
        return classify_codex_output(output)
    if is_claude_command(pane.current_command):
        output = capture_pane_output(pane.pid, verbose=verbose)
        return classify_claude_output(output)
    if running_regex.search(pane.current_command):
        return "RUN"
    return "READY"


def classify_window(
    window: WindowInfo,
    running_regex: re.Pattern,
    *,
    verbose: bool,
) -> WindowInfo:
    panes = window_pane_states(window.wid, verbose=verbose)
    if panes is None:
        window.state = "ERR"
        return window

    pane_states: List[str] = []
    for pane in panes:
        state = classify_pane(pane, running_regex, verbose=verbose)
        if state is not None:
            pane_states.append(state)

    window.state = aggregate_window_state(pane_states)
    return window


def desired_name(item: SessionInfo | WindowInfo) -> str:
    marker = memory_flag_prefix(item.current_name)
    marker_prefix = f"{marker} " if marker else ""
    state_prefix = f"*{item.state}* "
    return f"{marker_prefix}{state_prefix}{item.base_name}"


def rename_window(window: WindowInfo, *, verbose: bool) -> None:
    new_name = desired_name(window)
    if window.current_name == new_name:
        return
    run_tmux(["tmux", "rename-window", "-t", window.wid, new_name], verbose=verbose)


def registered_sessions() -> set[str]:
    sessions: set[str] = set()
    try:
        with open(SESSION_REGISTRY, encoding="utf-8") as registry:
            for line in registry:
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                if "\t" in value:
                    value = value.split("\t", 1)[0].strip()
                if value and value != "session":
                    sessions.add(strip_status_prefix(value))
    except FileNotFoundError:
        return sessions
    except OSError:
        return sessions
    return sessions


def should_manage(
    session: SessionInfo, pattern: re.Pattern, registered: set[str] | None = None
) -> bool:
    if session.base_name.startswith(IGNORE_PREFIX):
        return False
    if pattern.search(session.base_name):
        return True
    return session.base_name in (registered or set())


def should_manage_window(window: WindowInfo) -> bool:
    return not window.base_name.startswith(IGNORE_PREFIX)


def annotate_once(
    session_pattern: re.Pattern, running_pattern: re.Pattern, *, verbose: bool
) -> None:
    sessions = list_sessions(verbose=verbose)
    registered = registered_sessions()
    seen_windows: set[str] = set()
    for session in sessions:
        if not should_manage(session, session_pattern, registered):
            continue
        windows = list_windows(session, verbose=verbose)
        for window in windows:
            if window.wid in seen_windows:
                continue
            if not should_manage_window(window):
                continue
            seen_windows.add(window.wid)
            classify_window(window, running_pattern, verbose=verbose)
            if window.state is None:
                continue
            rename_window(window, verbose=verbose)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Annotate tmux window titles with RUN/READY/ERR status."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="Polling interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--session-regex",
        default=DEFAULT_SESSION_PATTERN,
        help="Regex for base session names to manage (default: %(default)s)",
    )
    parser.add_argument(
        "--running-regex",
        default=DEFAULT_RUNNING_PATTERN,
        help="Regex for pane commands considered RUNNING (default: %(default)s)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single annotation pass then exit.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print tmux errors for debugging.",
    )
    return parser.parse_args()


def main() -> int:
    if not DEFAULT_ENABLED:
        return 0

    args = parse_args()

    os.makedirs(os.path.dirname(LOCKFILE), exist_ok=True)
    with open(LOCKFILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Annotator already running; exiting.", verbose=args.verbose)
            return 0

        session_pattern = re.compile(args.session_regex)
        running_pattern = re.compile(args.running_regex)

        while True:
            try:
                annotate_once(
                    session_pattern,
                    running_pattern,
                    verbose=args.verbose,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log(f"Annotator loop error: {exc}", verbose=args.verbose)
            if args.once:
                break
            time.sleep(max(args.interval, 0.1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
