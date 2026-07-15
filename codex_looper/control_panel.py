from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .control import (
    append_control_command,
    deliver_operator_note,
    force_stop_from_state,
    format_stop_signal_results,
    interrupt_from_state,
    latest_focus_update,
)
from .state import STATE_FILENAME
from .status_state import repair_stale_state_file
from .transcript import format_agent_log_line


@dataclass(frozen=True)
class ControlPaneActionResult:
    message: str
    should_exit: bool = False


def load_run_state(run_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((run_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    raw.setdefault("run_dir", str(run_dir))
    return raw


def current_log_path(pointer_path: Path) -> Path | None:
    try:
        text = pointer_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return Path(text) if text else None


def supervisor_is_running(supervisor_pid: int | None) -> bool:
    if supervisor_pid is None or supervisor_pid <= 0:
        return True
    try:
        os.kill(supervisor_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def format_state_summary(
    state: dict[str, Any],
    *,
    log_path: Path | None,
    focus_summary: str = "",
) -> str:
    label = str(state.get("label") or state.get("run_id") or "unknown")
    status = str(state.get("status") or "unknown")
    loop = state.get("current_loop")
    prompt = state.get("current_prompt_index")
    total_prompts = state.get("total_prompts")
    stop_reason = str(state.get("stop_reason") or "")

    location = f"loop {loop or 0}"
    if prompt and total_prompts:
        location = f"{location}, prompt {prompt}/{total_prompts}"
    lines = [
        "== Looper Control Pane ==",
        f"run: {label}",
        f"status: {status} ({location})",
        f"focus: {focus_summary or 'not reported yet'}",
        f"log: {log_path if log_path else 'waiting for current log'}",
        "actions: b NOTE /btw | p stop after prompt | l stop after loop | i interrupt now | f force stop | r repair stale | q quit",
        "enter an action key, then Enter.",
    ]
    if stop_reason:
        lines.append(f"stop reason: {stop_reason}")
    return "\n".join(lines)


def render_header(run_dir: Path, pointer_path: Path, output_stream: TextIO) -> None:
    state = load_run_state(run_dir)
    focus = latest_focus_update(run_dir)
    print(
        format_state_summary(
            state,
            log_path=current_log_path(pointer_path),
            focus_summary=str(focus.get("summary") or ""),
        ),
        file=output_stream,
    )
    output_stream.flush()


def _state_for_signals(run_dir: Path) -> dict[str, Any]:
    state = load_run_state(run_dir)
    state.setdefault("run_dir", str(run_dir))
    return state


def run_control_pane_action(
    choice: str,
    *,
    run_dir: Path,
    force_grace_seconds: float = 5.0,
) -> ControlPaneActionResult:
    normalized = choice.strip().lower()
    if not normalized:
        return ControlPaneActionResult("")
    key = normalized[0]
    if key == "q":
        return ControlPaneActionResult("leaving looper control pane", should_exit=True)
    if key == "b":
        note = normalized[1:].strip()
        if not note:
            return ControlPaneActionResult("usage: b <operator note to send with /btw>")
        result = deliver_operator_note(
            run_dir=run_dir,
            state=_state_for_signals(run_dir),
            note=note,
            delivery="btw",
            actor="control pane",
        )
        if result.get("ok"):
            pane = result.get("target", {}).get("paneId")
            suffix = f" to {pane}" if pane else ""
            return ControlPaneActionResult(f"sent operator /btw note{suffix}")
        return ControlPaneActionResult(
            "operator note delivery failed: " + str(result.get("error") or "unknown delivery error")
        )
    if key == "p":
        append_control_command(
            run_dir,
            action="stop_after_prompt",
            reason="control pane: stop after prompt",
        )
        return ControlPaneActionResult("queued stop_after_prompt")
    if key == "l":
        append_control_command(
            run_dir,
            action="stop_after_loop",
            reason="control pane: stop after loop",
        )
        return ControlPaneActionResult("queued stop_after_loop")
    if key == "i":
        append_control_command(
            run_dir,
            action="interrupt_now",
            reason="control pane: interrupt now",
        )
        try:
            state, repaired, stale_reason = repair_stale_state_file(run_dir / STATE_FILENAME)
        except Exception as exc:
            return ControlPaneActionResult(f"stale-state repair failed: {exc}")
        if repaired and stale_reason:
            return ControlPaneActionResult(f"repaired stale looper state: {stale_reason}")
        detail = interrupt_from_state(state)
        return ControlPaneActionResult(detail or "queued interrupt_now")
    if key == "f":
        append_control_command(
            run_dir,
            action="interrupt_now",
            reason="control pane: force stop",
        )
        try:
            state, repaired, stale_reason = repair_stale_state_file(run_dir / STATE_FILENAME)
        except Exception as exc:
            return ControlPaneActionResult(f"stale-state repair failed: {exc}")
        if repaired and stale_reason:
            return ControlPaneActionResult(f"repaired stale looper state: {stale_reason}")
        results = force_stop_from_state(
            state,
            grace_seconds=force_grace_seconds,
        )
        detail = format_stop_signal_results(results) or "queued force stop"
        return ControlPaneActionResult(detail)
    if key == "r":
        try:
            _, repaired, stale_reason = repair_stale_state_file(run_dir / STATE_FILENAME)
        except Exception as exc:
            return ControlPaneActionResult(f"stale-state repair failed: {exc}")
        if repaired and stale_reason:
            return ControlPaneActionResult(f"repaired stale looper state: {stale_reason}")
        return ControlPaneActionResult("state is not stale")
    return ControlPaneActionResult(f"unknown action: {choice.strip()}")


def emit_new_log_lines(
    *,
    log_path: Path,
    offset: int,
    output_stream: TextIO,
) -> int:
    try:
        size = log_path.stat().st_size
    except (FileNotFoundError, OSError):
        return offset
    if size < offset:
        offset = 0
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
    except OSError:
        return offset
    for raw_line in chunk.splitlines(keepends=True):
        rendered = format_agent_log_line(raw_line.decode("utf-8", errors="replace"))
        if rendered is None:
            continue
        print(rendered, file=output_stream)
    output_stream.flush()
    return offset


def _stdin_ready(input_stream: TextIO, timeout_seconds: float) -> bool:
    try:
        ready, _, _ = select.select([input_stream], [], [], timeout_seconds)
    except (OSError, ValueError):
        time.sleep(timeout_seconds)
        return False
    return bool(ready)


def control_pane_main(
    argv: list[str] | None = None,
    *,
    prog: str = "control-pane",
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Show a looper transcript pane with basic operator controls.",
    )
    parser.add_argument("--run-dir", required=True, help="exact looper run directory")
    parser.add_argument("--pointer", required=True, help="current-log pointer path")
    parser.add_argument("--supervisor-pid", type=int, required=True, help="looper supervisor pid")
    parser.add_argument("--refresh-seconds", type=float, default=1.0, help="poll interval")
    parser.add_argument(
        "--force-grace-seconds",
        type=float,
        default=5.0,
        help="seconds between force-stop escalation stages",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    run_dir = Path(args.run_dir)
    pointer_path = Path(args.pointer)
    last_log_path: Path | None = None
    last_focus_id = str(latest_focus_update(run_dir).get("id") or "")
    offset = 0
    render_header(run_dir, pointer_path, output_stream)

    while True:
        log_path = current_log_path(pointer_path)
        if log_path != last_log_path:
            last_log_path = log_path
            offset = 0
            print("", file=output_stream)
            render_header(run_dir, pointer_path, output_stream)
        focus_id = str(latest_focus_update(run_dir).get("id") or "")
        if focus_id != last_focus_id:
            last_focus_id = focus_id
            print("", file=output_stream)
            render_header(run_dir, pointer_path, output_stream)
        if log_path is not None:
            offset = emit_new_log_lines(
                log_path=log_path,
                offset=offset,
                output_stream=output_stream,
            )

        if _stdin_ready(input_stream, max(args.refresh_seconds, 0.05)):
            line = input_stream.readline()
            if not line:
                break
            result = run_control_pane_action(
                line,
                run_dir=run_dir,
                force_grace_seconds=args.force_grace_seconds,
            )
            if result.message:
                print(result.message, file=output_stream)
                output_stream.flush()
            render_header(run_dir, pointer_path, output_stream)
            if result.should_exit:
                return 0

        if not supervisor_is_running(args.supervisor_pid):
            if log_path is not None:
                offset = emit_new_log_lines(
                    log_path=log_path,
                    offset=offset,
                    output_stream=output_stream,
                )
            print("\nLooper supervisor exited; control pane stopped.", file=output_stream)
            output_stream.flush()
            return 0
