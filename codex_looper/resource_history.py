"""Opt-in bounded aggregate history; never record commands or process identities."""

from __future__ import annotations

import json
from pathlib import Path

from .resource_config import read_private_bytes, write_private_bytes
from .resource_policy import finite_number


class ResourceHistory:
    def __init__(self, root: Path):
        self.path = root / "resource-history.jsonl"
        self.previous_signature = None
        self.previous_at = None
        self.cpu_previous = None

    def record(self, status: dict, *, hours: float, max_bytes: int) -> None:
        if not hours:
            return
        now = status["checked_at"]
        signature = (
            status["level"],
            status["effective_enforcement"],
            tuple(status["issues"]),
            status["cooling_jobs"],
        )
        if (
            self.previous_at is not None
            and 0 <= now - self.previous_at < 60
            and self.previous_signature == signature
        ):
            return
        from .health import read_memory

        memory = read_memory()
        item = {
            "schema_version": 1,
            "checked_at": now,
            "cpu_celsius": status["cpu_celsius"],
            "sample_age_seconds": status.get("sample_age_seconds"),
            "temperature_level": status["level"],
            "effective_enforcement": status["effective_enforcement"],
            "cooling_jobs": status["cooling_jobs"],
            "issues": status["issues"],
            "available_memory_mib": memory.available_mib if memory else None,
            "memory_pressure_percent": memory.pressure_percent if memory else None,
            "io_pressure_percent": memory.io_pressure_percent if memory else None,
            "cpu_usage_percent": self.cpu_usage(),
        }
        try:
            existing = read_private_bytes(self.path, max_bytes=16777216)
        except FileNotFoundError:
            existing = b""
        kept = []
        for line in existing.splitlines():
            value = json.loads(line)
            stamp = value.get("checked_at")
            if finite_number(stamp) and now - hours * 3600 <= stamp <= now:
                kept.append(line + b"\n")
        kept.append(
            (
                json.dumps(item, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
        )
        size = sum(map(len, kept))
        while kept and size > max_bytes:
            size -= len(kept.pop(0))
        write_private_bytes(self.path, b"".join(kept), max_bytes=max_bytes)
        self.previous_at, self.previous_signature = now, signature

    def cpu_usage(self) -> float | None:
        try:
            with Path("/proc/stat").open() as source:
                values = [int(x) for x in source.readline().split()[1:9]]
            # Guest fields are already included in user/nice and must not be counted twice.
            current = sum(values), values[3] + values[4]
            previous, self.cpu_previous = self.cpu_previous, current
            if previous is None or current[0] <= previous[0] or current[1] < previous[1]:
                return None
            return round(
                max(
                    0, min(100, 100 * (1 - (current[1] - previous[1]) / (current[0] - previous[0])))
                ),
                1,
            )
        except (OSError, ValueError, IndexError):
            return None
