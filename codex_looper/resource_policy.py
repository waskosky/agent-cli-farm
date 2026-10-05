"""Pure available-memory admission policy; this module never controls processes."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codex_looper.health import Memory


def finite_number(value: object) -> bool:
    """Accept real configuration numbers without coercing strings or booleans."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_memory_reading(memory: Memory | None) -> bool:
    if memory is None:
        return False
    values = (memory.total_mib, memory.available_mib, memory.pressure_percent)
    return (
        all(finite_number(value) for value in values)
        and memory.total_mib > 0
        and 0 <= memory.available_mib <= memory.total_mib
        and 0 <= memory.pressure_percent <= 100
    )


@dataclass(frozen=True)
class HeadroomSettings:
    reserve_mib: float = 1024
    recovery_mib: float = 1536
    recovery_seconds: float = 30

    def __post_init__(self) -> None:
        for name in ("reserve_mib", "recovery_mib", "recovery_seconds"):
            if not finite_number(getattr(self, name)):
                raise ValueError(f"{name} must be a finite number")
        if not 0 < self.reserve_mib < self.recovery_mib:
            raise ValueError("headroom must satisfy 0 < reserve_mib < recovery_mib")
        if self.recovery_seconds < 0:
            raise ValueError("recovery_seconds must be nonnegative")


class HeadroomGate:
    """Admit immediately when healthy, then require sustained recovery after waiting."""

    def __init__(self, settings: HeadroomSettings | None = None):
        self.settings = HeadroomSettings() if settings is None else settings
        self.reason = "No memory sample yet; admission is advisory."
        self._waiting = False
        self._stable_since: float | None = None
        self._last_sample: float | None = None

    def sample(self, memory: Memory | None, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if not valid_memory_reading(memory):
            return self._advisory("Memory counters unavailable or invalid")
        if memory.total_mib <= self.settings.recovery_mib:
            return self._advisory("Headroom thresholds do not fit this host")
        if not finite_number(now):
            return self._advisory("Monotonic clock sample invalid")
        if self._last_sample is not None and now < self._last_sample:
            self._stable_since = None
        self._last_sample = now
        if memory.available_mib <= self.settings.reserve_mib or memory.pressure_percent >= 25:
            self._waiting = True
            self._stable_since = None
            self.reason = "Waiting for available RAM or critical memory stalls to recover."
            return False
        if not self._waiting:
            self.reason = "Available RAM permits admission."
            return True
        if memory.available_mib < self.settings.recovery_mib or memory.pressure_percent >= 10:
            self._stable_since = None
            self.reason = "Waiting for recovery headroom and memory stalls below 10%."
            return False
        if self._stable_since is None:
            self._stable_since = now
        stable_seconds = now - self._stable_since
        if stable_seconds < self.settings.recovery_seconds:
            self.reason = (
                f"Waiting for stable recovery: {stable_seconds:.1f}/"
                f"{self.settings.recovery_seconds:g} seconds."
            )
            return False
        self._waiting = False
        self._stable_since = None
        self.reason = "Stable memory recovery permits admission."
        return True

    def _advisory(self, detail: str) -> bool:
        self._waiting = False
        self._stable_since = None
        self._last_sample = None
        self.reason = f"{detail}; advisory admission allowed."
        return True
