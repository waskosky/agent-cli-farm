from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"
STATE_SCHEMA_VERSION = 1


def utc_iso_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(
            json.dumps(_jsonable(value), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class LooperStateRecorder:
    def __init__(self, run_dir: Path, initial_state: dict[str, Any]) -> None:
        self.run_dir = run_dir
        started_at = str(initial_state.get("started_at") or utc_iso_stamp())
        self.state: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "started_at": started_at,
            "updated_at": started_at,
            **initial_state,
        }
        self.state_path = run_dir / STATE_FILENAME
        self.events_path = run_dir / EVENTS_FILENAME

    def record(self, event: str, **updates: Any) -> None:
        stamp = utc_iso_stamp()
        self.state.update({key: _jsonable(value) for key, value in updates.items()})
        self.state["updated_at"] = stamp
        self.state["last_event"] = event
        if event == "run_stopped" and not self.state.get("completed_at"):
            self.state["completed_at"] = stamp
        event_record = {"ts": stamp, "event": event, **self.state}
        _atomic_write_json(self.state_path, self.state)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as events_file:
            events_file.write(json.dumps(_jsonable(event_record), sort_keys=True) + "\n")
