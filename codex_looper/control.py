from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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


@dataclass(frozen=True)
class SignalTarget:
    kind: str
    identifier: int
    label: str


@dataclass(frozen=True)
class StopSignalResult:
    stage: str
    target: SignalTarget
    signal_name: str
    ok: bool
    detail: str


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


def _coerce_pid(value: Any) -> int | None:
    try:
        pid = int(str(value))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return pid


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _process_group_for_pid(pid: int) -> int | None:
    if os.name != "posix":
        return None
    try:
        pgid = os.getpgid(pid)
    except (LookupError, ProcessLookupError, PermissionError, OSError):
        return None
    return pgid if pgid > 0 else None


def descendant_pids(root_pid: int) -> list[int]:
    pending = [root_pid]
    descendants: list[int] = []
    seen = {root_pid}
    while pending:
        parent = pending.pop(0)
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(parent)],
                text=True,
                capture_output=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            break
        if result.returncode not in (0, 1):
            break
        for line in result.stdout.splitlines():
            child = _coerce_pid(line.strip())
            if child is None or child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            pending.append(child)
    return descendants


def _add_target(
    targets: list[SignalTarget],
    seen: set[tuple[str, int]],
    target: SignalTarget,
) -> None:
    key = (target.kind, target.identifier)
    if key in seen:
        return
    seen.add(key)
    targets.append(target)


def _add_process_group_or_process(
    targets: list[SignalTarget],
    seen: set[tuple[str, int]],
    *,
    pid: int,
    label: str,
) -> None:
    pgid = _process_group_for_pid(pid)
    if pgid is not None:
        _add_target(
            targets,
            seen,
            SignalTarget("process_group", pgid, f"{label} process group"),
        )
        return
    _add_target(targets, seen, SignalTarget("process", pid, f"{label} process"))


def stop_targets_from_state(state: Mapping[str, Any]) -> list[SignalTarget]:
    targets: list[SignalTarget] = []
    seen: set[tuple[str, int]] = set()
    child_pgid = _coerce_pid(state.get("child_pgid"))
    child_pid = _coerce_pid(state.get("child_pid"))
    hybrid_pane_pid = _coerce_pid(state.get("hybrid_pane_pid"))
    supervisor_pid = _coerce_pid(state.get("pid"))

    if child_pgid is not None and os.name == "posix":
        _add_target(
            targets,
            seen,
            SignalTarget("process_group", child_pgid, "child process group"),
        )
    elif child_pid is not None:
        _add_process_group_or_process(targets, seen, pid=child_pid, label="child")

    if hybrid_pane_pid is not None:
        for descendant_pid in descendant_pids(hybrid_pane_pid):
            _add_process_group_or_process(
                targets,
                seen,
                pid=descendant_pid,
                label="hybrid descendant",
            )
        _add_process_group_or_process(
            targets,
            seen,
            pid=hybrid_pane_pid,
            label="hybrid pane",
        )

    if supervisor_pid is not None:
        _add_target(
            targets,
            seen,
            SignalTarget("process", supervisor_pid, "supervisor process"),
        )

    return targets


def target_is_running(target: SignalTarget) -> bool:
    try:
        if target.kind == "process_group":
            if os.name != "posix":
                return False
            os.killpg(target.identifier, 0)
        else:
            os.kill(target.identifier, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def send_signal_to_targets(
    targets: Iterable[SignalTarget],
    signum: int,
    *,
    stage: str,
) -> list[StopSignalResult]:
    signal_name = _signal_name(signum)
    results: list[StopSignalResult] = []
    for target in targets:
        try:
            if target.kind == "process_group":
                if os.name != "posix":
                    raise OSError("process groups are unsupported on this platform")
                os.killpg(target.identifier, signum)
            else:
                os.kill(target.identifier, signum)
        except (LookupError, ProcessLookupError):
            results.append(
                StopSignalResult(
                    stage=stage,
                    target=target,
                    signal_name=signal_name,
                    ok=False,
                    detail=f"{target.label} {target.identifier} is no longer running",
                )
            )
        except (OSError, ValueError) as exc:
            results.append(
                StopSignalResult(
                    stage=stage,
                    target=target,
                    signal_name=signal_name,
                    ok=False,
                    detail=f"{stage} failed for {target.label} {target.identifier}: {exc}",
                )
            )
        else:
            results.append(
                StopSignalResult(
                    stage=stage,
                    target=target,
                    signal_name=signal_name,
                    ok=True,
                    detail=f"{stage}: sent {signal_name} to {target.label} {target.identifier}",
                )
            )
    return results


def format_stop_signal_results(results: Iterable[StopSignalResult]) -> str | None:
    result_list = list(results)
    all_details = [result.detail for result in result_list]
    details = [result.detail for result in result_list if result.ok or "failed" in result.detail]
    if not details:
        return "\n".join(all_details) if all_details else None
    return "\n".join(details)


def interrupt_from_state(state: Mapping[str, Any]) -> str | None:
    results = send_signal_to_targets(
        stop_targets_from_state(state),
        signal.SIGINT,
        stage="interrupt",
    )
    return format_stop_signal_results(results)


def force_stop_from_state(
    state: Mapping[str, Any],
    *,
    grace_seconds: float = 5.0,
    kill: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    target_is_running_fn: Callable[[SignalTarget], bool] = target_is_running,
) -> list[StopSignalResult]:
    targets = stop_targets_from_state(state)
    if not targets:
        return []

    results = send_signal_to_targets(targets, signal.SIGINT, stage="interrupt")
    if grace_seconds > 0:
        sleep_fn(grace_seconds)

    remaining = [target for target in targets if target_is_running_fn(target)]
    if remaining:
        results.extend(send_signal_to_targets(remaining, signal.SIGTERM, stage="terminate"))
        if grace_seconds > 0:
            sleep_fn(grace_seconds)

    remaining = [target for target in targets if target_is_running_fn(target)]
    if kill and remaining:
        results.extend(send_signal_to_targets(remaining, signal.SIGKILL, stage="kill"))
    return results
