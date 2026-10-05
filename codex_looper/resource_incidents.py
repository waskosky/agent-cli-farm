"""Detached incident scheduling and deterministic, consented batch-only remedies."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .health import read_memory, state_directory
from .resource_config import (
    MAX_JSON_BYTES,
    ResourceSettings,
    _no_duplicates,
    load_settings,
    private_directory,
    read_private_json,
    write_private_json,
    write_settings,
)
from .resource_jobs import WORKER_VARIABLES, JobStore
from .resource_policy import finite_number, valid_memory_reading
from .resource_reports import collect_report, model_projection

MAX_OUTPUT_BYTES = 32768
COOLDOWN_SECONDS = 900
SUSTAIN_SECONDS = 60
RETAIN_REPORTS = 20


def _reject_constant(_value):
    raise ValueError("nonfinite model output")


def validate_answer(raw: str, jobs: list[dict], identities: dict) -> dict:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("investigator output exceeds limit")
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid investigator JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "diagnosis",
        "evidence",
        "proposed_fixes",
        "actions",
    }:
        raise ValueError("unexpected investigator fields")
    if not isinstance(value["diagnosis"], str) or len(value["diagnosis"]) > 2000:
        raise ValueError("invalid diagnosis")
    for field in ("evidence", "proposed_fixes"):
        items = value[field]
        if (
            not isinstance(items, list)
            or len(items) > 8
            or any(not isinstance(item, str) or len(item) > 500 for item in items)
        ):
            raise ValueError("invalid investigator explanation")
    if not isinstance(value["actions"], list) or len(value["actions"]) > 8:
        raise ValueError("invalid investigator actions")
    known = {item["job_id"]: item for item in jobs}
    intents = set()
    for action in value["actions"]:
        if not isinstance(action, dict) or set(action) - {
            "kind",
            "job_id",
            "workers",
            "ttl_seconds",
        }:
            raise ValueError("unknown action fields")
        kind, key = action.get("kind"), action.get("job_id")
        if (
            not isinstance(kind, str)
            or kind not in {"defer", "reduce_workers", "restart"}
            or not isinstance(key, str)
        ):
            raise ValueError("unknown action")
        if key not in known or key not in identities or known[key].get("role") != "batch":
            raise ValueError("action target was not a reported registered batch job")
        if (kind, key) in intents:
            raise ValueError("duplicate action intent")
        intents.add((kind, key))
        job = known[key]
        ttl = action.get("ttl_seconds")
        if kind == "restart":
            if (
                ttl is not None
                or action.get("workers") is not None
                or job.get("restartable") is not True
                or job.get("restart_count") != 0
            ):
                raise ValueError("restart requires unused explicit consent")
        else:
            if ttl is None:
                action["ttl_seconds"] = 300
            elif not finite_number(ttl) or not 0 < ttl <= 3600:
                raise ValueError("invalid action TTL")
            if kind == "reduce_workers":
                count = action.get("workers")
                prior = job.get("workers")
                if (
                    job.get("worker_env") not in WORKER_VARIABLES
                    or type(count) is not int
                    or type(prior) is not int
                    or not 1 <= count < prior
                ):
                    raise ValueError("invalid declared worker reduction")
            elif action.get("workers") is not None:
                raise ValueError("deferral cannot change workers")
    return value


def short_metrics() -> dict:
    memory = read_memory()
    return {
        name: getattr(memory, name, None)
        for name in ("available_mib", "pressure_percent", "io_pressure_percent")
    }


def deteriorated(before: dict, after: dict) -> bool:
    available = before.get("available_mib"), after.get("available_mib")
    if all(finite_number(item) for item in available) and available[0] - available[1] >= 256:
        return True
    return any(
        finite_number(before.get(key))
        and finite_number(after.get(key))
        and after[key] - before[key] >= 10
        for key in ("pressure_percent", "io_pressure_percent")
    )


def apply_actions(
    answer: dict,
    identities: dict,
    store: JobStore,
    *,
    consent: bool,
    journal: Path,
    metrics=short_metrics,
    consent_check=None,
) -> list[dict]:
    if consent is not True:
        return []
    before = metrics()
    record = {"before": before, "actions": [], "after": None}
    # Durable intent precedes all mutations. A failed journal write prevents actions.
    write_private_json(journal, record)
    failed_jobs = set()
    for action in sorted(answer["actions"], key=lambda item: item["kind"] == "restart"):
        key, kind = action["job_id"], action["kind"]
        result = {"job_id": key, "kind": kind, "status": "rejected"}
        record["actions"].append(result)
        write_private_json(journal, record)
        identity = identities.get(key)
        try:
            if consent_check is not None and consent_check() is not True:
                raise ValueError("automatic consent revoked")
            if kind == "restart" and key in failed_jobs:
                raise ValueError("dependent reversible action failed")
            if not store.validate(key, identity, require_batch=True):
                raise ValueError("identity changed")
            if kind == "defer":
                result["override_id"] = store.defer_job(key, action["ttl_seconds"], identity)
            elif kind == "reduce_workers":
                result["override_id"] = store.reduce_workers(
                    key, action["workers"], action["ttl_seconds"], expectedidentity=identity
                )
            elif kind == "restart":
                store.request_restart(key, identity)
            else:
                raise ValueError("unknown action")
            result["status"] = (
                "restart_requested_nonreversible"
                if kind == "restart"
                else "applied_to_future_launches"
            )
            if kind != "restart":
                result["ttl_seconds"] = action["ttl_seconds"]
        except (OSError, ValueError):
            failed_jobs.add(key)
            result["status"] = "rejected_identity_or_consent"
        write_private_json(journal, record)
    after = metrics()
    record["after"] = after
    if deteriorated(before, after):
        for result in record["actions"]:
            if "override_id" in result:
                try:
                    store.rollback_override(result["override_id"])
                    result["rollback"] = "removed"
                except (OSError, ValueError):
                    result["rollback"] = "unavailable"
    write_private_json(journal, record)
    return record["actions"]


@contextmanager
def incident_lock(directory: Path):
    private_directory(directory, create=True)
    path = directory / "investigator.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("unsafe investigator lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            yield fd
    finally:
        os.close(fd)


def resource_directory() -> Path:
    return state_directory() / "resources"


def _last_started(directory: Path, now: float) -> float | None:
    try:
        value = read_private_json(directory / "cooldown.json").get("started_at")
    except FileNotFoundError:
        return None
    if not finite_number(value) or value > now:
        # Wall-clock rollback starts a fresh conservative cooldown.
        write_private_json(directory / "cooldown.json", {"started_at": now})
        return now
    return value


def _ready(directory: Path, now: float) -> bool:
    last = _last_started(directory, now)
    return last is None or now - last >= COOLDOWN_SECONDS


def launch_worker(directory: Path, lock_fd: int):
    root = Path(__file__).resolve().parent.parent
    executable = root / "bin/codex-resource"
    if not executable.exists():
        executable = root / "codex-resource"
    return subprocess.Popen(
        [
            sys.executable,
            str(executable),
            "_worker",
            "--lock-fd",
            str(lock_fd),
            "--state",
            str(directory),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(lock_fd,),
    )


class IncidentScheduler:
    """Cheap monitor-side checks; the model always runs in a detached worker."""

    def __init__(self, directory: Path, *, launch=launch_worker):
        self.directory = directory
        self.launch = launch
        self.since = None
        self.last_sample = None
        self.child = None

    def sample(self, memory, limits, settings: ResourceSettings, *, now: float):
        if self.child is not None and self.child.poll() is not None:
            self.child = None
        if not finite_number(now):
            self.since = None
            return
        if self.last_sample is not None and (now < self.last_sample or now - self.last_sample > 60):
            self.since = None
        self.last_sample = now
        if (
            settings.investigator != "codex"
            or not valid_memory_reading(memory)
            or not finite_number(memory.io_pressure_percent)
            or not 0 <= memory.io_pressure_percent <= 100
            or not (
                memory.level(limits) in ("warning", "critical") or memory.io_pressure_percent >= 10
            )
        ):
            self.since = None
            return
        if self.since is None:
            self.since = now
        if now - self.since < SUSTAIN_SECONDS or memory.available_mib < 512:
            return
        try:
            with incident_lock(self.directory) as fd:
                if fd is None or not _ready(self.directory, now):
                    return
                write_private_json(self.directory / "cooldown.json", {"started_at": now})
                self.child = self.launch(self.directory, fd)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Failed launches do not retry on every annotation pass.
            self.since = now


def retain(directory: Path, *, reserve: int = 0) -> None:
    private_directory(directory, create=True)
    files = sorted(directory.glob("*.json"), key=lambda path: path.name)
    for path in files[: -(RETAIN_REPORTS - reserve)]:
        read_private_json(path)  # Refuse unsafe existing files, including symlinks.
        path.unlink()


def reserve_report(saved: dict) -> dict:
    """Reserve space for all bounded results before any authorized mutation."""
    if (
        len(json.dumps(saved, allow_nan=False, sort_keys=True, separators=(",", ":")).encode())
        > MAX_JSON_BYTES - 8192
    ):
        raise ValueError("combined incident report exceeds private storage budget")
    return saved


def _investigate(directory: Path, *, actions: bool, scheduled: bool) -> dict:
    from .resource_investigator import InvestigatorError, investigate

    settings = load_settings()
    if settings.investigator != "codex":
        return {"status": "investigator_off"}
    memory = read_memory()
    if not valid_memory_reading(memory) or memory.available_mib < 512:
        return {"status": "deferred_memory_unavailable_or_below_512_mib"}
    store = JobStore()
    report_dir, journal_dir = directory / "reports", directory / "journals"
    private_directory(report_dir, create=True)
    private_directory(journal_dir, create=True)
    previous = None
    old = sorted(report_dir.glob("*.json"))
    if old:
        previous = read_private_json(old[-1]).get("report")
    report, trusted = collect_report(store=store, previous=previous)
    name = f"{time.time_ns():020d}-{secrets.token_hex(8)}.json"
    path = report_dir / name
    # Identities stay local and are never passed to the adapter.
    saved = {"report": report, "identities": trusted}
    retain(report_dir, reserve=1)
    write_private_json(path, saved)
    retain(report_dir)
    prompt = model_projection(report)
    reported_jobs = json.loads(prompt)["jobs"]
    try:
        raw = investigate(prompt, settings)
        answer = validate_answer(raw, reported_jobs, trusted)
        saved["answer"] = answer
        reserve_report(saved)
        write_private_json(path, saved)
        # Consent is hot-read after inference, immediately before applying actions.
        current = load_settings()
        consent = (
            current.investigator == "codex" and current.automatic_actions and (scheduled or actions)
        )
        if consent:
            retain(journal_dir, reserve=1)
        result = apply_actions(
            answer,
            trusted,
            store,
            consent=consent,
            journal=journal_dir / name,
            consent_check=automatic_consent,
        )
        saved["action_results"] = result
        saved["status"] = "diagnosed"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, InvestigatorError):
            saved["diagnostic"] = str(exc)
        saved.pop("answer", None)
        saved["status"] = "diagnostic_unavailable"
    write_private_json(path, saved)
    retain(journal_dir)
    return {
        key: saved[key]
        for key in ("status", "diagnostic", "answer", "action_results")
        if key in saved
    }


def automatic_consent() -> bool:
    settings = load_settings()
    return settings.investigator == "codex" and settings.automatic_actions


class WorkerTimeout(Exception):
    """Total investigation budget, distinct from recoverable per-action errors."""


@contextmanager
def worker_deadline(*, seconds: float = 120):
    def expired(_signum, _frame):
        raise WorkerTimeout("investigation time budget exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)


def run_investigation(directory: Path, *, actions=False, scheduled=False, lock_fd=None) -> dict:
    if lock_fd is not None:
        info, expected = os.fstat(lock_fd), (directory / "investigator.lock").lstat()
        if (
            (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("invalid inherited incident lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "investigator_busy"}
        return _investigate(directory, actions=actions, scheduled=scheduled)
    with incident_lock(directory) as fd:
        if fd is None:
            return {"status": "investigator_busy"}
        now = time.time()
        if not _ready(directory, now):
            return {"status": "cooldown"}
        write_private_json(directory / "cooldown.json", {"started_at": now})
        return _investigate(directory, actions=actions, scheduled=scheduled)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent opt-in resource settings and bounded incident diagnosis; configuration never changes services."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    for name in ("protect-agents", "queue-background", "automatic-actions"):
        configure.add_argument("--" + name, action=argparse.BooleanOptionalAction, default=None)
    configure.add_argument("--investigator", choices=("codex", "off"))
    configure.add_argument("--investigator-model")
    configure.add_argument("--investigator-binary")
    commands.add_parser("status")
    commands.add_parser("report").add_argument("--json", action="store_true")
    commands.add_parser("investigate").add_argument(
        "--actions",
        action="store_true",
        help="apply only with separate stored automatic-action consent",
    )
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--lock-fd", type=int)
    worker.add_argument("--state", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            settings = load_settings().as_dict()
            settings.update(
                {
                    name: value
                    for name, value in vars(args).items()
                    if name != "command" and value is not None
                }
            )
            write_settings(ResourceSettings.from_dict(settings))
            result = settings
        elif args.command == "status":
            result = {"settings": load_settings().as_dict(), "memory": short_metrics()}
        elif args.command == "report":
            result, _ = collect_report()
        else:
            if args.command == "_worker":
                from .resource_investigator import worker_preferences

                worker_preferences()
            with worker_deadline():
                result = run_investigation(
                    getattr(args, "state", None) or resource_directory(),
                    actions=getattr(args, "actions", False),
                    scheduled=args.command == "_worker",
                    lock_fd=getattr(args, "lock_fd", None),
                )
        print(json.dumps(result, allow_nan=False, ensure_ascii=True))
        return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, WorkerTimeout):
        print(
            "codex-resource: private settings, telemetry, or investigator unavailable",
            file=sys.stderr,
        )
        return 2
