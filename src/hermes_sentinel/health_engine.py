"""Health engine orchestration (Stage D4).

The bounded runtime composition layer that turns the frozen lower
stage D units into one deterministic per-host health evaluation:

    HeartbeatRepository (B1 read)          -> D1 freshness evidence
    latest heartbeat telemetry             -> D1 resource evidence
    external TCP probe (D2)                -> reachability evidence
    previous per-host resolution (memory)  -> D3 state resolution
                                              + D4 HostTransition

D4 owns exactly the orchestration promised in docs/ARCHITECTURE.md
sections 18-20: evidence acquisition, per-host retention of previous
D3 resolutions and confirmed transition creation. It re-opens none of
the lower contracts — D1/D2/D3 functions are called verbatim as the
source of truth, and no hysteresis, freshness, breach or probe logic
is duplicated here.

Contract points (see docs/ARCHITECTURE.md section 21):

- ``evaluate_host`` performs exactly one full evaluation per call:
  host configuration check, one central clock call, one repository
  read (``latest_heartbeat`` only), one D1 freshness evaluation, at
  most one D1 resource evaluation (only when a heartbeat exists),
  exactly one D2 TCP probe, one D3 resolution and at most one
  ``HostTransition`` — in exactly that order;
- the clock result is the single validated time authority for the
  whole evaluation: it is the D1 ``now``, the freshness age base and
  the transition confirmation moment;
- an unknown host fails closed with ``UnknownHostError`` before any
  repository, clock or TCP activity;
- a clock result that is not a ``datetime``, or is effectively naive
  (``tzinfo is None`` or ``utcoffset() is None``), fails closed with
  ``InvalidClockResultError`` — never silently coerced;
- a missing heartbeat yields public ``resources=None`` while the D3
  resolver still receives the accepted neutral, non-degrading
  ``ResourceAssessment(breaches=())`` input it requires (resources
  never participate in DOWN qualification, so no degradation and no
  DOWN is ever invented from absent telemetry);
- transitions are emitted ONLY on entering or leaving DOWN, comparing
  the previous remembered resolution state with the new one at the
  confirmed engine evaluation moment; the first evaluation of a host
  (even one resolving DOWN) has ``transition=None``, and ordinary
  HEALTHY <-> DEGRADED changes never emit;
- the per-host resolver memory is committed ONLY after the entire
  evaluation succeeded: any failure before result construction
  leaves the previously remembered per-host resolution exactly
  intact (all-or-nothing per host);
- the memory is process-local only: a restart starts every host from
  ``previous=None``, and nothing is persisted — D4 is read-only with
  respect to repository persistence (no writes, no schema changes,
  no receive-timestamp updates, no records of any kind are created);
- the D3 resolver remains the sole hysteresis authority: the per-host
  ``ExternalCheckSettings`` confirmation thresholds are passed
  through verbatim and never reinterpreted here.

Out of scope for D4: alerting and notification delivery,
scheduler/polling loops, service monitoring, retries/backoff, HTTP
endpoints, remote remediation and any new persistence (later roadmap
units).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from hermes_sentinel.config import SentinelConfig
from hermes_sentinel.domain import HostState, HostTransition
from hermes_sentinel.health import (
    HeartbeatFreshnessResult,
    ResourceAssessment,
    evaluate_heartbeat_freshness,
    evaluate_resource_thresholds,
)
from hermes_sentinel.ingestion import Clock, utc_now
from hermes_sentinel.persistence import HeartbeatRepository
from hermes_sentinel.reachability import TcpReachability, probe_tcp_reachability
from hermes_sentinel.state_resolver import (
    HostStateResolution,
    resolve_host_state,
)

__all__ = [
    "HostHealthEvaluation",
    "HealthEngineError",
    "UnknownHostError",
    "InvalidClockResultError",
    "HealthEngine",
]


#: The accepted neutral resource input for a missing heartbeat: the
#: unique non-degrading assessment. It is an internal resolver input
#: only — the public evaluation exposes ``resources=None`` instead.
_NEUTRAL_RESOURCES = ResourceAssessment(breaches=())


class HealthEngineError(Exception):
    """Base class for deterministic health engine failures."""


class UnknownHostError(HealthEngineError):
    """The evaluated host is not a configured Sentinel host.

    Raised before any clock, repository or TCP activity.
    """


class InvalidClockResultError(HealthEngineError):
    """The engine clock returned a value that is not a truly
    timezone-aware ``datetime``.

    A datetime is truly aware only if both ``tzinfo is not None`` and
    ``utcoffset() is not None`` (the same rule the B1/B2 boundaries
    enforce). An invalid clock result is never silently coerced; it
    fails closed before any repository read.
    """


@dataclass(frozen=True, slots=True)
class HostHealthEvaluation:
    """One complete deterministic per-host health evaluation.

    Immutable snapshot of everything one ``HealthEngine.evaluate_host``
    call established: the evaluated host, the single validated clock
    moment, the propagated D1/D2/D3 evidence and the optional confirmed
    DOWN/recovery transition.

    ``resources`` is ``None`` exactly when no heartbeat exists (the
    D3 resolver still consumed the neutral non-degrading input
    internally). ``transition`` is ``None`` unless this evaluation
    entered or left DOWN with an established previous resolution.
    """

    host: str
    evaluated_at: datetime
    freshness: HeartbeatFreshnessResult
    resources: ResourceAssessment | None
    reachability: TcpReachability
    resolution: HostStateResolution
    transition: HostTransition | None

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must be a non-empty string")
        # Defence in depth, matching the B-stage boundary rule: the
        # single evaluation moment is always truly timezone-aware.
        if self.evaluated_at.tzinfo is None or (
            self.evaluated_at.utcoffset() is None
        ):
            raise ValueError(
                "evaluated_at must be timezone-aware (tzinfo is not"
                " None and utcoffset() is not None), got"
                f" {self.evaluated_at!r}"
            )


def _build_transition(
    host: str,
    previous: HostStateResolution | None,
    resolution: HostStateResolution,
    evaluated_at: datetime,
) -> HostTransition | None:
    """Confirmed DOWN/recovery transition for one evaluation.

    Emitted ONLY when an established previous resolution is entered
    or left by DOWN on this evaluation. The first evaluation of a
    host (``previous=None``) never emits — even when the initial
    resolved state is DOWN — and ordinary HEALTHY <-> DEGRADED
    changes never emit.
    """
    if previous is None:
        return None
    entering_down = (
        previous.state is not HostState.DOWN
        and resolution.state is HostState.DOWN
    )
    leaving_down = (
        previous.state is HostState.DOWN
        and resolution.state is not HostState.DOWN
    )
    if not (entering_down or leaving_down):
        return None
    return HostTransition(
        host=host,
        from_state=previous.state,
        to_state=resolution.state,
        at=evaluated_at,
    )


class HealthEngine:
    """Bounded per-host health evaluation orchestrator (Stage D4).

    Composes the frozen D1/D2/D3 contracts and the B1 repository read
    into one deterministic ``HostHealthEvaluation`` per
    ``evaluate_host`` call. The engine keeps the per-host previous D3
    resolutions in process-local memory only; the usage model is the
    B-stage single service thread (like ``HeartbeatRepository``, the
    engine is not thread-safe).
    """

    def __init__(
        self,
        config: SentinelConfig,
        repository: HeartbeatRepository,
        clock: Clock = utc_now,
    ) -> None:
        self._config = config
        self._repository = repository
        self._clock = clock
        # Per-host resolver memory: the last fully committed D3
        # resolution. Process-local only; a restart starts from
        # previous=None for every host.
        self._resolutions: dict[str, HostStateResolution] = {}

    def evaluate_host(self, host: str) -> HostHealthEvaluation:
        """Perform exactly one full deterministic host evaluation.

        Order of operations (normative):

        1. ``host`` must be a configured Sentinel host — otherwise
           :class:`UnknownHostError` before any other activity;
        2. the injected clock is called exactly once and must return
           a truly timezone-aware ``datetime`` — otherwise
           :class:`InvalidClockResultError` before any repository
           read;
        3. ``repository.latest_heartbeat(host)`` is called exactly
           once (the only repository access; D4 never writes);
        4. D1 freshness is evaluated on the central ``received_at``
           axis against the single clock moment;
        5. with a heartbeat: the D1 resource assessment is evaluated
           and used both publicly and internally; without one: the
           public ``resources`` is ``None`` and the D3 resolver
           receives the neutral non-degrading input;
        6. exactly one D2 TCP probe supplies the reachability
           evidence;
        7. the D3 resolver resolves the new state from the evidence
           and the previously remembered per-host resolution;
        8. a transition is created only when DOWN was entered or
           left; the first evaluation never emits one;
        9. only after the complete immutable evaluation exists is the
           new resolution committed to the per-host memory — any
           earlier failure leaves the remembered state intact.

        Lower-layer exceptions (for example the D1 fail-closed
        ``ValueError`` on inconsistent clock evidence, repository
        failures or non-network probe defects) propagate unchanged:
        the engine adds no broad exception-wrapping hierarchy, and no
        partial state is committed on any failure.
        """
        host_config = self._config.host(host)
        if host_config is None:
            raise UnknownHostError(f"unknown Sentinel host: {host!r}")

        evaluated_at = self._clock()
        if not isinstance(evaluated_at, datetime) or (
            evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None
        ):
            raise InvalidClockResultError(
                "engine clock must return a timezone-aware datetime"
                " (tzinfo is not None and utcoffset() is not None),"
                f" got {evaluated_at!r}"
            )

        latest = self._repository.latest_heartbeat(host)

        freshness = evaluate_heartbeat_freshness(
            now=evaluated_at,
            latest_received_at=(
                None if latest is None else latest.received_at
            ),
            settings=host_config.heartbeat,
        )

        if latest is None:
            # No telemetry exists: no public resource assessment is
            # invented, and the resolver still gets its required
            # neutral, non-degrading input.
            public_resources: ResourceAssessment | None = None
            resolver_resources = _NEUTRAL_RESOURCES
        else:
            public_resources = evaluate_resource_thresholds(
                telemetry=latest.telemetry,
                thresholds=host_config.thresholds,
            )
            resolver_resources = public_resources

        reachability = probe_tcp_reachability(settings=host_config.external)

        previous = self._resolutions.get(host)
        resolution = resolve_host_state(
            previous=previous,
            freshness=freshness.status,
            resources=resolver_resources,
            reachability=reachability,
            settings=host_config.external,
        )

        transition = _build_transition(
            host, previous, resolution, evaluated_at
        )

        evaluation = HostHealthEvaluation(
            host=host,
            evaluated_at=evaluated_at,
            freshness=freshness,
            resources=public_resources,
            reachability=reachability,
            resolution=resolution,
            transition=transition,
        )

        # Commit point: the complete immutable evaluation exists, so
        # the per-host memory update is all-or-nothing from the
        # engine's point of view.
        self._resolutions[host] = resolution
        return evaluation
