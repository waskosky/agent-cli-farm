"""Opt-in argv job runner and private, identity-checked batch remedy APIs.

All process selection uses recorded Linux identities. Public metadata intentionally
excludes argv, cwd and environment values. Remedies affect future recipe launches;
only an explicit, revalidated restart request can stop a consenting batch job.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .health import read_memory, state_directory
from .resource_config import (
    ResourceSettings,
    load_settings,
    private_directory,
    read_private_json,
    write_private_json,
)
from .resource_policy import HeadroomGate, finite_number

WORKER_VARIABLES = frozenset(
    {"CARGO_BUILD_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL", "OMP_NUM_THREADS", "UV_THREADPOOL_SIZE"}
)
IDENTITY_FIELDS = (
    "job_id",
    "uid",
    "supervisor_pid",
    "supervisor_start_ticks",
    "payload_pid",
    "payload_start_ticks",
    "launch_pid",
    "launch_start_ticks",
    "cgroup",
    "scope",
    "invocation_id",
    "restart_count",
)
ID_RE = re.compile(r"[a-f0-9]{32}\Z")
SCOPE_RE = re.compile(r"codexfarm-(agent|batch)-[a-f0-9]{32}\.scope\Z")
MAX_JOBS = 512
MAX_OVERRIDE_SECONDS = 3600
MAX_PROC_SCAN = 65536


def warn(message: str) -> None:
    print(f"codex-job: {message}", file=sys.stderr, flush=True)


def process_identity(pid: int, proc_root: Path = Path("/proc")) -> dict | None:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        folder = proc_root / str(pid)
        uid = folder.stat().st_uid
        raw = (folder / "stat").read_text()
        values = raw[raw.rindex(")") + 2 :].split()
        if values[0] == "Z":
            return None
        # Fields 3 onward; comm may itself contain spaces and parentheses.
        start_ticks = int(values[19])
        group, session = int(values[2]), int(values[3])
        cgroup = (folder / "cgroup").read_text().strip()
        return {
            "pid": pid,
            "uid": uid,
            "start_ticks": start_ticks,
            "pgid": group,
            "sid": session,
            "cgroup": cgroup,
        }
    except (OSError, ValueError, IndexError):
        return None


def _recorded_process_state(record: dict, name: str) -> str:
    """Distinguish definite death/reuse from unreadable Linux identity counters."""
    pid, ticks, uid = (
        record.get(f"{name}_pid"),
        record.get(f"{name}_start_ticks"),
        record.get("uid"),
    )
    if pid is None and ticks is None:
        return "absent"
    if type(pid) is not int or pid <= 0 or type(ticks) is not int or type(uid) is not int:
        return "unknown"
    identity = process_identity(pid)
    if identity is not None:
        if identity["start_ticks"] != ticks:
            return "dead"
        return "live" if identity["uid"] == uid else "unknown"
    try:
        (Path("/proc") / str(pid)).stat()
    except FileNotFoundError:
        return "dead"
    except OSError:
        return "unknown"
    # Existing PIDs with unreadable counters or a changed UID may still be the
    # original live process. Only disappearance or start-tick reuse proves death.
    return "unknown"


def manager_environment(env: dict[str, str], *, runtime_root: Path | None = None) -> dict[str, str]:
    result = dict(env)
    if "XDG_RUNTIME_DIR" in result and "DBUS_SESSION_BUS_ADDRESS" in result:
        return result
    runtime = Path(
        result.get("XDG_RUNTIME_DIR", str(runtime_root or Path("/run/user") / str(os.getuid())))
    )
    try:
        directory, bus = runtime.lstat(), (runtime / "bus").lstat()
        if directory.st_uid != os.getuid() or not stat.S_ISDIR(directory.st_mode):
            return result
        if bus.st_uid != os.getuid() or not stat.S_ISSOCK(bus.st_mode):
            return result
    except OSError:
        return result
    result.setdefault("XDG_RUNTIME_DIR", str(runtime))
    result.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime / 'bus'}")
    return result


def manager_call(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        env=manager_environment(os.environ if env is None else env),
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )


def scope_invocation(scope: str) -> str | None:
    if not SCOPE_RE.fullmatch(scope):
        return None
    try:
        result = manager_call(["show", scope, "--property=InvocationID", "--value"])
        value = result.stdout.strip()
        if value.startswith("InvocationID="):
            value = value.partition("=")[2]
        return value if result.returncode == 0 and value else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def recipe_fingerprint(argv: list[str], cwd: str, worker_env: str | None) -> str:
    data = json.dumps([argv, cwd, worker_env], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def validate_workers(worker_env: str | None, workers: int | None) -> None:
    if worker_env is None and workers is None:
        return
    if worker_env not in WORKER_VARIABLES or type(workers) is not int or not 1 <= workers <= 4096:
        raise ValueError("declare a known worker variable and integer --workers within 1..4096")


def _bounded_seconds(value: object) -> float:
    if not finite_number(value) or not 0 < value <= MAX_OVERRIDE_SECONDS:
        raise ValueError(
            f"temporary remedy duration must be within 0..{MAX_OVERRIDE_SECONDS} seconds"
        )
    return float(value)


class JobStore:
    """Private job recipes and remedy requests; callers must supply exact identity snapshots."""

    def __init__(self, path: Path | None = None):
        self.path = state_directory() / "resources/jobs" if path is None else Path(path)
        self.overrides_path = self.path.parent / "overrides"

    def _prepare(self) -> None:
        if self.path == state_directory() / "resources/jobs":
            private_directory(state_directory(), create=True)
        private_directory(self.path.parent, create=True)
        private_directory(self.path, create=True)
        private_directory(self.overrides_path, create=True)

    @contextmanager
    def _locked(self):
        self._prepare()
        lock = self.path / ".lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("unsafe job store lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _id(job_id: str) -> str:
        if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            raise ValueError("unknown job ID")
        return job_id

    def record_path(self, job_id: str) -> Path:
        return self.path / f"{self._id(job_id)}.json"

    def marker_path(self, job_id: str) -> Path:
        return self.path / f"{self._id(job_id)}.started.json"

    def request_path(self, job_id: str) -> Path:
        return self.path / f"{self._id(job_id)}.restart.json"

    def read(self, job_id: str) -> dict:
        try:
            value = read_private_json(self.record_path(job_id))
        except (FileNotFoundError, OSError) as exc:
            raise ValueError("unknown job ID") from exc
        if value.get("job_id") != job_id or value.get("uid") != os.getuid():
            raise ValueError("wrong job identity or owner")
        return value

    def write(self, record: dict) -> None:
        write_private_json(self.record_path(record["job_id"]), record)

    def update(self, job_id: str, **changes) -> dict:
        with self._locked():
            record = self.read(job_id)
            record.update(changes, updated_at=time.time())
            self.write(record)
            return record

    def create(
        self,
        role: str,
        argv: list[str],
        cwd: str,
        *,
        restartable: bool = False,
        worker_env: str | None = None,
        workers: int | None = None,
        managed: bool = False,
    ) -> dict:
        if (
            role not in ("agent", "batch")
            or type(restartable) is not bool
            or type(managed) is not bool
        ):
            raise ValueError("unknown role or restart consent")
        if role == "agent" and restartable:
            raise ValueError("interactive agents cannot be restartable")
        if not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
            raise ValueError("job argv must contain literal strings")
        validate_workers(worker_env, workers)
        supervisor = process_identity(os.getpid())
        if supervisor is None:
            raise ValueError("supervisor identity unavailable")
        self._prepare()
        self.cleanup()
        with self._locked():
            if len(self._records()) >= MAX_JOBS:
                raise ValueError("private job retention limit reached; clean finished records")
            now = time.time()
            value = {
                "job_id": secrets.token_hex(16),
                "uid": os.getuid(),
                "role": role,
                "managed_launch": managed,
                "supervisor_pid": os.getpid(),
                "supervisor_start_ticks": supervisor["start_ticks"],
                "payload_pid": None,
                "payload_start_ticks": None,
                "launch_pid": None,
                "launch_start_ticks": None,
                "cgroup": None,
                "scope": None,
                "invocation_id": None,
                "status": "queued",
                "argv": argv,
                "cwd": cwd,
                "restartable": restartable,
                "restart_count": 0,
                "worker_env": worker_env,
                "workers": workers,
                "recipe_fingerprint": recipe_fingerprint(argv, cwd, worker_env),
                "created_at": now,
                "updated_at": now,
            }
            self.write(value)
            return value

    def register_payload(self, job_id: str, pid: int, scope: str | None = None) -> dict:
        identity = process_identity(pid)
        if identity is None or identity["uid"] != os.getuid():
            raise ValueError("payload identity unavailable")
        invocation = scope_invocation(scope) if scope else None
        # Doubles may intentionally lack a real cgroup; such jobs remain runnable
        # but cannot pass validation for remedies when scope isolation is absent.
        return self.update(
            job_id,
            payload_pid=pid,
            payload_start_ticks=identity["start_ticks"],
            cgroup=identity["cgroup"],
            scope=scope,
            invocation_id=invocation,
            status="running",
        )

    def identity(self, job_id: str) -> dict:
        value = self.read(job_id)
        return {name: value.get(name) for name in IDENTITY_FIELDS}

    def public(self, job_id: str) -> dict:
        value = self.read(job_id)
        keys = (
            *IDENTITY_FIELDS,
            "role",
            "status",
            "restartable",
            "workers",
            "worker_env",
            "created_at",
            "updated_at",
        )
        return {name: value.get(name) for name in keys}

    def list_jobs(self, *, limit: int = 64) -> list[dict]:
        """Bounded public metadata for reports; no private argv/cwd/env values."""
        if type(limit) is not int or not 0 <= limit <= MAX_JOBS:
            raise ValueError("job listing limit must be an integer within 0..512")
        if not self.path.exists() or not limit:
            return []
        private_directory(self.path)
        return [self.public(path.stem) for path in sorted(self._records())[:limit]]

    def validate(self, job_id: str, expectedidentity: dict, *, require_batch: bool = False) -> bool:
        try:
            value = self.read(job_id)
            if not isinstance(expectedidentity, dict) or self.identity(job_id) != expectedidentity:
                return False
            if value["status"] != "running" or value["role"] not in ("agent", "batch"):
                return False
            if require_batch and value["role"] != "batch":
                return False
            if value.get("managed_launch") is True and not value.get("launch_pid"):
                return False
            names = (
                ("supervisor", "payload", "launch")
                if value.get("launch_pid")
                else ("supervisor", "payload")
            )
            for name in names:
                identity = process_identity(value[f"{name}_pid"])
                if (
                    identity is None
                    or identity["uid"] != value["uid"]
                    or identity["start_ticks"] != value[f"{name}_start_ticks"]
                ):
                    return False
                if name == "payload" and identity["cgroup"] != value["cgroup"]:
                    return False
            scope = value.get("scope")
            if scope:
                if not SCOPE_RE.fullmatch(scope) or scope not in value["cgroup"].split("/"):
                    return False
                if (
                    not value.get("invocation_id")
                    or scope_invocation(scope) != value["invocation_id"]
                ):
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _batch(self, job_id: str, expectedidentity: dict) -> dict:
        if not self.validate(job_id, expectedidentity, require_batch=True):
            raise ValueError("batch job identity is stale, unavailable, or unowned")
        return self.read(job_id)

    def request_restart(self, job_id: str, expectedidentity: dict) -> None:
        with self._locked():
            value = self._batch(job_id, expectedidentity)
            if value.get("restartable") is not True or value.get("restart_count") != 0:
                raise ValueError("batch restart requires unused explicit consent")
            if self.request_path(job_id).exists():
                raise ValueError("restart already requested")
            write_private_json(
                self.request_path(job_id),
                {"identity": expectedidentity, "requested_at": time.time()},
            )

    def reduce_workers(
        self,
        job_id: str,
        count: int,
        ttl_seconds: float = 300,
        expectedidentity: dict | None = None,
    ) -> str:
        ttl = _bounded_seconds(ttl_seconds)
        with self._locked():
            value = self._batch(job_id, expectedidentity)
            validate_workers(value.get("worker_env"), count)
            if value.get("workers") is None or count >= value["workers"]:
                raise ValueError("worker remedy must reduce the declared count")
            overrides = self._recipe_overrides_locked(value["recipe_fingerprint"])
            if "workers" in overrides and count > overrides["workers"]:
                raise ValueError("worker remedy cannot increase an existing reduction")
            return self._override(value, "workers", count, ttl)

    def defer_job(self, job_id: str, seconds: float, expectedidentity: dict) -> str:
        seconds = _bounded_seconds(seconds)
        with self._locked():
            value = self._batch(job_id, expectedidentity)
            return self._override(value, "defer", True, seconds)

    def _override(self, value: dict, kind: str, item: object, ttl: float) -> str:
        self._recipe_overrides_locked("")
        if len(list(self.overrides_path.glob("*.json"))[: MAX_JOBS + 1]) >= MAX_JOBS:
            raise ValueError("temporary override retention limit reached")
        key = secrets.token_hex(16)
        write_private_json(
            self.overrides_path / f"{key}.json",
            {
                "override_id": key,
                "recipe_fingerprint": value["recipe_fingerprint"],
                "kind": kind,
                "value": item,
                "expires_at": time.time() + ttl,
            },
        )
        return key

    def delete_override(self, override_id: str) -> None:
        override_id = self._id(override_id)
        with self._locked():
            path = self.overrides_path / f"{override_id}.json"
            try:
                read_private_json(path)
            except FileNotFoundError:
                return
            # Expiration or an operator's prior rollback is already complete.
            # Existing unsafe files/symlinks still fail the strict read above.
            path.unlink(missing_ok=True)

    rollback_override = delete_override

    def recipe_overrides(self, fingerprint: str) -> dict:
        if not self.overrides_path.exists():
            return {}
        with self._locked():
            return self._recipe_overrides_locked(fingerprint)

    def _recipe_overrides_locked(self, fingerprint: str) -> dict:
        """Read/expire overrides while the caller owns the store lock."""
        private_directory(self.overrides_path)
        result: dict = {}
        now = time.time()
        for path in sorted(self.overrides_path.glob("*.json"))[:MAX_JOBS]:
            try:
                value = read_private_json(path)
            except FileNotFoundError:
                # An operator may remove a private override without our lock.
                # Existing unsafe paths still fail strict validation.
                continue
            expiry = value.get("expires_at")
            if not finite_number(expiry):
                raise ValueError("invalid override expiry")
            if expiry <= now:
                path.unlink(missing_ok=True)
                continue
            if value.get("recipe_fingerprint") != fingerprint:
                continue
            if value.get("kind") == "workers":
                count = value.get("value")
                if type(count) is not int or not 1 <= count <= 4096:
                    raise ValueError("invalid worker override")
                result["workers"] = min(result.get("workers", count), count)
            elif value.get("kind") == "defer" and value.get("value") is True:
                result["defer_until"] = max(result.get("defer_until", expiry), expiry)
            else:
                raise ValueError("unknown recipe override")
        return result

    def _records(self) -> list[Path]:
        return [path for path in self.path.glob("*.json") if ID_RE.fullmatch(path.stem)][
            : MAX_JOBS + 1
        ]

    def cleanup(self, *, now: float | None = None) -> None:
        if not self.path.exists():
            return
        now = time.time() if now is None else now
        with self._locked():
            terminal = []
            for path in self._records():
                value = read_private_json(path)
                states = {
                    name: _recorded_process_state(value, name)
                    for name in ("supervisor", "payload", "launch")
                }
                if value.get("status") in ("finished", "failed", "queued_timeout", "stale"):
                    terminal.append((value.get("updated_at", 0), path))
                elif value.get("status") in ("queued", "running") and "dead" in states.values():
                    # Scope/cgroup validation can fail temporarily. Only actual
                    # PID death or start-tick reuse is stale; UID changes remain
                    # ambiguous while the original recorded process is alive.
                    value.update(status="stale", updated_at=now)
                    self.write(value)
            terminal.sort(reverse=True)
            for index, (updated_at, path) in enumerate(terminal):
                if index >= 128 or now - updated_at > 7 * 86400:
                    value = read_private_json(path)
                    # Terminal labels, even legacy stale labels, do not prove
                    # process death. Recheck every recorded identity at deletion.
                    if any(
                        _recorded_process_state(value, name) in ("live", "unknown")
                        for name in ("supervisor", "payload", "launch")
                    ):
                        continue
                    for suffix in (".json", ".started.json", ".restart.json"):
                        (self.path / (path.stem + suffix)).unlink(missing_ok=True)
            # Also clean expired overrides without exposing private recipes.
            self._recipe_overrides_locked("")


def memory_policy(explicit: str | None, role: str, settings: ResourceSettings) -> str:
    return explicit or ("queue" if role == "batch" and settings.queue_background else "warn")


def admit(
    role: str,
    policy: str,
    settings: ResourceSettings,
    timeout: float,
    store: JobStore,
    fingerprint: str,
    *,
    recovery: bool = False,
    gate: HeadroomGate | None = None,
) -> bool:
    if role == "agent" or policy == "ignore":
        return True
    gate = gate or HeadroomGate(settings.headroom())
    start = time.monotonic()
    if recovery:
        # Prime waiting regardless of current headroom: every restart requires a
        # fresh continuous recovery period. Unknown/tiny samples still fail open.
        from .health import Memory

        gate.sample(Memory(settings.recovery_mib * 2, 0, 0), start)
    announced = False
    while True:
        now = time.monotonic()
        allowed = gate.sample(read_memory(), now)
        overrides = store.recipe_overrides(fingerprint)
        deferred = overrides.get("defer_until", 0) > time.time()
        if allowed and not deferred:
            if "advisory" in gate.reason:
                warn(gate.reason)
            return True
        if policy == "warn" and not recovery and not deferred:
            warn(gate.reason + " Advisory launch; use --memory-policy queue to wait.")
            return True
        if not announced:
            warn("Waiting for job admission. Manual bypass: --memory-policy ignore.")
            announced = True
        if now - start >= timeout:
            warn("Queue timeout; payload was not launched. Manual bypass: --memory-policy ignore.")
            return False
        time.sleep(min(0.25, max(0, timeout - (now - start))))


def _safe_owned_group(record: dict) -> int:
    payload = process_identity(record["payload_pid"])
    supervisor = process_identity(record["supervisor_pid"])
    leader = process_identity(record.get("launch_pid") or record["payload_pid"])
    leader_ticks = record.get("launch_start_ticks") or record["payload_start_ticks"]
    if payload is None or supervisor is None or leader is None:
        raise ValueError("owned batch process group unavailable")
    if (
        record["role"] != "batch"
        or payload["uid"] != os.getuid()
        or leader["uid"] != os.getuid()
        or leader["pid"] == supervisor["pid"]
        or leader["pgid"] != leader["pid"]
        or leader["sid"] != leader["pid"]
        or payload["pgid"] != leader["pgid"]
        or payload["sid"] != leader["sid"]
        or leader["pgid"] == supervisor["pgid"]
        or payload["start_ticks"] != record["payload_start_ticks"]
        or leader["start_ticks"] != leader_ticks
    ):
        raise ValueError("refusing to signal an unowned process group")
    return leader["pgid"]


def _group_members(group: int) -> list[dict]:
    members = []
    for index, path in enumerate(Path("/proc").iterdir()):
        if index >= MAX_PROC_SCAN:
            raise ValueError("process identity scan budget exhausted; refusing restart")
        if path.name.isdigit():
            pid = int(path.name)
            identity = process_identity(pid)
            if identity is None:
                try:
                    belongs = os.getpgid(pid) == group and os.getsid(pid) == group
                except ProcessLookupError:
                    continue
                except OSError:
                    belongs = False
                if belongs:
                    try:
                        raw = (path / "stat").read_text()
                        zombie = raw[raw.rindex(")") + 2 :].split()[0] == "Z"
                    except (OSError, ValueError, IndexError):
                        zombie = False
                    if not zombie:
                        raise ValueError("batch group member identity unreadable; refusing restart")
                continue
            if identity["pgid"] == group and identity["sid"] == group:
                if identity["uid"] != os.getuid():
                    raise ValueError("batch group contains a foreign UID")
                members.append(identity)
    return members


def _captured_member_state(member: dict, fd: int | None) -> str:
    current = process_identity(member["pid"])
    if current is not None:
        if current["start_ticks"] != member["start_ticks"]:
            return "dead"
        # A process can change credentials and leave its group without exiting.
        # That original instance remains ambiguous and must block restart.
        if current["uid"] != member["uid"]:
            return "unknown"
        return "live" if current == member else "unknown"
    if fd is not None:
        try:
            if select.select([fd], [], [], 0)[0]:
                return "dead"
        except OSError:
            pass
    try:
        path = Path("/proc") / str(member["pid"])
        path.stat()
        raw = (path / "stat").read_text()
        if raw[raw.rindex(")") + 2 :].split()[0] == "Z":
            return "dead"
    except FileNotFoundError:
        return "dead"
    except (OSError, ValueError, IndexError):
        pass
    return "unknown"


def terminate_owned_batch(
    store: JobStore, job_id: str, identity: dict, *, grace: float = 10
) -> None:
    record = store._batch(job_id, identity)
    group = _safe_owned_group(record)
    initial = _group_members(group)
    allowed_cgroups = {member["cgroup"] for member in initial}
    handles: dict[tuple[int, int], tuple[dict, int | None]] = {}

    def capture(members: list[dict]) -> None:
        for member in members:
            if member["cgroup"] not in allowed_cgroups:
                raise ValueError("batch descendant cgroup changed; refusing restart")
            key = (member["pid"], member["start_ticks"])
            if key in handles:
                continue
            fd = None
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                try:
                    fd = os.pidfd_open(member["pid"])
                except ProcessLookupError:
                    continue
            state = _captured_member_state(member, fd)
            if state != "live":
                if fd is not None:
                    os.close(fd)
                if state == "unknown":
                    raise ValueError(
                        "batch member identity changed or unreadable; refusing restart"
                    )
                continue
            handles[key] = (member, fd)

    def empty_group() -> bool:
        members = _group_members(group)
        capture(members)
        live_known = False
        for member, fd in handles.values():
            state = _captured_member_state(member, fd)
            if state == "unknown":
                raise ValueError("known batch member identity unavailable; refusing restart")
            live_known = live_known or state == "live"
        return not members and not live_known

    try:
        capture(initial)
        if not store.validate(job_id, identity, require_batch=True):
            raise ValueError("batch identity changed before termination")
        os.killpg(_safe_owned_group(record), signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # TERM handlers may fork cleanup children after the initial snapshot.
            # Scan this dedicated session/group, never names or a user-wide group.
            if empty_group():
                time.sleep(0.02)
                if empty_group():
                    return
            time.sleep(0.05)
        # Re-scan and capture stable handles before each bounded kill pass. A
        # group leader can exit first; its remaining descendants still belong
        # to this unique batch session, with matching UID and original cgroups.
        kill_deadline = time.monotonic() + 1
        while time.monotonic() < kill_deadline:
            if empty_group():
                return
            for member, fd in list(handles.values()):
                state = _captured_member_state(member, fd)
                if state == "unknown":
                    raise ValueError("batch identity unavailable before signal; refusing restart")
                if state == "dead":
                    continue
                try:
                    if fd is not None:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    else:
                        os.kill(member["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.02)
        if not empty_group():
            raise ValueError("batch group still has live descendants; refusing restart")
    finally:
        for _member, fd in handles.values():
            if fd is not None:
                os.close(fd)


def handle_restart(
    store: JobStore, job_id: str, process: subprocess.Popen, settings: ResourceSettings, policy: str
) -> bool:
    try:
        request = read_private_json(store.request_path(job_id))
    except FileNotFoundError:
        return False
    identity = request.get("identity")
    record = store._batch(job_id, identity)
    if record.get("restartable") is not True or record.get("restart_count") != 0:
        raise ValueError("restart consent is unavailable")
    group = _safe_owned_group(record)
    # Consume consent before attempting termination, including failed attempts.
    store.update(job_id, restart_count=1)
    identity = store.identity(job_id)
    terminate_owned_batch(store, job_id, identity)
    process.wait(timeout=3)
    if _group_members(group):
        raise ValueError("old batch group has live descendants; restart refused")
    store.request_path(job_id).unlink(missing_ok=True)
    store.update(
        job_id, restart_count=1, status="queued", payload_pid=None, payload_start_ticks=None
    )
    if not admit(
        "batch",
        policy,
        settings,
        settings.queue_timeout,
        store,
        record["recipe_fingerprint"],
        recovery=True,
    ):
        store.update(job_id, status="queued_timeout")
        return False
    # Prior execution evidence remains durable throughout recovery. Only a
    # successfully admitted next attempt can clear its old handshake marker.
    store.marker_path(job_id).unlink(missing_ok=True)
    return True


def _apply_preferences(role: str) -> None:
    if role == "batch":
        try:
            os.setpriority(os.PRIO_PROCESS, 0, max(10, os.getpriority(os.PRIO_PROCESS, 0)))
        except OSError:
            warn("batch nice preference unavailable")
        try:
            Path("/proc/self/oom_score_adj").write_text("250")
        except OSError:
            warn("batch OOM preference unavailable")
    else:
        # Negative oom_score_adj requires the separately opted-in root helper.
        # Scope CPU/IO weights provide a modest unprivileged preference instead.
        warn(
            "agent negative OOM preference needs the optional root helper; scope weights are the unprivileged preference"
        )


def inner_main(store: JobStore, job_id: str, argv: list[str], scope: str | None) -> int:
    try:
        record = store.read(job_id)
        _apply_preferences(record["role"])
        store.register_payload(job_id, os.getpid(), scope)
        # The durable marker is the sole authority for fallback. Never exec if
        # writing it fails, even when the record update already succeeded.
        write_private_json(store.marker_path(job_id), {"started": True, "pid": os.getpid()})
    except (OSError, ValueError) as exc:
        warn(f"payload handshake failed ({type(exc).__name__}); payload not executed")
        return 122
    try:
        os.execvpe(argv[0], argv, os.environ)
    except OSError as exc:
        warn(f"payload exec failed ({type(exc).__name__})")
        return 127
    return 127


def runner_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    candidate = root / "bin/codex-job"
    return candidate if candidate.exists() else root / "codex-job"


def optional_agent_command(command: list[str], env: dict[str, str]) -> list[str]:
    try:
        # Settings locations follow the intended child environment, including HOME.
        home = Path(env.get("HOME", str(Path.home())))
        config = (
            Path(env.get("XDG_CONFIG_HOME", str(home / ".config")))
            / "codexfarm/resource-settings.json"
        )
        settings = load_settings(config)
        enabled = env.get("CODEXFARM_RESOURCE_PROTECTION") == "1" or settings.protect_agents
    except (OSError, ValueError, TypeError):
        warn("optional resource settings unavailable; launching agent normally")
        return command
    if enabled:
        return [sys.executable, str(runner_path()), "run", "--role", "agent", "--", *command]
    return command


def _scope_ready(role: str, env: dict[str, str]) -> bool:
    try:
        if manager_call(["show-environment"], env).returncode != 0:
            return False
        # Runtime-only sibling preferences. Leaf weights cannot prioritize
        # across independently equal-weight parent slices.
        interactive, batch = "codexfarm-interactive.slice", "codexfarm-batch.slice"
        if manager_call(["start", "codexfarm.slice", interactive, batch], env).returncode != 0:
            return False
        for unit, weight in ((interactive, 200), (batch, 25)):
            if (
                manager_call(
                    [
                        "set-property",
                        "--runtime",
                        unit,
                        f"CPUWeight={weight}",
                        f"IOWeight={weight}",
                    ],
                    env,
                ).returncode
                != 0
            ):
                warn("cross-role slice priority unavailable; retaining scope preferences")
        if role == "agent":
            # Every intermediate node needs protection for leaf MemoryLow to
            # reach the shared parent. Never reduce stronger or unknown values.
            for unit in ("codexfarm.slice", interactive):
                result = manager_call(["show", unit, "--property=MemoryLow", "--value"], env)
                existing = result.stdout.strip()
                if existing.startswith("MemoryLow="):
                    existing = existing.partition("=")[2]
                if result.returncode != 0 or (existing != "infinity" and not existing.isdigit()):
                    warn(f"{unit} memory preference could not be read; preserving it")
                elif existing != "infinity" and int(existing) < 1024 * 1024 * 1024:
                    if (
                        manager_call(
                            ["set-property", "--runtime", unit, "MemoryLow=1024M"], env
                        ).returncode
                        != 0
                    ):
                        warn(
                            "shared agent memory protection unavailable; retaining relative weights"
                        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def scope_command(
    role: str,
    scope: str,
    inner: list[str],
    *,
    memory_high: float | None = None,
    memory_max: float | None = None,
) -> list[str]:
    props = (
        ["MemoryLow=infinity", "CPUWeight=200", "IOWeight=200"]
        if role == "agent"
        else ["CPUWeight=25", "IOWeight=25"]
    )
    if memory_high is not None:
        props.append(f"MemoryHigh={int(memory_high * 1024 * 1024)}")
    if memory_max is not None:
        props.append(f"MemoryMax={int(memory_max * 1024 * 1024)}")
    result = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--expand-environment=no",
        f"--unit={scope}",
        f"--slice=codexfarm-{'interactive' if role == 'agent' else 'batch'}.slice",
    ]
    for prop in props:
        result.extend(["-p", prop])
    return [*result, "--", *inner]


def _launch(
    store: JobStore, record: dict, args: argparse.Namespace, env: dict[str, str], scoped: bool
) -> subprocess.Popen:
    scope = f"codexfarm-{record['role']}-{secrets.token_hex(16)}.scope" if scoped else None
    inner = [
        sys.executable,
        str(runner_path()),
        "_inner",
        record["job_id"],
        scope or "-",
        "--",
        *record["argv"],
    ]
    command = (
        scope_command(
            record["role"], scope, inner, memory_high=args.memory_high, memory_max=args.memory_max
        )
        if scoped
        else inner
    )
    process = subprocess.Popen(
        command, env=env, cwd=record["cwd"], start_new_session=record["role"] == "batch"
    )
    identity = process_identity(process.pid)
    if identity is not None:
        try:
            store.update(
                record["job_id"], launch_pid=process.pid, launch_start_ticks=identity["start_ticks"]
            )
        except (OSError, ValueError):
            # Popen already succeeded. Never let a storage error be interpreted
            # as a pre-execution launcher error: the durable marker may already
            # exist and the payload may be running. Keep this same child owned.
            warn("launcher identity record unavailable; continuing to supervise the same child")
    return process


def _wait(
    store: JobStore,
    record: dict,
    process: subprocess.Popen,
    settings: ResourceSettings,
    policy: str,
) -> tuple[int, bool]:
    while process.poll() is None:
        if record["role"] == "batch" and store.request_path(record["job_id"]).exists():
            try:
                restart = handle_restart(store, record["job_id"], process, settings, policy)
            except (ValueError, OSError, subprocess.TimeoutExpired):
                warn("restart request rejected: identity or consent changed")
                store.request_path(record["job_id"]).unlink(missing_ok=True)
                if process.poll() is not None:
                    store.update(record["job_id"], status="failed")
                    return 1, False
            else:
                return process.returncode or 0, restart
        time.sleep(0.1)
    return process.returncode, False


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run literal argv with optional user scopes. No caps by default; no services are installed."
    )
    commands = result.add_subparsers(dest="action", required=True)
    run = commands.add_parser(
        "run",
        help="run an explicitly declared agent or batch job",
        description=(
            "No caps by default. Optional scopes need a usable user manager; "
            "no services are installed. Agents bypass admission and reject "
            "limits/restarts. Negative agent OOM preference needs a separately "
            "opted-in privileged helper."
        ),
    )
    run.add_argument("--role", choices=("agent", "batch"), default="batch")
    run.add_argument(
        "--scope",
        choices=("auto", "required", "off"),
        default="auto",
        help="optional user-manager isolation; required fails before payload if unavailable",
    )
    run.add_argument(
        "--memory-policy",
        choices=("warn", "queue", "ignore"),
        default=None,
        help="new batch admission only; ignore bypasses memory and temporary deferrals",
    )
    run.add_argument("--queue-timeout", type=float, default=None)
    run.add_argument(
        "--restartable",
        action="store_true",
        help="batch-only consent for at most one externally requested restart",
    )
    run.add_argument("--worker-env", choices=sorted(WORKER_VARIABLES))
    run.add_argument("--workers", type=int)
    run.add_argument(
        "--memory-high", type=float, help="opt-in batch MiB threshold; requires scope enforcement"
    )
    run.add_argument(
        "--memory-max", type=float, help="opt-in batch MiB hard ceiling; requires scope enforcement"
    )
    run.add_argument("argv", nargs=argparse.REMAINDER)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_agent-enabled"]:
        return 0 if optional_agent_command(["agent"], dict(os.environ)) != ["agent"] else 1
    if argv and argv[0] == "_inner":
        if len(argv) < 5 or argv[3] != "--":
            return 2
        return inner_main(JobStore(), argv[1], argv[4:], None if argv[2] == "-" else argv[2])
    args = parser().parse_args(argv)
    try:
        settings = load_settings()
        command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
        if not command:
            raise ValueError("provide argv after --")
        if args.role == "agent" and (
            args.restartable or args.memory_high is not None or args.memory_max is not None
        ):
            raise ValueError("agents never accept restart consent or explicit memory ceilings")
        for value in (args.memory_high, args.memory_max):
            if value is not None and (not finite_number(value) or not 0 < value <= 1_000_000_000):
                raise ValueError("memory limits must be positive finite MiB")
        if (
            args.memory_high is not None
            and args.memory_max is not None
            and args.memory_high > args.memory_max
        ):
            raise ValueError("memory-high must be <= memory-max")
        limits = args.memory_high is not None or args.memory_max is not None
        if limits and args.scope == "off":
            raise ValueError("explicit limits require an enforcing scope")
        timeout = settings.queue_timeout if args.queue_timeout is None else args.queue_timeout
        if not finite_number(timeout) or not 0 <= timeout <= 86400:
            raise ValueError("queue-timeout must be finite seconds within 0..86400")
        validate_workers(args.worker_env, args.workers)
        store = JobStore()
        try:
            record = store.create(
                args.role,
                command,
                str(Path.cwd()),
                restartable=args.restartable,
                worker_env=args.worker_env,
                workers=args.workers,
                managed=True,
            )
        except (OSError, ValueError):
            if args.role != "agent" or args.scope == "required":
                raise
            warn("private job registration unavailable; launching optional agent normally")
            os.execvpe(command[0], command, os.environ)
            return 127
        policy = memory_policy(args.memory_policy, args.role, settings)
        if not admit(args.role, policy, settings, timeout, store, record["recipe_fingerprint"]):
            store.update(record["job_id"], status="queued_timeout")
            return 124
        env = manager_environment(dict(os.environ))
        overrides = store.recipe_overrides(record["recipe_fingerprint"])
        if record["worker_env"]:
            count = min(record["workers"], overrides.get("workers", record["workers"]))
            env[record["worker_env"]] = str(count)
            record = store.update(record["job_id"], workers=count)
        scoped = args.scope != "off" and _scope_ready(args.role, env)
        mandatory = args.scope == "required" or limits
        if args.scope != "off" and not scoped:
            if mandatory:
                store.update(record["job_id"], status="failed")
                warn("required user scope unavailable; payload not launched")
                return 1
            warn("user scope unavailable; falling back before execution")
        while True:
            try:
                process = _launch(store, record, args, env, scoped)
            except OSError:
                if not scoped:
                    raise
                if mandatory:
                    store.update(record["job_id"], status="failed")
                    warn("required scope launcher unavailable; payload not launched")
                    return 1
                warn("scope launcher unavailable; falling back before execution")
                scoped = False
                continue
            # Batch is a separate owned group. Relay termination to it while
            # keeping agent tty, inherited group, and job-control behavior intact.
            previous = {}
            if args.role == "batch":

                def forward(signum, _frame, owned_process=process):
                    try:
                        os.killpg(owned_process.pid, signum)
                    except ProcessLookupError:
                        pass

                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    previous[signum] = signal.signal(signum, forward)
            else:
                # Terminal interrupts already reach the provider in this inherited
                # foreground group. A Python supervisor must stay alive when the
                # provider handles Ctrl-C rather than exiting its interactive UI.
                def terminal_interrupt(_signum, _frame):
                    pass

                for signum in (signal.SIGINT, signal.SIGQUIT):
                    previous[signum] = signal.signal(signum, terminal_interrupt)
            try:
                code, restart = _wait(store, record, process, settings, policy)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            if restart:
                record = store.read(record["job_id"])
                overrides = store.recipe_overrides(record["recipe_fingerprint"])
                if record["worker_env"]:
                    count = min(record["workers"], overrides.get("workers", record["workers"]))
                    env[record["worker_env"]] = str(count)
                    record = store.update(record["job_id"], workers=count)
                continue
            current = store.read(record["job_id"])
            if current["status"] == "queued_timeout":
                return 124
            restart_failed = current["status"] == "failed" and current["restart_count"] == 1
            marker = store.marker_path(record["job_id"])
            if scoped and not marker.exists() and not restart_failed:
                if mandatory:
                    warn("required scope creation failed; payload not launched")
                    store.update(record["job_id"], status="failed")
                    return 1
                warn("scope creation failed; falling back before execution")
                scoped = False
                continue
            store.update(
                record["job_id"], status="finished" if code == 0 else "failed", returncode=code
            )
            return code if code >= 0 else 128 - code
    except (OSError, ValueError, TypeError) as exc:
        warn(f"invalid resource configuration or inaccessible private state: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
