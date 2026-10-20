"""Opt-in thermal telemetry and reversible, identity-checked batch CPU budgets.

The Windows bridge writes sensor data; this module never starts privileged
software. Interactive agents are excluded from admission and remedies.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .component_versions import GUARD_FILES, fingerprint
from .health import config_directory, state_directory
from .resource_config import read_private_json, write_private_json
from .resource_policy import finite_number

GUARD_VERSION = "2"
IMPLEMENTATION = fingerprint(GUARD_FILES)
MAX_REMEDIES = 64
SENSOR_VERSION = "3"
COOL_GUID = "0f5a6bd1-cc7e-4635-8bdd-09d0b13df1a8"


@dataclass(frozen=True)
class ThermalSettings:
    enabled: bool = False
    enforce: bool = False
    sensor_file: str = ""
    warning_c: float = 80
    queue_c: float = 85
    critical_c: float = 90
    recovery_c: float = 75
    recovery_seconds: float = 60
    batch_budget_percent: float = 200
    max_sample_age: float = 15
    guard_max_age: float = 30
    sensor_loss_policy: str = "retain"
    sensor_loss_grace_seconds: float = 30
    history_hours: float = 0
    history_max_bytes: int = 1048576

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.enforce) is not bool:
            raise ValueError("thermal enable/enforcement must be boolean")
        if not isinstance(self.sensor_file, str) or (
            self.sensor_file and not Path(self.sensor_file).is_absolute()
        ):
            raise ValueError("thermal sensor file must be an absolute path")
        if self.enabled and not self.sensor_file:
            raise ValueError("enabled thermal monitoring requires a sensor file")
        for key in ("warning_c", "queue_c", "critical_c", "recovery_c"):
            value = getattr(self, key)
            if not finite_number(value) or not 0 <= value <= 120:
                raise ValueError("thermal thresholds must be finite Celsius within 0..120")
        if not self.recovery_c < self.warning_c < self.queue_c < self.critical_c:
            raise ValueError("require recovery < warning < queue < critical temperatures")
        if self.sensor_loss_policy not in ("retain", "budget"):
            raise ValueError("sensor loss policy must be retain or budget")
        if (
            type(self.history_max_bytes) is not int
            or not 4096 <= self.history_max_bytes <= 16777216
        ):
            raise ValueError("thermal history byte limit must be within 4096..16777216")
        for key, lower, upper in (
            ("recovery_seconds", 1, 3600),
            ("batch_budget_percent", 1, 10000),
            ("max_sample_age", 1, 60),
            ("guard_max_age", 10, 120),
            ("sensor_loss_grace_seconds", 5, 3600),
            ("history_hours", 0, 168),
        ):
            if not finite_number(getattr(self, key)) or not lower <= getattr(self, key) <= upper:
                raise ValueError(f"invalid thermal {key}")


def settings_path() -> Path:
    return config_directory() / "thermal-settings.json"


def load_settings() -> ThermalSettings:
    try:
        settings_path().lstat()
        value = read_private_json(settings_path())
    except FileNotFoundError:
        return ThermalSettings()
    if set(value) - set(ThermalSettings.__dataclass_fields__):
        raise ValueError("unknown thermal settings fields")
    return ThermalSettings(**value)


def prepare_private_directory(path: Path) -> None:
    """Secure only the owned farm directory after an explicit enable choice."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
            raise ValueError("thermal storage directory must be owned and not a symlink")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def save_settings(settings: ThermalSettings) -> None:
    prepare_private_directory(settings_path().parent)
    write_private_json(settings_path(), asdict(settings))


def settings_fingerprint(settings: ThermalSettings) -> str:
    return hashlib.sha256(json.dumps(asdict(settings), sort_keys=True).encode()).hexdigest()[:16]


def read_temperature(settings: ThermalSettings, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not settings.enabled:
        return {"level": "disabled", "cpu_celsius": None}
    try:
        value = read_windows_json(Path(settings.sensor_file))
        if not isinstance(value, dict):
            raise ValueError("temperature sample must be an object")
        stamp, cpu = value["checked_at"], value["cpu_celsius"]
        if value.get("source") != "LibreHardwareMonitor" or not finite_number(stamp):
            raise ValueError("invalid temperature source/timestamp")
        sample_age = now - stamp
        result = {"sample_at": stamp, "sample_age_seconds": max(0, sample_age)}
        if not -5 <= sample_age <= settings.max_sample_age:
            return {**result, "level": "unavailable", "cpu_celsius": None, "reason": "stale_sample"}
        if not finite_number(cpu) or not 0 < cpu <= 130:
            return {
                **result,
                "level": "unavailable",
                "cpu_celsius": None,
                "reason": "sensor_unavailable",
            }
        level = (
            "critical"
            if cpu >= settings.critical_c
            else "hot"
            if cpu >= settings.queue_c
            else "warning"
            if cpu >= settings.warning_c
            else "ok"
        )
        return {**result, "level": level, "cpu_celsius": cpu}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return {"level": "unavailable", "cpu_celsius": None, "reason": "invalid_or_missing_sample"}


def sensor_digest() -> str:
    root = Path(__file__).resolve().parent.parent
    source = root / "tools/thermal-windows/sensor.ps1"
    if not source.is_file():
        source = root / "codexfarm-thermal-windows/sensor.ps1"
    return hashlib.sha256(source.read_bytes()).hexdigest()


def read_windows_json(path: Path) -> dict:
    from .health import read_bounded

    raw = read_bounded(path)
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Windows telemetry receipt must be an object")
    return value


def cooling_health(settings: ThermalSettings, now: float, root: Path) -> dict:
    try:
        consent = read_private_json(root / "thermal-setup.json")
    except FileNotFoundError:
        return {"health": "disabled", "expected": False}
    if consent.get("cooling_approved") is not True:
        return {"health": "disabled", "expected": False}
    try:
        value = read_windows_json(Path(settings.sensor_file).with_name("cooling-status.json"))
        stamp = value.get("checked_at")
        if (
            value.get("schema_version") != 1
            or value.get("source") != "CodexFarmWindows"
            or not finite_number(stamp)
            or not -5 <= now - stamp <= 120
            or value.get("state") != "checked"
        ):
            raise ValueError("cooling status unavailable")
        verified = value.get("active_guid") == COOL_GUID and value.get("verified") is True
        return {
            "expected": True,
            "health": "healthy" if verified else "degraded",
            "verified": verified,
            "checked_at": stamp,
        }
    except (OSError, ValueError, TypeError):
        return {"health": "unavailable", "expected": True, "verified": False}


def guard_identity() -> dict:
    from .resource_jobs import process_identity

    identity = process_identity(os.getpid()) or {}
    return {"pid": os.getpid(), "start_ticks": identity.get("start_ticks"), "boot_id": boot_id()}


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def guard_alive(value: dict) -> bool:
    from .resource_jobs import process_identity

    if not isinstance(value, dict):
        return False
    identity = process_identity(value.get("pid"))
    return bool(
        identity
        and identity["uid"] == os.getuid()
        and value.get("boot_id") == boot_id()
        and identity["start_ticks"] == value.get("start_ticks")
    )


def protection_status(
    settings: ThermalSettings | None = None,
    now: float | None = None,
    *,
    root: Path | None = None,
    monotonic_now: float | None = None,
) -> dict:
    """Read bounded local data only; a configured flag is not effective protection."""
    settings = load_settings() if settings is None else settings
    wall = time.time() if now is None else now
    moment = (time.monotonic() if now is None else now) if monotonic_now is None else monotonic_now
    sample = read_temperature(settings, wall)
    result = {
        **sample,
        "schema_version": 1,
        "configured_enforcement": settings.enabled and settings.enforce,
        "effective_enforcement": False,
        "guard_ready": False,
        "issues": [],
        "issue_codes": [],
        "health": "disabled",
        "guard": "disabled",
        "guard_version": GUARD_VERSION,
        "implementation": IMPLEMENTATION,
    }
    if not settings.enabled:
        return result

    def issue(code, message):
        result["issue_codes"].append(code)
        result["issues"].append(message)

    if sample["level"] == "unavailable":
        issue("sensor_unavailable", "thermal sensor unavailable or stale")
    elif sample["level"] != "ok":
        issue("cpu_" + sample["level"], "CPU temperature " + sample["level"])
    try:
        cooling = cooling_health(settings, wall, root or state_directory())
        result["cooling"] = cooling
        if cooling["health"] not in ("healthy", "disabled"):
            issue(
                "cooling_" + cooling["health"],
                "approved Windows cooling profile changed or its status is unavailable; operator choice preserved",
            )
        raw_sensor = read_windows_json(Path(settings.sensor_file))
        if raw_sensor.get("helper_version") is not None and (
            raw_sensor.get("helper_version") != SENSOR_VERSION
            or raw_sensor.get("helper_sha256") != sensor_digest()
        ):
            issue(
                "sensor_upgrade_needed",
                "Windows sensor helper is older than the installed code; refresh thermal setup",
            )
        if raw_sensor.get("helper_version") == SENSOR_VERSION:
            try:
                watchdog = read_windows_json(
                    Path(settings.sensor_file).with_name("watchdog-status.json")
                )
                checked = watchdog.get("checked_at")
                if (
                    watchdog.get("schema_version") != 1
                    or not finite_number(checked)
                    or not -5 <= wall - checked <= 180
                    or watchdog.get("state")
                    not in ("healthy", "starting", "cooldown", "recovery-requested")
                ):
                    raise ValueError("sensor watchdog unavailable")
                result["sensor_recovery"] = "healthy"
            except (OSError, ValueError, TypeError):
                result["sensor_recovery"] = "unavailable"
                issue(
                    "sensor_recovery_unavailable",
                    "Windows sensor recovery watchdog is unavailable or disabled",
                )
    except (OSError, ValueError, TypeError):
        if sample["level"] != "unavailable":
            issue("thermal_receipt_invalid", "thermal helper or cooling consent receipt is invalid")
    if settings.enforce:
        try:
            guard = read_private_json((root or state_directory()) / "thermal-status.json")
            if guard.get("schema_version") != 1 or guard.get("guard_version") != GUARD_VERSION:
                raise ValueError("thermal guard version is incompatible; refresh the guard")
            if guard.get("implementation") != IMPLEMENTATION:
                raise ValueError("thermal guard is running older code; refresh the guard")
            checked, monotonic_at = guard.get("checked_at"), guard.get("monotonic_at")
            if (
                not finite_number(checked)
                or not finite_number(monotonic_at)
                or not -5 <= wall - checked <= settings.guard_max_age
                or not 0 <= moment - monotonic_at <= settings.guard_max_age
            ):
                raise ValueError("thermal guard heartbeat is stale")
            if not guard_alive(guard.get("identity", {})):
                raise ValueError("thermal guard process identity is unavailable")
            if guard.get("settings_fingerprint") != settings_fingerprint(settings):
                raise ValueError("thermal guard has not applied the current configuration")
            if guard.get("configured_enforcement") is not True:
                raise ValueError("thermal guard is not in enforcement mode")
            failures = guard.get("issues")
            if (
                not isinstance(failures, list)
                or len(failures) > 32
                or not all(isinstance(x, str) and len(x) <= 160 for x in failures)
            ):
                raise ValueError("thermal guard status is malformed")
            coverage = guard.get("coverage")
            if (
                not isinstance(coverage, dict)
                or set(coverage) != {"eligible", "verified", "capped", "complete"}
                or type(coverage["complete"]) is not bool
                or any(
                    type(coverage[key]) is not int or not 0 <= coverage[key] <= 512
                    for key in ("eligible", "verified", "capped")
                )
                or not coverage["capped"] <= coverage["verified"] <= coverage["eligible"]
            ):
                raise ValueError("thermal guard coverage status is malformed")
            ready = not failures and coverage["complete"]
            result["guard"] = "healthy" if ready else "degraded"
            result["guard_ready"] = ready
            result["cooling_jobs"] = guard.get("cooling_jobs", 0)
            result["coverage"] = coverage
            if guard.get("history_issue"):
                issue("history_unavailable", "optional aggregate resource history is unavailable")
            if not ready:
                issue(
                    "guard_degraded",
                    "thermal guard reports incomplete enforcement; run codex-thermal status",
                )
        except FileNotFoundError:
            result["guard"] = "unavailable"
            issue("guard_unavailable", "thermal guard heartbeat is missing")
        except (OSError, ValueError, TypeError) as exc:
            result["guard"] = "unavailable"
            # Errors are deliberately fixed diagnostics, not arbitrary private file contents.
            message = str(exc)
            if not message.startswith("thermal guard"):
                message = "thermal guard status is inaccessible or invalid"
            issue("guard_unavailable", message)
    result["health"] = "degraded" if result["issues"] else "healthy"
    result["effective_enforcement"] = bool(
        settings.enforce and result["guard_ready"] and sample["cpu_celsius"] is not None
    )
    return result


def admission_reason() -> str | None:
    settings = load_settings()
    if not settings.enabled or not settings.enforce:
        return None
    status = protection_status(settings)
    if status["cpu_celsius"] is None:
        return "thermal sensor unavailable; waiting for fresh CPU temperature"
    if not status["guard_ready"]:
        return "thermal guard unavailable or degraded; waiting for verified protection"
    if status["cpu_celsius"] >= settings.queue_c:
        return "CPU hot; waiting before starting a managed batch job"
    return None


def enforcing() -> bool:
    settings = load_settings()
    return settings.enabled and settings.enforce


def quota_us(value: str) -> float | None:
    value = value.strip()
    if value == "infinity":
        return math.inf
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s)?", value)
    if not match:
        return None
    result = float(match[1]) * {None: 1, "us": 1, "ms": 1000, "s": 1000000}[match[2]]
    return result if math.isfinite(result) and result > 0 else None


def manager(args: list[str], *, timeout: float = 2) -> subprocess.CompletedProcess:
    from .resource_jobs import manager_call

    try:
        return manager_call(args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(
            args, 1, "", "manager unavailable or time budget exhausted"
        )


def precheck_service() -> Path:
    """Check masks/manager without writing units, settings, or host state."""
    unit = "codex-thermal.service"
    config = config_directory().parent
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    locations = [
        config_directory().parent / "systemd/user",
        config / "systemd/user.control",
        runtime / "systemd/user",
        runtime / "systemd/user.control",
        runtime / "systemd/transient",
        runtime / "systemd/generator.early",
        runtime / "systemd/generator",
        runtime / "systemd/generator.late",
        data / "systemd/user",
        Path("/etc/systemd/user"),
        Path("/run/systemd/user"),
        Path("/usr/local/lib/systemd/user"),
        Path("/usr/lib/systemd/user"),
        Path("/lib/systemd/user"),
    ]
    locations.extend(
        Path(path) for path in os.environ.get("SYSTEMD_UNIT_PATH", "").split(":") if path
    )
    for key, default in (
        ("XDG_CONFIG_DIRS", "/etc/xdg"),
        ("XDG_DATA_DIRS", "/usr/local/share:/usr/share"),
    ):
        locations.extend(
            Path(path) / "systemd/user" for path in os.environ.get(key, default).split(":") if path
        )
    for directory in locations:
        candidate = directory / unit
        if candidate.exists() and (
            candidate.resolve() == Path("/dev/null")
            or (candidate.is_file() and candidate.stat().st_size == 0)
        ):
            raise ValueError(f"thermal service is masked at {candidate}; mask preserved")
    state = manager(["show", unit, "--property=LoadState", "--value"])
    if "masked" in state.stdout:
        raise ValueError("thermal service is masked; mask preserved")
    if manager(["show-environment"]).returncode:
        raise ValueError("user service manager unavailable; no service files written")
    return locations[0] / unit


def install_service() -> None:
    path = precheck_service()
    unit = path.name

    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    helper = Path(__file__).resolve().parent.parent / "bin/codex-thermal"
    if not helper.exists():
        helper = Path(__file__).resolve().parent.parent / "codex-thermal"
    content = (
        "[Unit]\nDescription=Opt-in Codex farm thermal safeguards\n"
        "StartLimitIntervalSec=0\n\n[Service]\nType=notify\nNotifyAccess=main\n"
        "WatchdogSec=30\nWatchdogSignal=SIGTERM\nLimitCORE=0\nTimeoutStartSec=30\n"
        f"ExecStart={quote(sys.executable)} {quote(helper)} monitor\n"
        f"Environment={quote('XDG_CONFIG_HOME=' + str(config_directory().parent))}\n"
        f"Environment={quote('XDG_STATE_HOME=' + str(state_directory().parent))}\n"
        f"Environment={quote('PATH=' + os.environ.get('PATH', os.defpath))}\n"
        "UMask=0077\nRestart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codex-thermal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    for args in (["daemon-reload"], ["enable", unit], ["restart", unit]):
        result = manager(args, timeout=20)
        if result.returncode:
            raise ValueError("thermal service installation failed: " + result.stderr.strip())
    settings = load_settings()
    if settings.enabled and settings.enforce:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = protection_status(settings)
            if status["guard_ready"] and status["cpu_celsius"] is not None:
                return
            time.sleep(0.25)
        raise ValueError(
            "thermal service installed but protection did not become ready; run codex-thermal status"
        )


class Controller:
    def __init__(self, root: Path | None = None):
        self.root = root or state_directory()
        self.path = self.root / "thermal-remedies.json"
        self.cool_since = None
        self.lost_since = None
        self.generation = secrets.token_hex(16)
        self._deadline = math.inf
        self.cursor = 0
        self.history_retry_after = 0
        self.history_issue = None
        from .resource_history import ResourceHistory

        self.history = ResourceHistory(self.root)
        try:
            self.entries = read_private_json(self.path)
        except FileNotFoundError:
            self.entries = {}
        if len(self.entries) > MAX_REMEDIES:
            raise ValueError("thermal remedy journal exceeds 64 jobs")
        for entry in self.entries.values():
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("identity"), dict)
                or not finite_number(entry.get("applied_us"))
                or entry["applied_us"] <= 0
                or (
                    entry.get("original_us") is not None
                    and (not finite_number(entry["original_us"]) or entry["original_us"] <= 0)
                )
                or type(entry.get("pending", False)) is not bool
            ):
                raise ValueError("invalid thermal rollback journal")

    def _read_quota(self, scope: str) -> float | None:
        result = manager(["show", scope, "--property=CPUQuotaPerSecUSec", "--value"])
        return quota_us(result.stdout) if result.returncode == 0 else None

    def _set_quota(self, store, job_id: str, identity: dict, percent: float | None) -> bool:
        # Validation includes actual UID, process start ticks, exact cgroup and
        # scope generation. Never discover remedy targets by executable name.
        if not store.validate(job_id, identity, require_batch=True) or not identity.get("scope"):
            return False
        prop = "CPUQuota=" + (f"{percent:.6f}%" if percent is not None else "")
        if manager(["set-property", "--runtime", identity["scope"], prop]).returncode:
            return False
        from .resource_jobs import kernel_limits_effective

        return kernel_limits_effective(
            identity, cpu_quota=percent, require_cpu=True, unlimited_cpu=percent is None
        )

    def release(self, store) -> None:
        for job_id, entry in list(self.entries.items()):
            if time.monotonic() >= self._deadline:
                break
            identity = entry["identity"]
            if not store.validate(job_id, identity, require_batch=True):
                # Finished jobs need no rollback. Unreadable/live identities
                # retain their journal, so temporary manager failures recover.
                try:
                    record = store.read(job_id)
                    from .resource_jobs import _recorded_process_state

                    if all(
                        _recorded_process_state(record, name) in {"dead", "absent"}
                        for name in ("supervisor", "payload", "launch")
                    ):
                        del self.entries[job_id]
                except (OSError, ValueError):
                    pass
                continue
            current = self._read_quota(identity["scope"])
            if current is None:
                continue
            if abs(current - entry["applied_us"]) > 1:
                # Preserve a later operator change, including stronger limits.
                del self.entries[job_id]
                continue
            original = entry["original_us"]
            if self._set_quota(
                store, job_id, identity, None if original is None else original / 10000
            ):
                del self.entries[job_id]

    def tick(self, settings: ThermalSettings, store, now: float | None = None) -> dict:
        from .resource_jobs import manager_budget

        self._deadline = time.monotonic() + 2
        with manager_budget(2):
            return self._tick(settings, store, now)

    def _tick(self, settings: ThermalSettings, store, now: float | None = None) -> dict:
        moment = time.monotonic() if now is None else now
        now = time.time() if now is None else now
        sample = read_temperature(settings, now)
        issues = []
        coverage = {"eligible": 0, "verified": 0, "capped": 0, "complete": True}
        if self.cool_since is not None and moment < self.cool_since:
            self.cool_since = None
        if sample["cpu_celsius"] is None:
            if self.lost_since is None or moment < self.lost_since:
                self.lost_since = moment
        else:
            self.lost_since = None
        loss_budget = (
            settings.sensor_loss_policy == "budget"
            and self.lost_since is not None
            and moment - self.lost_since >= settings.sensor_loss_grace_seconds
        )
        if not settings.enabled or not settings.enforce:
            self.release(store)
        else:
            if sample["cpu_celsius"] is not None and sample["cpu_celsius"] < settings.recovery_c:
                if self.cool_since is None:
                    self.cool_since = moment
                if moment - self.cool_since >= settings.recovery_seconds:
                    self.release(store)
                    if self.entries:
                        issues.append("thermal rollback is pending verification")
            else:
                self.cool_since = None
            from .resource_jobs import MAX_JOBS, _recorded_process_state

            try:
                inventory = store.list_jobs(limit=MAX_JOBS)
            except (OSError, ValueError):
                inventory = []
                issues.append("batch registry unavailable")
                coverage["complete"] = False
            if len(inventory) >= MAX_JOBS:
                issues.append("batch inventory limit reached")
                coverage["complete"] = False
            jobs = []
            for job in inventory:
                if job["role"] != "batch":
                    continue
                payload = _recorded_process_state(job, "payload")
                if payload == "dead":
                    continue
                if job["status"] == "running" or payload in ("live", "unknown"):
                    jobs.append(job)
            coverage["eligible"] = len(jobs)
            if len(jobs) > MAX_REMEDIES:
                issues.append("thermal batch capacity exceeded")
                coverage["complete"] = False
            protect = (
                sample["level"] == "critical"
                or (bool(self.entries) and sample["cpu_celsius"] is not None)
                or loss_budget
            )
            # Rotate so one slow scope cannot indefinitely starve later jobs.
            offset = self.cursor % max(1, len(jobs))
            jobs = jobs[offset:] + jobs[:offset]
            processed = 0
            percent = settings.batch_budget_percent / max(1, len(jobs))
            for job in jobs:
                if time.monotonic() >= self._deadline:
                    issues.append("thermal pass time budget exhausted")
                    coverage["complete"] = False
                    break
                processed += 1
                job_id = job["job_id"]
                try:
                    identity = store.identity(job_id)
                    valid = bool(identity.get("scope")) and store.validate(
                        job_id, identity, require_batch=True
                    )
                except (OSError, ValueError, TypeError):
                    valid = False
                if not valid:
                    issues.append("batch identity unavailable")
                    coverage["complete"] = False
                    continue
                coverage["verified"] += 1
                if protect:
                    current = self._read_quota(identity["scope"])
                    if current is None:
                        issues.append("batch CPU quota unavailable")
                        coverage["complete"] = False
                        continue
                    previous = self.entries.get(job_id)
                    if not previous and len(self.entries) >= MAX_REMEDIES:
                        issues.append("thermal remedy journal is full")
                        coverage["complete"] = False
                        continue
                    if previous:
                        original = previous["original_us"]
                        unchanged_original = (
                            math.isinf(current)
                            if original is None
                            else abs(current - original) <= 1
                        )
                        if previous["identity"] != identity or (
                            abs(current - previous["applied_us"]) > 1
                            and not (previous.get("pending") and unchanged_original)
                        ):
                            if current <= percent * 10000:
                                # Ownership of this limit passed to the operator;
                                # never later restore over the stronger choice.
                                del self.entries[job_id]
                                coverage["capped"] += 1
                                continue
                            issues.append("operator CPU change preserved")
                            coverage["complete"] = False
                            continue
                    target = min(current, percent * 10000)
                    if target >= current:
                        coverage["capped"] += 1
                        continue
                    # Persist rollback BEFORE applying a reversible remedy.
                    self.entries[job_id] = {
                        "identity": identity,
                        "original_us": previous["original_us"]
                        if previous
                        else (None if math.isinf(current) else current),
                        "applied_us": target,
                        "pending": True,
                    }
                    write_private_json(self.path, self.entries)
                    if not self._set_quota(store, job_id, identity, target / 10000):
                        issues.append("batch CPU budget could not be applied")
                        coverage["complete"] = False
                    else:
                        self.entries[job_id]["pending"] = False
                        coverage["capped"] += 1
            self.cursor = offset + max(1, processed)
        write_private_json(self.path, self.entries)
        status = {
            **sample,
            "checked_at": now,
            "schema_version": 1,
            "guard_version": GUARD_VERSION,
            "implementation": IMPLEMENTATION,
            "generation": self.generation,
            "identity": guard_identity(),
            "monotonic_at": moment,
            "settings_fingerprint": settings_fingerprint(settings),
            "configured_enforcement": settings.enabled and settings.enforce,
            "enforcing": settings.enabled
            and settings.enforce
            and sample["cpu_celsius"] is not None
            and not issues,
            "effective_enforcement": settings.enabled
            and settings.enforce
            and sample["cpu_celsius"] is not None
            and not issues,
            "sensor_loss_budget_active": loss_budget,
            "coverage": coverage,
            "cooling_jobs": len(self.entries),
            "issues": sorted(set(issues)),
        }
        write_private_json(self.root / "thermal-status.json", status)
        # Status and rollback always precede optional history. History failure must
        # not terminate enforcement or produce a retry/write storm.
        if moment >= self.history_retry_after:
            try:
                self.history.record(
                    status, hours=settings.history_hours, max_bytes=settings.history_max_bytes
                )
                self.history_issue = None
            except (OSError, ValueError, TypeError):
                self.history_retry_after = moment + 60
                self.history_issue = "aggregate history unavailable; existing file preserved"
        if self.history_issue:
            status["history_issue"] = self.history_issue
            write_private_json(self.root / "thermal-status.json", status)
        return status


def notify_watchdog() -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.settimeout(0.1)
            notifier.sendto(b"READY=1\nWATCHDOG=1", address)
    except OSError:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    commands.add_parser("monitor")
    commands.add_parser("release")
    commands.add_parser("install-service")
    config = commands.add_parser("configure")
    config.add_argument("--sensor-file", required=True)
    config.add_argument("--enforce", action="store_true")
    config.add_argument("--disable", action="store_true")
    config.add_argument(
        "--sensor-loss-policy",
        choices=("retain", "budget"),
        default=None,
        help="budget explicitly applies the configured batch CPU budget after sensor loss",
    )
    config.add_argument("--sensor-loss-grace-seconds", type=float, default=None)
    config.add_argument("--history-hours", type=float, default=None)
    config.add_argument("--history-max-bytes", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        if args.action == "configure":
            values = asdict(load_settings())
            values.update(
                enabled=not args.disable, enforce=args.enforce, sensor_file=args.sensor_file
            )
            for field in (
                "sensor_loss_policy",
                "sensor_loss_grace_seconds",
                "history_hours",
                "history_max_bytes",
            ):
                if getattr(args, field) is not None:
                    values[field] = getattr(args, field)
            settings = ThermalSettings(**values)
            print("Thermal plan: " + json.dumps(asdict(settings), sort_keys=True), flush=True)
            save_settings(settings)
            print(json.dumps(asdict(settings), indent=2))
            return 0
        settings = load_settings()
        if args.action == "install-service":
            install_service()
            print("Installed and enabled the per-user thermal service.")
            return 0
        if args.action == "status":
            journal = state_directory() / "thermal-remedies.json"
            try:
                journal.lstat()
                pending = len(read_private_json(journal))
            except FileNotFoundError:
                pending = 0
            status = protection_status(settings)
            print(
                json.dumps(
                    {
                        "settings": asdict(settings),
                        "protection": status,
                        "rollback_records": pending,
                    },
                    indent=2,
                )
            )
            return int(
                status["health"] not in ("healthy", "disabled")
                or (pending and not settings.enforce)
            )
        from .resource_jobs import JobStore

        prepare_private_directory(state_directory())
        lock_path = state_directory() / "thermal.lock"
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("thermal monitor is already running") from None
            controller = Controller()
            store = JobStore()
            if args.action == "release":
                controller.release(store)
                write_private_json(controller.path, controller.entries)
                return 0
            while True:
                controller.tick(load_settings(), store)
                notify_watchdog()
                time.sleep(5)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Thermal safeguard unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
