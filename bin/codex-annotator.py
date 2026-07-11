#!/usr/bin/env python3
"""
Background tmux status annotator for Codex sessions.
Classifies managed tmux windows as RUN/READY/ERR and prefixes the window title.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR_FOR_IMPORTS = Path(__file__).resolve().parent
for import_root in (SCRIPT_DIR_FOR_IMPORTS.parent, SCRIPT_DIR_FOR_IMPORTS):
    if (import_root / "codex_looper").is_dir():
        sys.path.insert(0, str(import_root))
        break

from codex_looper.pane_status import (  # noqa: E402
    aggregate_window_state,
    classify_claude_output,
    classify_codex_output,
    is_claude_command,
    is_codex_command,
)

DEFAULT_SESSION_PATTERN = os.environ.get("CODEX_ANNOTATOR_SESSION_REGEX", r"^codex")
DEFAULT_RUNNING_PATTERN = os.environ.get("CODEX_ANNOTATOR_RUNNING_REGEX", r"(codex|node|ssh)")
DEFAULT_ENABLED = os.environ.get("CODEX_ANNOTATOR_ENABLED", "1").lower() not in {
    "0",
    "false",
    "no",
}
DEFAULT_UPDATE_TITLES = os.environ.get("CODEX_ANNOTATOR_UPDATE_TITLES", "0").lower() not in {
    "0",
    "false",
    "no",
}
DEFAULT_NOTIFY_READY = os.environ.get("CODEX_ANNOTATOR_NOTIFY_READY", "1").lower() not in {
    "0",
    "false",
    "no",
}
DEFAULT_READY_MESSAGE = os.environ.get("CODEX_ANNOTATOR_READY_MESSAGE", "READY: {name}")
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
TMUX_STATE_OPTION = "@codex_state"
TMUX_LAST_READY_OPTION = "@codex_last_ready"


def parse_positive_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a positive finite number") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("interval must be a positive finite number")
    return interval


def default_interval_from_env(value: str) -> float:
    try:
        return parse_positive_interval(value)
    except argparse.ArgumentTypeError:
        return 1.0


DEFAULT_INTERVAL = default_interval_from_env(os.environ.get("CODEX_ANNOTATOR_INTERVAL", "1.0"))


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
    state: str | None = None


@dataclasses.dataclass
class WindowInfo:
    sid: str
    wid: str
    current_name: str
    base_name: str
    state: str | None = None


@dataclasses.dataclass
class PaneInfo:
    pid: str
    current_command: str
    dead: bool
    start_command: str = ""


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


def run_tmux(cmd: list[str], *, verbose: bool) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        log("tmux not found; annotator exiting", verbose=verbose)
        return None
    if result.returncode != 0:
        detail = result.stderr.strip()
        if detail:
            log(f"tmux command failed ({' '.join(cmd)}): {detail}", verbose=verbose)
        else:
            log(f"tmux command failed ({' '.join(cmd)})", verbose=verbose)
        return None
    return result.stdout


def list_sessions(verbose: bool) -> list[SessionInfo]:
    output = run_tmux(
        ["tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}"],
        verbose=verbose,
    )
    sessions: list[SessionInfo] = []
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


def list_windows(session: SessionInfo, *, verbose: bool) -> list[WindowInfo]:
    output = run_tmux(
        ["tmux", "list-windows", "-t", session.sid, "-F", "#{window_id}\t#{window_name}"],
        verbose=verbose,
    )
    windows: list[WindowInfo] = []
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


def window_pane_states(wid: str, *, verbose: bool) -> list[PaneInfo] | None:
    output = run_tmux(
        [
            "tmux",
            "list-panes",
            "-t",
            wid,
            "-F",
            "#{pane_id}\t#{pane_current_command}\t#{pane_dead}\t#{pane_start_command}",
        ],
        verbose=verbose,
    )
    if not output:
        return None
    panes: list[PaneInfo] = []
    for line in output.splitlines():
        if line.count("\t") < 2:
            continue
        parts = line.split("\t", 3)
        if len(parts) == 3:
            pid, cmd, dead = parts
            start_command = cmd
        else:
            pid, cmd, dead, start_command = parts
        panes.append(
            PaneInfo(
                pid=pid.strip(),
                current_command=cmd.strip(),
                dead=dead.strip() in {"1", "true", "True"},
                start_command=start_command.strip(),
            )
        )
    return panes


def capture_pane_output(pane_id: str, *, verbose: bool) -> str | None:
    return run_tmux(
        ["tmux", "capture-pane", "-t", pane_id, "-e", "-p", "-S", f"-{CAPTURE_LINES}"],
        verbose=verbose,
    )


def is_looper_command(cmd: str) -> bool:
    return bool(
        re.search(
            r"(^|[/\s])(?:codex|claude|gemini)-looper(?:\.py)?(\s|$)",
            cmd,
            re.IGNORECASE,
        )
    )


def classify_pane(
    pane: PaneInfo,
    running_regex: re.Pattern,
    *,
    verbose: bool,
) -> str | None:
    if pane.dead:
        return None
    command_context = f"{pane.current_command} {pane.start_command}".strip()
    if is_looper_command(command_context):
        return "RUN"
    if is_codex_command(command_context):
        output = capture_pane_output(pane.pid, verbose=verbose)
        return classify_codex_output(output)
    if is_claude_command(command_context):
        output = capture_pane_output(pane.pid, verbose=verbose)
        return classify_claude_output(output)
    if running_regex.search(command_context):
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

    pane_states: list[str] = []
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


def set_window_status(window: WindowInfo, *, ready_at: float | None, verbose: bool) -> None:
    run_tmux(
        [
            "tmux",
            "set-window-option",
            "-q",
            "-t",
            window.wid,
            TMUX_STATE_OPTION,
            window.state or "",
        ],
        verbose=verbose,
    )
    if ready_at is not None:
        run_tmux(
            [
                "tmux",
                "set-window-option",
                "-q",
                "-t",
                window.wid,
                TMUX_LAST_READY_OPTION,
                str(int(ready_at)),
            ],
            verbose=verbose,
        )


def notify_ready(window: WindowInfo, message_template: str, *, verbose: bool) -> None:
    try:
        message = message_template.format(name=window.base_name, state=window.state or "")
    except (IndexError, KeyError, ValueError):
        message = f"READY: {window.base_name}"
    run_tmux(["tmux", "display-message", "-t", window.wid, message], verbose=verbose)


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
    session_pattern: re.Pattern,
    running_pattern: re.Pattern,
    *,
    state_cache: dict[str, str] | None = None,
    update_titles: bool = DEFAULT_UPDATE_TITLES,
    notify_on_ready: bool = DEFAULT_NOTIFY_READY,
    ready_message: str = DEFAULT_READY_MESSAGE,
    now: float | None = None,
    verbose: bool,
) -> None:
    sessions = list_sessions(verbose=verbose)
    registered = registered_sessions()
    seen_windows: set[str] = set()
    now = time.time() if now is None else now
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
            previous_state = state_cache.get(window.wid) if state_cache is not None else None
            ready_at = now if previous_state == "RUN" and window.state == "READY" else None
            set_window_status(window, ready_at=ready_at, verbose=verbose)
            if notify_on_ready and ready_at is not None:
                notify_ready(window, ready_message, verbose=verbose)
            if update_titles:
                rename_window(window, verbose=verbose)
            if state_cache is not None:
                state_cache[window.wid] = window.state
    if state_cache is not None:
        for wid in list(state_cache):
            if wid not in seen_windows:
                del state_cache[wid]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Annotate tmux window titles with RUN/READY/ERR status."
    )
    parser.add_argument(
        "--interval",
        type=parse_positive_interval,
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
        "--update-titles",
        action="store_true",
        default=DEFAULT_UPDATE_TITLES,
        help="Prefix tmux window titles with RUN/READY/ERR status.",
    )
    parser.add_argument(
        "--no-notify-ready",
        action="store_false",
        dest="notify_ready",
        default=DEFAULT_NOTIFY_READY,
        help="Disable tmux display-message notifications when a window becomes READY.",
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
    try:
        session_pattern = re.compile(args.session_regex)
        running_pattern = re.compile(args.running_regex)
    except re.error as exc:
        print(f"Invalid regular expression: {exc}", file=sys.stderr)
        return 2

    lock_dir = os.path.dirname(LOCKFILE)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    with open(LOCKFILE, "w", encoding="utf-8") as lock:
        try:
            os.chmod(LOCKFILE, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Annotator already running; exiting.", verbose=args.verbose)
            return 0

        state_cache: dict[str, str] = {}

        while True:
            try:
                annotate_once(
                    session_pattern,
                    running_pattern,
                    state_cache=state_cache,
                    update_titles=args.update_titles,
                    notify_on_ready=args.notify_ready,
                    ready_message=DEFAULT_READY_MESSAGE,
                    verbose=args.verbose,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log(f"Annotator loop error: {exc}", verbose=args.verbose)
            if args.once:
                break
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
