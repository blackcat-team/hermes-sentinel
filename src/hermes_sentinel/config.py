"""Typed configuration contracts for Hermes Sentinel.

Only the shapes and their invariants are defined here. Configuration
loading/parsing is out of scope for Stage A1.

Notable contract points (see docs/ARCHITECTURE.md):

- a production node configuration without services is the normal case:
  ``HostConfig.services`` defaults to an empty tuple;
- an empty ``services`` list is valid and never affects host state;
- service monitoring is not part of the MVP; ``services`` exists only
  as a forward-compatible extension point (Stage I).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = [
    "HeartbeatSettings",
    "ExternalCheckSettings",
    "Thresholds",
    "HostConfig",
    "SentinelConfig",
]


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")


def _require_percent(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if not 0.0 < value <= 100.0:
        raise ValueError(f"{name} must be within (0, 100], got {value!r}")


def _require_non_empty(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must be a non-empty string")


def _require_positive_int(name: str, value: int) -> None:
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value!r}")


@dataclass(frozen=True, slots=True)
class HeartbeatSettings:
    """Heartbeat freshness contract (Stage B).

    A heartbeat is considered stale when no report has arrived within
    ``stale_after_seconds`` since the last accepted one.
    """

    expected_interval_seconds: float
    stale_after_seconds: float

    def __post_init__(self) -> None:
        _require_positive(
            "expected_interval_seconds", self.expected_interval_seconds
        )
        _require_positive("stale_after_seconds", self.stale_after_seconds)


@dataclass(frozen=True, slots=True)
class ExternalCheckSettings:
    """External TCP reachability and debounce contract (Stage D).

    DOWN requires heartbeat lost/stale AND an external TCP failure,
    confirmed by ``down_confirmations`` consecutive failed probes.
    Recovery is confirmed by ``recovery_confirmations`` consecutive
    successful probes (hysteresis).
    """

    tcp_host: str
    tcp_port: int
    timeout_seconds: float = 5.0
    down_confirmations: int = 3
    recovery_confirmations: int = 2

    def __post_init__(self) -> None:
        _require_non_empty("tcp_host", self.tcp_host)
        if not 1 <= self.tcp_port <= 65535:
            raise ValueError(
                f"tcp_port must be within [1, 65535], got {self.tcp_port!r}"
            )
        _require_positive("timeout_seconds", self.timeout_seconds)
        _require_positive_int("down_confirmations", self.down_confirmations)
        _require_positive_int(
            "recovery_confirmations", self.recovery_confirmations
        )


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Resource breach thresholds.

    A breach may move a host to DEGRADED; it never moves a host to
    DOWN. ``load5_max`` is an absolute 5-minute load average limit;
    ``None`` disables the load check.
    """

    cpu_percent: float = 90.0
    ram_percent: float = 90.0
    swap_percent: float = 80.0
    disk_percent: float = 85.0
    inode_percent: float = 90.0
    load5_max: float | None = None

    def __post_init__(self) -> None:
        _require_percent("cpu_percent", self.cpu_percent)
        _require_percent("ram_percent", self.ram_percent)
        _require_percent("swap_percent", self.swap_percent)
        _require_percent("disk_percent", self.disk_percent)
        _require_percent("inode_percent", self.inode_percent)
        if self.load5_max is not None:
            _require_positive("load5_max", self.load5_max)


@dataclass(frozen=True, slots=True)
class HostConfig:
    """Production node configuration for one monitored host.

    ``services`` is an optional extension point (Stage I). An empty
    tuple is the normal production configuration: it is valid and never
    affects host state, because service state does not participate in
    HEALTHY/DEGRADED/DOWN semantics.
    """

    name: str
    heartbeat: HeartbeatSettings
    external: ExternalCheckSettings
    thresholds: Thresholds = field(default_factory=Thresholds)
    services: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty("name", self.name)
        for service in self.services:
            _require_non_empty("services entry", service)
        if len(set(self.services)) != len(self.services):
            raise ValueError("services entries must be unique")


@dataclass(frozen=True, slots=True)
class SentinelConfig:
    """Whole-service configuration: the set of monitored hosts."""

    hosts: tuple[HostConfig, ...] = ()

    def __post_init__(self) -> None:
        names = [host.name for host in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("host names must be unique")

    def host(self, name: str) -> HostConfig | None:
        """Return the host configuration with the given name, if present."""
        for candidate in self.hosts:
            if candidate.name == name:
                return candidate
        return None
