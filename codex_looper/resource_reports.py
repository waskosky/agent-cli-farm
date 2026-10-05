"""Bounded, untrusted resource telemetry; private recipes never enter reports."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .health import read_memory
from .resource_jobs import ID_RE, WORKER_VARIABLES, JobStore
from .resource_policy import finite_number

MAX_SCAN_PIDS = 4096
MAX_CONSUMERS = 20
SCAN_SECONDS = 2
# Native CLI framing, schema and trusted instructions need the rest of 16 KiB.
MAX_PROMPT_BYTES = 2500


def label(value: object, limit: int = 96) -> str:
    clean = "".join(char for char in str(value) if not unicodedata.category(char).startswith("C"))[
        :limit
    ]
    while len(json.dumps(clean).encode()) > limit + 2:
        clean = clean[:-1]
    return clean


def _read(path: Path | str, limit: int = 8192, *, directory_fd: int | None = None) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("resource counter must be a regular file")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("resource counter exceeds read limit")
    return value.decode("utf-8", "surrogateescape")


def _counters(directory_fd: int, name: str, limit: int = 24) -> dict:
    result = {}
    try:
        for line in _read(name, directory_fd=directory_fd).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                result[label(parts[0], 40)] = min(int(parts[1]), 2**63 - 1)
                if len(result) >= limit:
                    break
    except (OSError, ValueError):
        pass
    return result


@contextmanager
def _directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def _status(fd: int) -> dict:
    return dict(
        line.split(":", 1) for line in _read("status", directory_fd=fd).splitlines() if ":" in line
    )


def _uids(fields: dict) -> tuple[int, ...]:
    values = tuple(int(value) for value in fields["Uid"].split())
    if len(values) != 4 or any(value < 0 for value in values):
        raise ValueError("process credentials unavailable")
    return values


def _process_identity(fd: int) -> tuple:
    raw = _read("stat", directory_fd=fd)
    fields = raw[raw.rindex(")") + 2 :].split()
    if fields[0] == "Z":
        raise ValueError("process exited")
    return int(fields[19]), _uids(_status(fd)), _read("cgroup", directory_fd=fd).rstrip("\n")


def _same_process(folder: Path, fd: int, identity: tuple) -> bool:
    # A retained proc directory never retargets a reused PID. Checking the current
    # directory additionally detects replacement in proc fixtures/mounted views.
    current, opened = folder.stat(follow_symlinks=False), os.fstat(fd)
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino) and _process_identity(
        fd
    ) == identity


def _group_sample(raw_cgroup: str, root: Path) -> dict:
    """Walk the exact kernel path beneath a stable cgroup root; display is never input."""
    if not raw_cgroup.startswith("0::/") or "\n" in raw_cgroup:
        return {}
    relative = raw_cgroup[4:]
    parts = relative.split("/") if relative else []
    if any(part in ("", ".", "..") for part in parts):
        return {}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result = {
            "memory_stat": _counters(fd, "memory.stat", 16),
            "memory_events": _counters(fd, "memory.events", 8),
        }
        try:
            result["cgroup_inode"] = os.fstat(fd).st_ino
            result["cgroup_current_mib"] = (
                int(_read("memory.current", 64, directory_fd=fd)) / 1048576
            )
            result["cgroup_growth_mib"] = None
        except (OSError, ValueError):
            pass
        return result
    finally:
        os.close(fd)


def _key(item: dict) -> tuple:
    return tuple(item.get(name) for name in ("pid", "start_ticks", "uid", "cgroup_identity"))


def collect_report(
    *,
    proc: Path = Path("/proc"),
    store: JobStore | None = None,
    previous: dict | None = None,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    clock=time.monotonic,
) -> tuple[dict, dict]:
    started = clock()
    memory = read_memory(proc)
    report = {
        "sampled_at": time.time(),
        "memory": asdict(memory) if memory else None,
        "scanned_pids": 0,
        "consumers": [],
        "jobs": [],
    }
    candidates = []
    rss_by_pid = {}
    try:
        try:
            with os.scandir(proc) as entries:
                for entry in entries:
                    if clock() - started >= SCAN_SECONDS or report["scanned_pids"] >= MAX_SCAN_PIDS:
                        break
                    if not entry.name.isdigit():
                        continue
                    report["scanned_pids"] += 1
                    folder = proc / entry.name
                    try:
                        with _directory(folder) as fd:
                            identity = _process_identity(fd)
                            fields = _status(fd)
                            rss = int(fields.get("VmRSS", "0").split()[0]) / 1024
                            if (
                                rss < 0
                                or _uids(fields) != identity[1]
                                or not _same_process(folder, fd, identity)
                            ):
                                continue
                            ticks, uids, raw_cgroup = identity
                            item = {
                                "pid": int(entry.name),
                                "start_ticks": ticks,
                                "uid": uids[1],
                                "label": label(fields.get("Name", "").strip()),
                                "cgroup": label(raw_cgroup, 256),
                                "cgroup_identity": hashlib.sha256(
                                    raw_cgroup.encode("utf-8", "surrogateescape")
                                ).hexdigest(),
                                "rss_mib": rss,
                                "pss_mib": None,
                                "growth_mib": None,
                            }
                            rss_by_pid[item["pid"]] = rss
                            # Retain at most 20 proc handles, regardless of the scan count.
                            if len(candidates) == MAX_CONSUMERS:
                                smallest = min(candidates, key=lambda value: value[3]["rss_mib"])
                                if rss <= smallest[3]["rss_mib"]:
                                    continue
                                candidates.remove(smallest)
                                os.close(smallest[0])
                            candidates.append((os.dup(fd), folder, identity, item))
                    except (OSError, ValueError, IndexError, KeyError):
                        continue
        except OSError:
            pass
        old = {_key(item): item for item in (previous or {}).get("consumers", [])}
        for fd, folder, identity, item in sorted(
            candidates, key=lambda value: value[3]["rss_mib"], reverse=True
        ):
            prior = old.get(_key(item))
            try:
                if clock() - started < SCAN_SECONDS:
                    if not _same_process(folder, fd, identity):
                        continue
                    try:
                        for line in _read("smaps_rollup", directory_fd=fd).splitlines():
                            if line.startswith("Pss:"):
                                item["pss_mib"] = int(line.split()[1]) / 1024
                                break
                    except (OSError, ValueError, IndexError):
                        pass
                    if clock() - started < SCAN_SECONDS:
                        try:
                            item.update(_group_sample(identity[2], cgroup_root))
                        except (OSError, ValueError):
                            pass
                    # Reject the entire sample when RSS, PSS or group counters may
                    # belong to different credentials, start ticks or membership.
                    if not _same_process(folder, fd, identity):
                        continue
                if prior is not None:
                    item["growth_mib"] = round(item["rss_mib"] - prior["rss_mib"], 3)
                    if (
                        "cgroup_current_mib" in item
                        and prior.get("cgroup_inode") == item["cgroup_inode"]
                        and finite_number(prior.get("cgroup_current_mib"))
                    ):
                        item["cgroup_growth_mib"] = (
                            item["cgroup_current_mib"] - prior["cgroup_current_mib"]
                        )
                report["consumers"].append(item)
            except (OSError, ValueError, IndexError, KeyError):
                continue
    finally:
        for fd, *_rest in candidates:
            os.close(fd)
    store = store or JobStore()
    identities = {}
    try:
        jobs = sorted(
            store.list_jobs(limit=512),
            key=lambda job: (
                job.get("status") != "running",
                -rss_by_pid.get(job.get("payload_pid"), 0),
            ),
        )
        for job in jobs[:20]:
            key = job.get("job_id")
            if not isinstance(key, str) or not ID_RE.fullmatch(key):
                continue
            public = {
                name: job.get(name)
                for name in (
                    "job_id",
                    "role",
                    "status",
                    "payload_pid",
                    "restartable",
                    "restart_count",
                    "workers",
                    "worker_env",
                )
            }
            for name in ("role", "status"):
                public[name] = label(public[name], 24)
            if public["worker_env"] not in WORKER_VARIABLES:
                public["worker_env"] = None
            report["jobs"].append(public)
            identities[key] = store.identity(key)
    except (OSError, ValueError):
        report["jobs_unavailable"] = True
    return report, identities


def model_projection(report: dict) -> str:
    """Select public fields, then trim whole rows to reserve native framing budget."""
    memory = report.get("memory")
    projected = {
        "memory": {
            key: memory.get(key)
            for key in (
                "total_mib",
                "available_mib",
                "swap_used_mib",
                "pressure_percent",
                "io_pressure_percent",
            )
        }
        if isinstance(memory, dict)
        else None,
        "jobs": [
            {
                key: item.get(key)
                for key in (
                    "job_id",
                    "role",
                    "status",
                    "payload_pid",
                    "workers",
                    "worker_env",
                    "restartable",
                    "restart_count",
                )
            }
            for item in report.get("jobs", [])[:4]
        ],
        "consumers": [
            {
                key: (label(item.get(key), 48) if key == "label" else item.get(key))
                for key in (
                    "pid",
                    "uid",
                    "label",
                    "rss_mib",
                    "pss_mib",
                    "growth_mib",
                    "cgroup_current_mib",
                    "cgroup_growth_mib",
                )
            }
            for item in report.get("consumers", [])[:8]
        ],
    }
    for source, target in zip(report.get("consumers", [])[:8], projected["consumers"], strict=True):
        for field, keys in (
            ("memory_stat", ("anon", "file", "shmem")),
            ("memory_events", ("high", "oom", "oom_kill")),
        ):
            target[field] = {
                key: source[field][key] for key in keys if key in source.get(field, {})
            }
    while True:
        data = json.dumps(projected, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
        if len(data.encode()) <= MAX_PROMPT_BYTES:
            return data
        if projected["consumers"]:
            projected["consumers"].pop()
        elif projected["jobs"]:
            projected["jobs"].pop()
        else:
            raise ValueError("telemetry cannot fit investigator input budget")
