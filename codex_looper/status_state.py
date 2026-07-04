from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .state import EVENTS_FILENAME, STATE_FILENAME, _atomic_write_json, _jsonable, utc_iso_stamp

ACTIVE_RUN_STATUSES = frozenset({"running", "retrying"})
EXTERNAL_TERMINATION_PREFIX = "external termination"


@dataclass(frozen=True)
class LooperStateView:
    state_path: Path
    state: dict[str, Any]
    updated_at: str
    stale: bool = False
    stale_reason: str | None = None
    repaired: bool = False
    unreadable_error: str | None = None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        run_dir = str(self.state.get("run_dir") or self.state_path.parent)
        run_name = Path(run_dir).name if run_dir else self.state_path.parent.name
        return self.updated_at, run_name, run_dir


def _load_state(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("state file did not contain a JSON object")
    return raw


def _coerce_pid(value: Any) -> int | None:
    try:
        pid = int(str(value))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return pid


def _parse_linux_proc_state(stat_text: str) -> str | None:
    command_end = stat_text.rfind(")")
    if command_end < 0:
        return None
    fields = stat_text[command_end + 1 :].strip().split()
    if not fields:
        return None
    return fields[0]


def _linux_proc_state(pid: int) -> str | None:
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        return _parse_linux_proc_state(stat_text)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def process_is_running(pid: int) -> bool:
    if os.name == "posix" and _linux_proc_state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def active_state_stale_reason(state: Mapping[str, Any]) -> str | None:
    status = str(state.get("status") or "")
    if status not in ACTIVE_RUN_STATUSES:
        return None
    supervisor_pid = _coerce_pid(state.get("pid"))
    if supervisor_pid is None:
        return "active looper state has no supervisor pid"
    if process_is_running(supervisor_pid):
        return None
    return f"supervisor process {supervisor_pid} is no longer running"


def stopped_state_from_stale(
    state: Mapping[str, Any],
    *,
    stale_reason: str,
    stamp: str | None = None,
) -> dict[str, Any]:
    updated = stamp or utc_iso_stamp()
    out = {str(key): _jsonable(value) for key, value in state.items()}
    out["status"] = "stopped"
    out["updated_at"] = updated
    out["completed_at"] = out.get("completed_at") or updated
    out["last_event"] = "run_stopped"
    out["stale_repaired_at"] = updated
    out["stale_reason"] = stale_reason
    out["stop_reason"] = f"{EXTERNAL_TERMINATION_PREFIX}: {stale_reason}"
    out.setdefault("exit_code", None)
    return out


def repair_stale_state_file(state_path: Path, *, stamp: str | None = None) -> tuple[dict[str, Any], bool, str | None]:
    state = _load_state(state_path)
    stale_reason = active_state_stale_reason(state)
    if stale_reason is None:
        return state, False, None

    updated = stamp or utc_iso_stamp()
    repaired = stopped_state_from_stale(state, stale_reason=stale_reason, stamp=updated)
    _atomic_write_json(state_path, repaired)
    events_path = state_path.parent / EVENTS_FILENAME
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event_record = {"ts": updated, "event": "run_stopped", **repaired}
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(event_record), sort_keys=True) + "\n")
    return repaired, True, stale_reason


def iter_looper_state_views(
    state_root: Path,
    *,
    repair_stale: bool = False,
) -> Iterator[LooperStateView]:
    for state_path in state_root.glob(f"*/{STATE_FILENAME}"):
        try:
            if repair_stale:
                state, repaired, stale_reason = repair_stale_state_file(state_path)
            else:
                state = _load_state(state_path)
                stale_reason = active_state_stale_reason(state)
                repaired = False
        except Exception as exc:
            yield LooperStateView(
                state_path=state_path,
                state={"label": state_path.parent.name, "status": f"unreadable: {exc}"},
                updated_at="",
                unreadable_error=str(exc),
            )
            continue

        state.setdefault("run_dir", str(state_path.parent))
        updated_at = str(state.get("updated_at") or state.get("started_at") or "")
        if stale_reason and not repaired:
            display_state = dict(state)
            display_state["stale"] = True
            display_state["stale_reason"] = stale_reason
        else:
            display_state = state
        yield LooperStateView(
            state_path=state_path,
            state=display_state,
            updated_at=updated_at,
            stale=stale_reason is not None and not repaired,
            stale_reason=stale_reason,
            repaired=repaired,
        )
