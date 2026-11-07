"""Small restore event records with a shared, strictly bounded retention budget."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from codex_looper.health import read_memory

MAX_LOG_BYTES = 1024 * 1024
LOG_COUNT = 3
MAX_RECORD_BYTES = 8192
EVENTS = ("start", "launch_start", "launch_end", "skip", "memory_refused", "complete", "exit")


def read_text(path: Path) -> str:
    try:
        with path.open() as handle:
            return handle.read(4096)
    except (OSError, UnicodeError):
        return ""


def resource_snapshot(proc: Path) -> dict:
    memory = read_memory(proc)
    pressure = {}
    for resource in ("cpu", "io", "memory"):
        values = {}
        for line in read_text(proc / "pressure" / resource).splitlines():
            fields = line.split()
            if not fields or fields[0] not in {"some", "full"}:
                continue
            try:
                value = float(dict(field.split("=", 1) for field in fields[1:])["avg10"])
                if math.isfinite(value) and 0 <= value <= 100:
                    values[fields[0]] = value
            except (ValueError, KeyError):
                continue
        pressure[resource] = values
    processes = {"total": 0, "codex": 0, "node": 0}
    rss = {"codex": 0.0, "node": 0.0}
    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            processes["total"] += 1
            command = read_text(entry / "comm").strip()
            if command in rss:
                processes[command] += 1
                for line in read_text(entry / "status").splitlines():
                    if line.startswith("VmRSS:"):
                        try:
                            rss[command] += int(line.split()[1]) / 1024
                        except (ValueError, IndexError):
                            pass
    except OSError:
        pass
    return {
        "memory": asdict(memory) if memory else None,
        "pressure_avg10": pressure,
        "processes": processes,
        "rss_mib": {key: round(value, 1) for key, value in rss.items()},
    }


def tmux_snapshot() -> dict:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_dead}"],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if result.returncode:
        return {"available": False}
    states = [line for line in result.stdout.splitlines() if line in {"0", "1"}]
    return {"available": True, "panes": len(states), "dead": states.count("1")}


def append_record(directory: Path, record: dict, *, max_bytes: int = MAX_LOG_BYTES) -> bool:
    payload = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(payload) > min(MAX_RECORD_BYTES, max_bytes):
        return False
    if directory.is_symlink():
        raise ValueError("diagnostics directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    paths = [directory / "restore.jsonl"] + [
        directory / f"restore.jsonl.{index}" for index in range(1, LOG_COUNT)
    ]
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock = os.open(directory / ".restore.lock", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("diagnostic lock must be a regular file")
        os.fchmod(lock, 0o600)
        deadline = time.monotonic() + 0.25
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("diagnostic logs must be regular files")
            if path.exists() and path.stat().st_size > max_bytes:
                path.unlink()
        if paths[0].exists() and paths[0].stat().st_size + len(payload) > max_bytes:
            paths[-1].unlink(missing_ok=True)
            for index in range(LOG_COUNT - 1, 0, -1):
                if paths[index - 1].exists():
                    paths[index - 1].replace(paths[index])
        fd = os.open(paths[0], flags | os.O_APPEND, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("diagnostic log must be a regular file")
            os.fchmod(fd, 0o600)
            if os.write(fd, payload) != len(payload):
                raise OSError("incomplete diagnostic write")
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=EVENTS)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=("registry", "dispatch", "farm"), required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--memory-policy", choices=("warn", "enforce", "ignore"), required=True)
    parser.add_argument("--provider", choices=("codex", "claude", "gemini", "other"))
    for field in ("row", "exit-code", "restored", "failed"):
        parser.add_argument("--" + field, type=int)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]{1,10}-[0-9]{1,5}-[0-9]{1,5}", args.run_id):
        parser.error("invalid diagnostic run identifier")
    proc = Path(os.environ.get("CODEX_PROC_ROOT", "/proc"))
    boot = read_text(proc / "sys/kernel/random/boot_id").strip()
    record = {
        "version": 1,
        "time": datetime.now(timezone.utc).isoformat(),
        "monotonic_seconds": round(time.monotonic(), 3),
        "boot_id": boot if re.fullmatch(r"[0-9a-f-]{36}", boot) else None,
        **{key: value for key, value in vars(args).items() if value is not None},
        "resources": resource_snapshot(proc),
    }
    if args.event in {"launch_start", "launch_end", "complete"}:
        record["tmux"] = tmux_snapshot()
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return 0 if append_record(state / "codexfarm/diagnostics", record) else 1
