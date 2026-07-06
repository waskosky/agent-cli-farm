from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .hybrid import TmuxCommandResult, default_command_runner, tmux_prompt_paste_commands

CONTROL_FILENAME = "control.jsonl"
OPERATOR_NOTES_FILENAME = "operator_notes.jsonl"
VALID_CONTROL_ACTIONS = frozenset({"stop_after_prompt", "stop_after_loop", "interrupt_now"})
OPERATOR_NOTE_DELIVERIES = frozenset({"record", "btw", "prompt"})
TMUX_PANE_FIELDS = (
    "#{session_name}\t#{window_name}\t#{pane_id}\t#{pane_current_command}\t"
    "#{pane_start_command}\t#{pane_pid}\t#{pane_current_path}\t#{pane_active}"
)
INTERACTIVE_AGENT_COMMANDS = frozenset({"claude", "codex", "gemini"})

CommandRunner = Callable[..., TmuxCommandResult]


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


def operator_notes_file_path(run_dir: Path) -> Path:
    return run_dir / OPERATOR_NOTES_FILENAME


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


def format_operator_note_for_delivery(note: str, *, delivery: str = "btw") -> str:
    normalized_delivery = str(delivery or "btw").strip()
    if normalized_delivery not in OPERATOR_NOTE_DELIVERIES:
        valid = ", ".join(sorted(OPERATOR_NOTE_DELIVERIES))
        raise ControlError(f"invalid operator note delivery {normalized_delivery!r}; expected one of: {valid}")
    text = str(note or "").strip()
    if not text:
        raise ControlError("operator note text is required")
    if normalized_delivery == "btw":
        return "/btw " + re.sub(r"\s+", " ", text).strip()
    return text


def _compact_preview(text: str, *, limit: int = 200) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "..."


def append_operator_note(
    run_dir: Path,
    *,
    note: str,
    delivery: str,
    status: str,
    actor: str = "operator",
    target: Mapping[str, Any] | None = None,
    error: str = "",
    command_id: str | None = None,
    delivered_text_preview: str = "",
) -> dict[str, Any]:
    if delivery not in OPERATOR_NOTE_DELIVERIES:
        valid = ", ".join(sorted(OPERATOR_NOTE_DELIVERIES))
        raise ControlError(f"invalid operator note delivery {delivery!r}; expected one of: {valid}")
    record: dict[str, Any] = {
        "id": command_id or _control_id(),
        "created_at": utc_iso_stamp(),
        "actor": str(actor or "operator"),
        "delivery": delivery,
        "status": status,
        "note": str(note or "").strip(),
        "note_preview": _compact_preview(note),
        "target": dict(target or {}),
    }
    if error:
        record["error"] = error
    if delivered_text_preview:
        record["delivered_text_preview"] = _compact_preview(delivered_text_preview)
    path = operator_notes_file_path(run_dir)
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


def _tmux_result(command_runner: CommandRunner, command: list[str], input_text: str | None = None) -> TmuxCommandResult:
    try:
        return command_runner(command, input_text=input_text)
    except TypeError:
        return command_runner(command, input_text)


def _parse_tmux_panes(stdout: str) -> list[dict[str, Any]]:
    panes: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        panes.append(
            {
                "session_name": parts[0],
                "window_name": parts[1],
                "pane_id": parts[2],
                "pane_current_command": parts[3],
                "pane_start_command": parts[4],
                "pane_pid": parts[5],
                "pane_current_path": parts[6],
                "pane_active": parts[7] == "1",
            }
        )
    return panes


def _pane_command_matches_agent(pane: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    candidates = {
        str(state.get("agent_name") or "").strip().lower(),
        str(state.get("agent_kind") or "").strip().lower(),
    }
    candidates.update(INTERACTIVE_AGENT_COMMANDS)
    commands = [
        str(pane.get("pane_current_command") or "").strip().lower(),
        str(pane.get("pane_start_command") or "").strip().lower(),
    ]
    for command in commands:
        command_name = Path(command.split()[0]).name if command else ""
        if command_name in candidates:
            return True
        if any(candidate and re.search(rf"(^|[/\s-]){re.escape(candidate)}($|[\s.-])", command) for candidate in candidates):
            return True
    return False


def _pane_score(
    pane: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    tmux_session: str,
) -> int:
    score = 0
    label = str(state.get("label") or state.get("run_id") or "").strip()
    if tmux_session and pane.get("session_name") == tmux_session:
        score += 60
    if label and pane.get("window_name") == label:
        score += 80
    if _pane_command_matches_agent(pane, state):
        score += 30
    if pane.get("pane_active") is True:
        score += 5
    agent_cwd = str(state.get("agent_cwd") or "").strip()
    pane_path = str(pane.get("pane_current_path") or "").strip()
    if agent_cwd and pane_path:
        try:
            if Path(agent_cwd).resolve() == Path(pane_path).resolve():
                score += 20
        except OSError:
            pass
    return score


def resolve_operator_note_target(
    *,
    state: Mapping[str, Any],
    pane_id: str = "",
    tmux_session: str = "",
    allow_pane_scan: bool = False,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, Any]:
    normalized_pane = str(pane_id or "").strip()
    if normalized_pane:
        return {
            "ok": True,
            "source": "explicit_pane",
            "paneId": normalized_pane,
            "runDir": str(state.get("run_dir") or ""),
            "label": str(state.get("label") or ""),
        }

    state_pane = str(state.get("hybrid_pane_id") or "").strip()
    if state_pane:
        return {
            "ok": True,
            "source": "looper_state",
            "paneId": state_pane,
            "runDir": str(state.get("run_dir") or ""),
            "label": str(state.get("label") or ""),
            "sessionId": str(state.get("current_session_id") or ""),
        }

    if not allow_pane_scan:
        return {
            "ok": False,
            "source": "looper_state",
            "runDir": str(state.get("run_dir") or ""),
            "label": str(state.get("label") or ""),
            "error": (
                "selected run has no hybrid pane id; pass --pane for an explicit target "
                "or --allow-pane-scan after verifying operator intent"
            ),
        }

    result = _tmux_result(
        command_runner,
        ["tmux", "list-panes", "-a", "-F", TMUX_PANE_FIELDS],
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "tmux list-panes failed").strip()
        return {
            "ok": False,
            "source": "tmux_panes",
            "runDir": str(state.get("run_dir") or ""),
            "label": str(state.get("label") or ""),
            "error": detail,
        }

    panes = _parse_tmux_panes(result.stdout)
    candidates = [
        pane
        for pane in panes
        if (not tmux_session or pane.get("session_name") == tmux_session)
        and _pane_command_matches_agent(pane, state)
    ]
    if not candidates:
        return {
            "ok": False,
            "source": "tmux_panes",
            "runDir": str(state.get("run_dir") or ""),
            "label": str(state.get("label") or ""),
            "error": "no matching interactive agent pane found",
        }
    selected = max(candidates, key=lambda pane: _pane_score(pane, state=state, tmux_session=tmux_session))
    return {
        "ok": True,
        "source": "tmux_panes",
        "paneId": selected["pane_id"],
        "runDir": str(state.get("run_dir") or ""),
        "label": str(state.get("label") or ""),
        "sessionName": selected.get("session_name") or "",
        "windowName": selected.get("window_name") or "",
        "paneCommand": selected.get("pane_current_command") or "",
        "panePid": selected.get("pane_pid") or "",
        "paneCurrentPath": selected.get("pane_current_path") or "",
    }


def paste_text_to_tmux_pane(
    *,
    pane_id: str,
    text: str,
    command_runner: CommandRunner = default_command_runner,
    buffer_name: str | None = None,
) -> dict[str, Any]:
    normalized_pane = str(pane_id or "").strip()
    if not normalized_pane:
        return {"ok": False, "error": "pane id is required"}
    buffer = buffer_name or f"codex-looper-note-{secrets.token_hex(6)}"
    for command in tmux_prompt_paste_commands(normalized_pane, buffer_name=buffer):
        input_text = text if command[1:2] == ["load-buffer"] else None
        result = _tmux_result(command_runner, command, input_text)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "tmux paste failed").strip()
            return {
                "ok": False,
                "error": detail,
                "paneId": normalized_pane,
                "failedCommand": command,
            }
    _tmux_result(command_runner, ["tmux", "delete-buffer", "-b", buffer])
    return {"ok": True, "paneId": normalized_pane, "bufferName": buffer}


def deliver_operator_note(
    *,
    run_dir: Path,
    state: Mapping[str, Any],
    note: str,
    delivery: str = "btw",
    actor: str = "operator",
    pane_id: str = "",
    tmux_session: str = "",
    allow_pane_scan: bool = False,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, Any]:
    normalized_delivery = str(delivery or "btw").strip()
    delivered_text = format_operator_note_for_delivery(note, delivery=normalized_delivery)
    if normalized_delivery == "record":
        record = append_operator_note(
            run_dir,
            note=note,
            delivery=normalized_delivery,
            status="recorded",
            actor=actor,
        )
        return {
            "ok": True,
            "delivery": normalized_delivery,
            "delivered": False,
            "status": "recorded",
            "note": record,
            "target": {},
        }

    target = resolve_operator_note_target(
        state=state,
        pane_id=pane_id,
        tmux_session=tmux_session,
        allow_pane_scan=allow_pane_scan,
        command_runner=command_runner,
    )
    if not target.get("ok"):
        error = str(target.get("error") or "operator note target not found")
        record = append_operator_note(
            run_dir,
            note=note,
            delivery=normalized_delivery,
            status="failed",
            actor=actor,
            target=target,
            error=error,
        )
        return {
            "ok": False,
            "delivery": normalized_delivery,
            "delivered": False,
            "status": "failed",
            "note": record,
            "target": target,
            "error": error,
        }

    pasted = paste_text_to_tmux_pane(
        pane_id=str(target.get("paneId") or ""),
        text=delivered_text,
        command_runner=command_runner,
    )
    if not pasted.get("ok"):
        error = str(pasted.get("error") or "tmux paste failed")
        record = append_operator_note(
            run_dir,
            note=note,
            delivery=normalized_delivery,
            status="failed",
            actor=actor,
            target=target,
            error=error,
            delivered_text_preview=delivered_text,
        )
        return {
            "ok": False,
            "delivery": normalized_delivery,
            "delivered": False,
            "status": "failed",
            "note": record,
            "target": target,
            "error": error,
        }

    record = append_operator_note(
        run_dir,
        note=note,
        delivery=normalized_delivery,
        status="delivered",
        actor=actor,
        target=target,
        delivered_text_preview=delivered_text,
    )
    return {
        "ok": True,
        "delivery": normalized_delivery,
        "delivered": True,
        "status": "delivered",
        "note": record,
        "target": target,
        "deliveredTextPreview": _compact_preview(delivered_text),
    }


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
