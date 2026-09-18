"""Small, bounded host/backup checks shared by the annotator, doctor and restore."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def state_directory() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codexfarm"


def config_directory() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "codexfarm"


def number(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


@dataclass(frozen=True)
class Limits:
    warning_percent: float = 20
    critical_percent: float = 10
    session_mib: float = 1024

    @classmethod
    def from_env(cls) -> Limits:
        result = cls(
            number("CODEXFARM_MEMORY_WARN_PERCENT", 20),
            number("CODEXFARM_MEMORY_CRITICAL_PERCENT", 10),
            number("CODEXFARM_MEMORY_SESSION_MIB", 1024),
        )
        if not 0 < result.critical_percent < result.warning_percent < 100:
            raise ValueError("memory percentages must satisfy 0 < critical < warning < 100")
        return result


@dataclass(frozen=True)
class Memory:
    total_mib: float
    available_mib: float
    swap_used_mib: float
    pressure_percent: float = 0

    def level(self, limits: Limits) -> str:
        available = 100 * self.available_mib / self.total_mib
        if available <= limits.critical_percent or self.pressure_percent >= 25:
            return "critical"
        if available <= limits.warning_percent or self.pressure_percent >= 10:
            return "warning"
        return "ok"

    def description(self) -> str:
        return (
            f"{self.available_mib:.0f}/{self.total_mib:.0f} MiB RAM available, "
            f"{self.swap_used_mib:.0f} MiB swap used, "
            f"memory stalls {self.pressure_percent:.1f}%"
        )


def read_memory(proc: Path | None = None) -> Memory | None:
    proc = proc or Path(os.environ.get("CODEX_PROC_ROOT", "/proc"))
    try:
        fields = {}
        for line in (proc / "meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            fields[key] = int(value.split()[0]) / 1024
        total, available = fields["MemTotal"], fields["MemAvailable"]
        if total <= 0 or not 0 <= available <= total:
            return None
    except (OSError, ValueError, KeyError, IndexError):
        return None
    pressure = 0.0
    try:
        for line in (proc / "pressure/memory").read_text().splitlines():
            if line.startswith("some "):
                pressure = float(dict(item.split("=", 1) for item in line.split()[1:])["avg10"])
                if not math.isfinite(pressure) or not 0 <= pressure <= 100:
                    pressure = 0.0
    except (OSError, ValueError, KeyError):
        pass
    return Memory(
        total, available, max(0, fields.get("SwapTotal", 0) - fields.get("SwapFree", 0)), pressure
    )


def tree_memory(processes: str, roots: dict[str, set[int]]) -> dict[str, float]:
    """RSS attribution only: shared pages may count twice, so never use for host capacity."""
    children: dict[int, set[int]] = {}
    rss: dict[int, int] = {}
    for line in processes.splitlines():
        try:
            pid, parent, kib = map(int, line.split())
        except ValueError:
            continue
        rss[pid] = max(0, kib)
        children.setdefault(parent, set()).add(pid)
    totals = {}
    for window, pids in roots.items():
        seen: set[int] = set()
        pending = list(pids)
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            pending.extend(children.get(pid, ()))
        totals[window] = sum(rss.get(pid, 0) for pid in seen) / 1024
    return totals


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def age(value: object, now: float) -> float:
    try:
        stamp = float(value)
        if math.isfinite(stamp) and 0 < stamp <= now + 60:
            return max(0, now - stamp)
    except (TypeError, ValueError):
        pass
    return math.inf


def backup_issues(state: Path, now: float, *, expected: bool) -> list[str]:
    status = read_json(state / "backup-status.json")
    if not status:
        return ["no successful backup recorded; run codex-backup"] if expected else []
    issues = []
    if age(status.get("checked_at"), now) > 900:
        issues.append("backup scheduler heartbeat older than 15 minutes")
    if status.get("manifest_save_ok") is not True:
        issues.append("latest exact session manifest save failed or was skipped")
    if status.get("backup_ok") is not True:
        issues.append("latest conversation backup failed")
    destination = Path(str(status.get("destination", state / "backups")))
    latest = read_json(destination / "latest.json")
    archive = destination / Path(str(latest.get("archive", "missing"))).name
    if not archive.is_file() or archive.is_symlink():
        issues.append("conversation backup archive is missing")
    elif age(latest.get("created_at"), now) > 7200:
        issues.append("conversation backup older than 2 hours")
    return issues


def write_status(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class HealthMonitor:
    """Run at most every 15s; warn after two readings, immediately on critical pressure."""

    def __init__(self, state: Path, limits: Limits, *, status_enabled: bool = True):
        self.state, self.limits, self.status_enabled = state, limits, status_enabled
        self.last_sample = -math.inf
        self.last_notice = -math.inf
        self.previous: set[str] = set()
        self.active: set[str] = set()

    def tick(self, window_ids: set[str], tmux, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self.last_sample < 15:
            return
        self.last_sample = now
        memory = read_memory()
        roots: dict[str, set[int]] = {}
        sessions: set[str] = set()
        panes = tmux(["tmux", "list-panes", "-a", "-F", "#{session_id}\t#{window_id}\t#{pane_pid}"])
        for line in (panes or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1] in window_ids and parts[2].isdigit():
                sessions.add(parts[0])
                roots.setdefault(parts[1], set()).add(int(parts[2]))
        try:
            processes = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,rss="],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            totals = tree_memory(processes, roots)
        except (OSError, subprocess.SubprocessError):
            totals = {}
        observed: set[str] = set()
        level = memory.level(self.limits) if memory else "unavailable"
        if level in {"warning", "critical"}:
            observed.add("host")
        for window, mib in totals.items():
            tmux(
                ["tmux", "set-option", "-w", "-t", window, "@codexfarm_memory_mib", str(round(mib))]
            )
            if mib >= self.limits.session_mib:
                observed.add(window)
        active = observed & self.previous
        if level == "critical":
            active.add("host")
        self.previous = observed
        issues = backup_issues(
            self.state, now, expected=(config_directory() / "farms.tsv").exists()
        )
        if issues:
            active.add("backup")
        labels = []
        if "host" in active:
            labels.append("RAM CRITICAL" if level == "critical" else "RAM LOW")
        large = sorted((wid for wid in totals if wid in active), key=totals.get, reverse=True)
        if large:
            labels.append(
                "LARGE CHAT " + ",".join(f"{wid}:{totals[wid]:.0f}MiB" for wid in large[:3])
            )
        if issues:
            labels.append("BACKUP WARNING")
        label = " | ".join(labels)
        signature = active | ({"critical"} if level == "critical" else set())
        notify = bool(active) and (signature != self.active or now - self.last_notice >= 300)
        for session in sessions:
            tmux(["tmux", "set-option", "-t", session, "@codexfarm_health", label])
            if self.status_enabled:
                current = tmux(["tmux", "show-options", "-v", "-t", session, "status-right"])
                if current is not None and "#{@codexfarm_health}" not in current:
                    prefix = "#{?@codexfarm_health,#[fg=yellow]#{@codexfarm_health}#[default] ,}"
                    tmux(
                        [
                            "tmux",
                            "set-option",
                            "-t",
                            session,
                            "status-right",
                            prefix + current.rstrip("\n"),
                        ]
                    )
            if notify:
                detail = memory.description() if "host" in active and memory else ""
                message = "Farm: " + label + ". " + detail
                if issues:
                    message += ". " + issues[0] + "; run codex-doctor"
                tmux(["tmux", "display-message", "-d", "10000", "-t", session, message])
        if notify:
            self.last_notice = now
        self.active = signature
        write_status(
            self.state / "health-status.json",
            {
                "checked_at": now,
                "pid": os.getpid(),
                "memory": asdict(memory) if memory else None,
                "level": level,
                "windows_mib": totals,
                "warnings": labels,
                "backup_issues": issues,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Report farm memory, backup and monitor health.")
    parser.add_argument(
        "--restore-check", action="store_true", help="warn on low RAM; exit 3 at critical pressure"
    )
    args = parser.parse_args()
    try:
        limits = Limits.from_env()
    except ValueError as error:
        parser.error(str(error))
    memory = read_memory()
    level = memory.level(limits) if memory else "unavailable"
    if args.restore_check:
        if level in {"warning", "critical"}:
            print(f"Memory {level}: {memory.description()}")
        elif memory is None:
            print("Memory check unavailable on this host; restore cannot enforce a RAM guard.")
        return 3 if level == "critical" else 0
    print(
        f"Memory {level}: {memory.description() if memory else 'Linux memory counters unavailable'}"
    )
    state, now = state_directory(), time.time()
    issues = backup_issues(state, now, expected=(config_directory() / "farms.tsv").exists())
    health = read_json(state / "health-status.json")
    if (state / "managed_sessions").exists() or health:
        if age(health.get("checked_at"), now) > 60:
            issues.append("memory monitor heartbeat missing or older than 60 seconds")
        else:
            windows = health.get("windows_mib", {})
            if not isinstance(windows, dict):
                issues.append("invalid memory monitor state; restart codex-annotator")
                windows = {}
            for window, mib in windows.items():
                if float(mib) >= limits.session_mib:
                    issues.append(f"large chat process tree {window}: {float(mib):.0f} MiB RSS")
    for issue in issues:
        print(f"[WARN] {issue}")
    if not issues:
        print("No backup or monitor faults recorded.")
    return int(bool(issues) or level in {"warning", "critical"})
