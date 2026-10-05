"""Bounded, untrusted resource telemetry; private recipes never enter reports."""

from __future__ import annotations

import hashlib
import json
import os
import time
import unicodedata
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


def _read(path: Path, limit: int = 8192) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return stream.read(limit)


def _counters(path: Path, limit: int = 24) -> dict:
    result = {}
    try:
        for line in _read(path).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                result[label(parts[0], 40)] = min(int(parts[1]), 2**63 - 1)
                if len(result) >= limit:
                    break
    except OSError:
        pass
    return result


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
    try:
        with os.scandir(proc) as entries:
            for entry in entries:
                if clock() - started >= SCAN_SECONDS or report["scanned_pids"] >= MAX_SCAN_PIDS:
                    break
                if not entry.name.isdigit():
                    continue
                report["scanned_pids"] += 1
                try:
                    folder = proc / entry.name
                    info = entry.stat(follow_symlinks=False)
                    raw = _read(folder / "stat")
                    ticks = int(raw[raw.rindex(")") + 2 :].split()[19])
                    fields = dict(
                        line.split(":", 1)
                        for line in _read(folder / "status").splitlines()
                        if ":" in line
                    )
                    rss = int(fields.get("VmRSS", "0").split()[0]) / 1024
                    cgroup = _read(folder / "cgroup", 2048).strip()
                    candidates.append(
                        {
                            "pid": int(entry.name),
                            "start_ticks": ticks,
                            "uid": info.st_uid,
                            "label": label(fields.get("Name", "").strip()),
                            "cgroup": label(cgroup, 256),
                            "cgroup_identity": hashlib.sha256(cgroup.encode()).hexdigest(),
                            "rss_mib": rss,
                            "pss_mib": None,
                            "growth_mib": None,
                        }
                    )
                except (OSError, ValueError, IndexError):
                    continue
    except OSError:
        pass
    old = {_key(item): item for item in (previous or {}).get("consumers", [])}
    for item in sorted(candidates, key=lambda item: item["rss_mib"], reverse=True)[:MAX_CONSUMERS]:
        prior = old.get(_key(item))
        if prior is not None:
            item["growth_mib"] = round(item["rss_mib"] - prior["rss_mib"], 3)
        if clock() - started < SCAN_SECONDS:
            try:
                for line in _read(proc / str(item["pid"]) / "smaps_rollup").splitlines():
                    if line.startswith("Pss:"):
                        item["pss_mib"] = int(line.split()[1]) / 1024
                        break
            except (OSError, ValueError, IndexError):
                pass
        # cgroup paths are untrusted; never permit traversal outside the mounted root.
        relative = item["cgroup"].removeprefix("0::/")
        if (
            clock() - started < SCAN_SECONDS
            and item["cgroup"].startswith("0::/")
            and ".." not in Path(relative).parts
        ):
            group = cgroup_root / relative
            item["memory_stat"] = _counters(group / "memory.stat", 16)
            item["memory_events"] = _counters(group / "memory.events", 8)
            try:
                item["cgroup_inode"] = group.stat().st_ino
                item["cgroup_current_mib"] = int(_read(group / "memory.current", 64)) / 1048576
                item["cgroup_growth_mib"] = None
                if (
                    prior
                    and prior.get("cgroup_inode") == item["cgroup_inode"]
                    and finite_number(prior.get("cgroup_current_mib"))
                ):
                    item["cgroup_growth_mib"] = (
                        item["cgroup_current_mib"] - prior["cgroup_current_mib"]
                    )
            except (OSError, ValueError):
                pass
        report["consumers"].append(item)
    store = store or JobStore()
    identities = {}
    try:
        rss_by_pid = {item["pid"]: item["rss_mib"] for item in candidates}
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
