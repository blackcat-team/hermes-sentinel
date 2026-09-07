"""Typed domain contracts for Hermes Sentinel.

This module defines the data shapes shared by future roadmap stages
(heartbeat ingestion, host reporter, health engine, incidents). It
intentionally contains no behaviour beyond invariant validation:
state resolution, persistence, network probes and delivery are
implemented by later stages, not here.

Host state semantics are normative and documented in
docs/ARCHITECTURE.md:

- resource problems may produce DEGRADED, never DOWN;
- heartbeat lost while TCP reachable => DEGRADED;
- DOWN requires heartbeat lost/stale AND external TCP failure,
  confirmed by debounce/hysteresis;
- service state never participates in HEALTHY/DEGRADED/DOWN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "HostState",
    "LoadAverage",
    "ResourceUsage",
    "HostTelemetry",
    "HostTransition",
]


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")


def _require_non_negative(name: str, value: float) -> None:
    _require_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


def _require_percent(name: str, value: float) -> None:
    _require_finite(name, value)
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"{name} must be within [0, 100], got {value!r}")


def _require_non_empty(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must be a non-empty string")


class HostState(Enum):
    """Host-level state.

    - HEALTHY: host is alive and no resource threshold is breached.
    - DEGRADED: heartbeats are stale but the host is still externally
      reachable, or a resource threshold is breached.
    - DOWN: heartbeat is lost/stale AND the external TCP probe fails,
      confirmed by debounce/hysteresis.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class LoadAverage:
    """Unix load average over the 1/5/15 minute windows."""

    one: float
    five: float
    fifteen: float

    def __post_init__(self) -> None:
        _require_non_negative("load.one", self.one)
        _require_non_negative("load.five", self.five)
        _require_non_negative("load.fifteen", self.fifteen)


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Used/total/percent usage of a finite resource.

    Units are defined by the caller: RAM/swap and filesystem usage are
    bytes, inode usage is a count. When ``total`` is 0 the resource is
    absent (e.g. swap disabled) and ``used``/``percent`` must be 0.
    """

    used: float
    total: float
    percent: float

    def __post_init__(self) -> None:
        _require_non_negative("used", self.used)
        _require_non_negative("total", self.total)
        _require_percent("percent", self.percent)
        if self.total == 0.0:
            if self.used != 0.0:
                raise ValueError(
                    "used must be 0 when total is 0 (resource absent)"
                )
            if self.percent != 0.0:
                raise ValueError(
                    "percent must be 0 when total is 0 (resource absent)"
                )
        elif self.used > self.total:
            raise ValueError(
                f"used ({self.used!r}) must not exceed total ({self.total!r})"
            )


@dataclass(frozen=True, slots=True)
class HostTelemetry:
    """Mandatory per-host telemetry snapshot (host reporter payload).

    Covers the MVP primary question: is the server alive, is it
    reachable, is its heartbeat fresh, and are CPU/RAM/swap/load,
    the root filesystem "/" and root inodes healthy.
    """

    host: str
    timestamp: datetime
    uptime_seconds: float
    load: LoadAverage
    cpu_percent: float
    ram: ResourceUsage
    swap: ResourceUsage
    root_filesystem: ResourceUsage
    root_inodes: ResourceUsage

    def __post_init__(self) -> None:
        _require_non_empty("host", self.host)
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        _require_non_negative("uptime_seconds", self.uptime_seconds)
        _require_percent("cpu_percent", self.cpu_percent)


@dataclass(frozen=True, slots=True)
class HostTransition:
    """A confirmed host state change (DOWN / RECOVERED events).

    A transition always changes state; ``at`` is the moment the change
    was confirmed by the health engine (Stage D), not the moment it was
    first suspected.
    """

    host: str
    from_state: HostState
    to_state: HostState
    at: datetime

    def __post_init__(self) -> None:
        _require_non_empty("host", self.host)
        if self.at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        if self.from_state is self.to_state:
            raise ValueError("transition must change the host state")

    @property
    def is_down_event(self) -> bool:
        """True when the host transitioned into DOWN."""
        return self.to_state is HostState.DOWN

    @property
    def is_recovery_event(self) -> bool:
        """True when the host left DOWN (DOWN -> HEALTHY or DOWN -> DEGRADED)."""
        return (
            self.from_state is HostState.DOWN
            and self.to_state is not HostState.DOWN
        )
