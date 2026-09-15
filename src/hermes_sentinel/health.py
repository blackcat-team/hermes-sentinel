"""Deterministic health signals core (Stage D1).

Pure, side-effect-free evaluation of the two deterministic health
signal families:

- heartbeat freshness: is the latest accepted heartbeat MISSING,
  FRESH or STALE?
- resource thresholds: which configured resource thresholds, if any,
  are breached by a telemetry snapshot?

D1 answers exactly these two questions and nothing else. It does NOT
decide HEALTHY / DEGRADED / DOWN, does not emit state transitions,
and performs no network reachability probing: external TCP checks,
debounce/hysteresis and orchestration belong to later stage D units
(see docs/ARCHITECTURE.md section 18).

Contract points:

- freshness is evaluated exclusively against the central
  ``received_at`` time axis (Stage B2): the reporter-side telemetry
  timestamp is a separate axis and never participates;
- ``stale_after_seconds`` is the sole freshness authority —
  ``expected_interval_seconds`` is informational and never introduces
  an intermediate status;
- exact threshold equality stays FRESH (``age <= stale_after_seconds``);
- a ``latest_received_at`` in the future relative to ``now`` fails
  closed with ``ValueError``: inconsistent clock evidence is never
  clamped or silently accepted;
- a resource metric breaches when ``metric >= threshold`` (equality
  is a breach);
- ``load5_max is None`` disables the load check entirely;
- resource breaches are signal-only facts: they may later produce
  DEGRADED, never DOWN;
- the module is pure: no I/O, no wall-clock access, no caches, no
  globals holding evaluation state — all time enters explicitly as
  function arguments and results are immutable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hermes_sentinel.config import HeartbeatSettings, Thresholds
from hermes_sentinel.domain import HostTelemetry

__all__ = [
    "HeartbeatFreshness",
    "ResourceMetric",
    "HeartbeatFreshnessResult",
    "ResourceAssessment",
    "evaluate_heartbeat_freshness",
    "evaluate_resource_thresholds",
]


def _require_aware(name: str, value: datetime) -> None:
    """True datetime awareness per authoritative Python semantics.

    A datetime is timezone-aware only if BOTH hold: ``tzinfo is not
    None`` AND ``utcoffset() is not None``. A non-None tzinfo whose
    ``utcoffset()`` returns None denotes an effectively naive
    datetime (the same rule as the Stage B boundaries).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware (tzinfo is not None and"
            f" utcoffset() is not None), got {value!r}"
        )


class HeartbeatFreshness(Enum):
    """Freshness of the latest accepted heartbeat (central time axis)."""

    MISSING = "missing"
    FRESH = "fresh"
    STALE = "stale"


class ResourceMetric(Enum):
    """A configured resource threshold that a snapshot may breach."""

    CPU = "cpu"
    RAM = "ram"
    SWAP = "swap"
    DISK = "disk"
    INODES = "inodes"
    LOAD5 = "load5"


#: Canonical deterministic breach order for assessment results.
_CANONICAL_METRIC_ORDER: tuple[ResourceMetric, ...] = (
    ResourceMetric.CPU,
    ResourceMetric.RAM,
    ResourceMetric.SWAP,
    ResourceMetric.DISK,
    ResourceMetric.INODES,
    ResourceMetric.LOAD5,
)


@dataclass(frozen=True, slots=True)
class HeartbeatFreshnessResult:
    """Outcome of a heartbeat freshness evaluation.

    Invariants:

    - ``MISSING``: ``age_seconds`` is ``None`` — no heartbeat was
      ever accepted, and a fake age is never invented;
    - ``FRESH`` / ``STALE``: ``age_seconds`` is finite and ``>= 0``.
    """

    status: HeartbeatFreshness
    age_seconds: float | None

    def __post_init__(self) -> None:
        if self.status is HeartbeatFreshness.MISSING:
            if self.age_seconds is not None:
                raise ValueError(
                    "age_seconds must be None when status is MISSING"
                )
            return
        age = self.age_seconds
        if age is None:
            raise ValueError(
                "age_seconds must not be None when status is"
                f" {self.status.value!r}"
            )
        if not math.isfinite(age):
            raise ValueError(f"age_seconds must be finite, got {age!r}")
        if age < 0:
            raise ValueError(f"age_seconds must be >= 0, got {age!r}")


@dataclass(frozen=True, slots=True)
class ResourceAssessment:
    """Which configured resource thresholds are breached.

    ``breaches`` is an immutable tuple in the canonical deterministic
    order CPU, RAM, SWAP, DISK, INODES, LOAD5 — never a set or a
    list. This is a signal-only fact: resource breaches may later
    produce DEGRADED, never DOWN, and this module performs no host
    state resolution.
    """

    breaches: tuple[ResourceMetric, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.breaches, tuple):
            raise ValueError(
                "breaches must be a tuple (immutable), got"
                f" {type(self.breaches).__name__}"
            )
        rank = {
            metric: index
            for index, metric in enumerate(_CANONICAL_METRIC_ORDER)
        }
        positions: list[int] = []
        for metric in self.breaches:
            if metric not in rank:
                raise ValueError(f"unknown resource metric: {metric!r}")
            positions.append(rank[metric])
        if positions != sorted(positions):
            raise ValueError(
                "breaches must follow the canonical order"
                " CPU, RAM, SWAP, DISK, INODES, LOAD5"
            )
        if len(set(positions)) != len(positions):
            raise ValueError("breaches must not contain duplicates")

    @property
    def is_breached(self) -> bool:
        """True when at least one resource threshold is breached."""
        return len(self.breaches) > 0


def evaluate_heartbeat_freshness(
    *,
    now: datetime,
    latest_received_at: datetime | None,
    settings: HeartbeatSettings,
) -> HeartbeatFreshnessResult:
    """Classify the freshness of the latest accepted heartbeat.

    Freshness is measured ONLY on the central ``received_at`` time
    axis (assigned by the Stage B2 ingestion clock); the reporter-side
    telemetry timestamp is a separate axis and never participates.

    Rules (``stale_after_seconds`` is the sole authority):

    - no accepted heartbeat at all -> ``MISSING`` with
      ``age_seconds=None`` (never a fake timestamp, never a host
      state decision);
    - ``age = now - latest_received_at`` with
      ``0 <= age <= stale_after_seconds`` -> ``FRESH`` (exact
      threshold equality stays FRESH);
    - ``age > stale_after_seconds`` -> ``STALE``.

    ``expected_interval_seconds`` is informational only: an age above
    the expected interval but below the stale threshold is still
    FRESH; no intermediate status is invented from it.

    Clock safety (fail closed with ``ValueError``):

    - ``now`` and a non-None ``latest_received_at`` must both be
      truly timezone-aware (``tzinfo`` and ``utcoffset()`` not None);
      offset-aware timestamps with different UTC offsets compare by
      instant;
    - ``latest_received_at > now`` means the central clock moved
      backwards or the evidence is inconsistent: the age is never
      clamped to zero, ``abs()`` is never used and the reporter
      timestamp is never substituted.

    All time enters explicitly as arguments; no wall clock is read.
    """
    _require_aware("now", now)
    if latest_received_at is None:
        return HeartbeatFreshnessResult(
            status=HeartbeatFreshness.MISSING,
            age_seconds=None,
        )
    _require_aware("latest_received_at", latest_received_at)
    if latest_received_at > now:
        raise ValueError(
            "latest_received_at must not be in the future relative to"
            f" now (latest_received_at={latest_received_at!r},"
            f" now={now!r}): inconsistent clock evidence fails closed"
        )
    age_seconds = (now - latest_received_at).total_seconds()
    if age_seconds <= settings.stale_after_seconds:
        status = HeartbeatFreshness.FRESH
    else:
        status = HeartbeatFreshness.STALE
    return HeartbeatFreshnessResult(
        status=status,
        age_seconds=age_seconds,
    )


def evaluate_resource_thresholds(
    *,
    telemetry: HostTelemetry,
    thresholds: Thresholds,
) -> ResourceAssessment:
    """Determine which configured resource thresholds are breached.

    A metric breaches when ``metric >= threshold`` — equality is a
    breach. Exact mappings (no load1/load15 thresholds exist):

    - CPU: ``telemetry.cpu_percent`` vs ``thresholds.cpu_percent``;
    - RAM: ``telemetry.ram.percent`` vs ``thresholds.ram_percent``;
    - SWAP: ``telemetry.swap.percent`` vs
      ``thresholds.swap_percent`` (an absent swap resource is the
      normal 0/0/0 domain shape and is simply not breached under a
      positive threshold — never special-cased);
    - DISK: ``telemetry.root_filesystem.percent`` vs
      ``thresholds.disk_percent``;
    - INODES: ``telemetry.root_inodes.percent`` vs
      ``thresholds.inode_percent``;
    - LOAD5: ``telemetry.load.five`` vs ``thresholds.load5_max``
      (``None`` disables the check entirely: no LOAD5 breach can be
      emitted regardless of the observed load).

    The result is a signal-only fact in the canonical order CPU,
    RAM, SWAP, DISK, INODES, LOAD5. Inputs are never mutated.
    """
    breaches: list[ResourceMetric] = []
    if telemetry.cpu_percent >= thresholds.cpu_percent:
        breaches.append(ResourceMetric.CPU)
    if telemetry.ram.percent >= thresholds.ram_percent:
        breaches.append(ResourceMetric.RAM)
    if telemetry.swap.percent >= thresholds.swap_percent:
        breaches.append(ResourceMetric.SWAP)
    if telemetry.root_filesystem.percent >= thresholds.disk_percent:
        breaches.append(ResourceMetric.DISK)
    if telemetry.root_inodes.percent >= thresholds.inode_percent:
        breaches.append(ResourceMetric.INODES)
    if (
        thresholds.load5_max is not None
        and telemetry.load.five >= thresholds.load5_max
    ):
        breaches.append(ResourceMetric.LOAD5)
    return ResourceAssessment(breaches=tuple(breaches))
