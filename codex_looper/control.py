from __future__ import annotations

import json
import os
import secrets
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTROL_FILENAME = "control.jsonl"
VALID_CONTROL_ACTIONS = frozenset({"stop_after_prompt", "stop_after_loop", "interrupt_now"})


class ControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlCommand:
    id: str
    action: str
    reason: str
    created_at: str


def utc_iso_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _control_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def control_file_path(run_dir: Path) -> Path:
    return run_dir / CONTROL_FILENAME


def append_control_command(
    run_dir: Path,
    *,
    action: str,
    reason: str = "",
    command_id: str | None = None,
) -> dict[str, str]:
    if action not in VALID_CONTROL_ACTIONS:
        valid = ", ".join(sorted(VALID_CONTROL_ACTIONS))
        raise ControlError(f"invalid control action {action!r}; expected one of: {valid}")
    record = {
        "id": command_id or _control_id(),
        "action": action,
        "reason": reason,
        "created_at": utc_iso_stamp(),
    }
    path = control_file_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def read_control_commands(run_dir: Path, *, processed_ids: Iterable[str]) -> list[ControlCommand]:
    processed = set(processed_ids)
    path = control_file_path(run_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    commands: list[ControlCommand] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        action = raw.get("action")
        if action not in VALID_CONTROL_ACTIONS:
            continue
        command_id = raw.get("id")
        normalized_id = str(command_id) if command_id else f"line-{line_number}"
        if normalized_id in processed:
            continue
        reason = raw.get("reason")
        created_at = raw.get("created_at")
        commands.append(
            ControlCommand(
                id=normalized_id,
                action=str(action),
                reason=str(reason) if reason is not None else "",
                created_at=str(created_at) if created_at is not None else "",
            )
        )
    return commands


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def iter_run_states(state_root: Path) -> Iterable[dict[str, Any]]:
    if not state_root.exists():
        return
    for state_path in state_root.glob("*/state.json"):
        state = _load_state(state_path)
        if state is None:
            continue
        state.setdefault("run_dir", str(state_path.parent))
        yield state


def _state_sort_key(state: Mapping[str, Any]) -> tuple[str, str, str]:
    run_dir = str(state.get("run_dir") or "")
    updated_at = str(state.get("updated_at") or state.get("started_at") or "")
    run_name = Path(run_dir).name if run_dir else ""
    return updated_at, run_name, run_dir


def select_control_run(
    *,
    state_root: Path,
    label: str | None = None,
    run_dir: Path | None = None,
    include_stopped: bool = False,
) -> dict[str, Any]:
    if run_dir is not None:
        state = _load_state(run_dir / "state.json") or {"run_dir": str(run_dir), "label": run_dir.name}
        state["run_dir"] = str(run_dir)
        return state

    candidates = list(iter_run_states(state_root))
    if label:
        candidates = [
            state
            for state in candidates
            if state.get("label") == label
            or state.get("run_id") == label
            or Path(str(state.get("run_dir") or "")).name == label
        ]
    if not include_stopped:
        active = [
            state
            for state in candidates
            if str(state.get("status") or "") in {"running", "retrying"}
        ]
        if active:
            candidates = active
    if not candidates:
        target = label or str(state_root)
        raise ControlError(f"no matching looper run found for {target}")
    return max(candidates, key=_state_sort_key)


def interrupt_from_state(state: Mapping[str, Any]) -> str | None:
    pgid = state.get("child_pgid")
    pid = state.get("child_pid") or state.get("pid")
    try:
        if pgid and os.name == "posix":
            os.killpg(int(str(pgid)), signal.SIGINT)
            return f"sent SIGINT to process group {pgid}"
        if pid:
            os.kill(int(str(pid)), signal.SIGINT)
            return f"sent SIGINT to process {pid}"
    except (LookupError, ProcessLookupError):
        return "target process is no longer running"
    except (OSError, ValueError) as exc:
        return f"interrupt failed: {exc}"
    return None
