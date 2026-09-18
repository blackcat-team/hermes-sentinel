"""Host state resolver with debounce/hysteresis (Stage D3).

A pure, deterministic state machine that combines the already-computed
D1/D2 evidence — heartbeat freshness, resource assessment and external
TCP reachability — with the explicit previous D3 resolution and the
existing confirmation settings into the final host state:

- ``HEALTHY`` / ``DEGRADED`` / ``DOWN`` (``hermes_sentinel.domain``).
- ``HostStateResolution`` is BOTH the current resolved state AND the
  minimal explicit memory to feed into the next evaluation.

Contract points (see docs/ARCHITECTURE.md section 20):

- base instantaneous state outside confirmed DOWN hysteresis:
  HEALTHY iff heartbeat FRESH AND TCP REACHABLE AND no resource
  breach; every other non-DOWN evidence combination is DEGRADED;
- resource problems alone never produce DOWN;
- a DOWN-confirmation observation is exactly heartbeat
  MISSING/STALE AND TCP UNREACHABLE — resource state does not
  participate in DOWN qualification;
- exactly ``down_confirmations`` consecutive qualifying observations
  confirm DOWN; any break in the combined predicate resets the
  pending down streak;
- once DOWN is confirmed it is held: heartbeat or resource changes
  alone never release it, and it is left only after exactly
  ``recovery_confirmations`` consecutive REACHABLE TCP observations
  (the recovery target state is recomputed from current evidence by
  the base instantaneous rule and may be HEALTHY or DEGRADED);
- ``previous=None`` means first evaluation: no established previous
  state, both streak counters 0;
- the module is pure: no I/O, no networking, no persistence, no log
  writes, no wall-clock access, no environment reads, no mutable
  module state — all memory enters explicitly as the immutable
  ``previous`` argument.

D3 deliberately performs no heartbeat/resource calculation, no TCP
probing, no database queries and no ``HostTransition`` construction:
D4 owns orchestration, per-host retention of previous resolutions
and confirmed transition creation.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_sentinel.config import ExternalCheckSettings
from hermes_sentinel.domain import HostState
from hermes_sentinel.health import HeartbeatFreshness, ResourceAssessment
from hermes_sentinel.reachability import TcpReachability

__all__ = [
    "HostStateResolution",
    "resolve_host_state",
]


@dataclass(frozen=True, slots=True)
class HostStateResolution:
    """Resolved host state plus the explicit D3 resolver memory.

    Invariants (structural only — semantic input validation belongs
    to the D1/D2/config contracts and is never duplicated here):

    - ``down_failures`` / ``recovery_successes`` are true integers
      (never ``bool``) and are ``>= 0``;
    - when ``state`` is DOWN: ``down_failures`` is 0 (a confirmed
      DOWN has no pending down streak) while ``recovery_successes``
      may be ``>= 0`` (the pending recovery streak);
    - when ``state`` is not DOWN: ``recovery_successes`` is 0 while
      ``down_failures`` may be ``>= 0`` (the pending down streak).

    Invalid direct construction fails closed with ``ValueError``.
    """

    state: HostState
    down_failures: int = 0
    recovery_successes: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("down_failures", self.down_failures),
            ("recovery_successes", self.recovery_successes),
        ):
            # bool is an int subclass but is never an acceptable
            # counter value: a counter is an integer count, not a
            # boolean flag.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"{name} must be an integer, got {type(value).__name__}"
                )
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value!r}")
        if self.state is HostState.DOWN:
            if self.down_failures != 0:
                raise ValueError(
                    "down_failures must be 0 when state is DOWN, got"
                    f" {self.down_failures!r}"
                )
        elif self.recovery_successes != 0:
            raise ValueError(
                "recovery_successes must be 0 when state is not DOWN,"
                f" got {self.recovery_successes!r}"
            )


def _base_state(
    freshness: HeartbeatFreshness,
    resources: ResourceAssessment,
    reachability: TcpReachability,
) -> HostState:
    """Base instantaneous state outside confirmed DOWN hysteresis.

    HEALTHY iff heartbeat FRESH AND TCP REACHABLE AND no resource
    threshold is breached; every other evidence combination is
    DEGRADED. Resource problems alone never produce DOWN.
    """
    if (
        freshness is HeartbeatFreshness.FRESH
        and reachability is TcpReachability.REACHABLE
        and not resources.is_breached
    ):
        return HostState.HEALTHY
    return HostState.DEGRADED


def _is_down_observation(
    freshness: HeartbeatFreshness,
    reachability: TcpReachability,
) -> bool:
    """A DOWN-confirmation observation.

    Exactly: heartbeat MISSING or STALE (the lost-heartbeat
    qualifying family) AND TCP UNREACHABLE. Resource state does not
    participate.
    """
    return (
        freshness
        in (HeartbeatFreshness.MISSING, HeartbeatFreshness.STALE)
        and reachability is TcpReachability.UNREACHABLE
    )


def resolve_host_state(
    *,
    previous: HostStateResolution | None,
    freshness: HeartbeatFreshness,
    resources: ResourceAssessment,
    reachability: TcpReachability,
    settings: ExternalCheckSettings,
) -> HostStateResolution:
    """Resolve the host state from D1/D2 evidence plus explicit memory.

    Consumes ONLY ``settings.down_confirmations`` and
    ``settings.recovery_confirmations`` (both guaranteed ``>= 1`` by
    the existing config contract); the TCP target/timeout fields
    belong to D2 and are never read here.

    ``previous=None`` means first evaluation: no established previous
    state, ``down_failures=0`` and ``recovery_successes=0``.

    The function is pure: it reads no clock, performs no I/O, mutates
    neither the arguments nor any module state, and returns a fresh
    immutable resolution. There is no host name and no timestamp —
    those are D4 orchestration concerns.
    """
    if previous is not None and previous.state is HostState.DOWN:
        # Confirmed DOWN hold: only the recovery hysteresis — the
        # consecutive REACHABLE TCP streak — may leave DOWN.
        if reachability is TcpReachability.UNREACHABLE:
            # Still externally down: remain DOWN and reset any
            # pending recovery streak; down_failures stays 0.
            return HostStateResolution(
                state=HostState.DOWN,
                down_failures=0,
                recovery_successes=0,
            )
        # REACHABLE: one more consecutive recovery success.
        successes = previous.recovery_successes + 1
        if successes >= settings.recovery_confirmations:
            # Exact threshold reached: leave DOWN on THIS evaluation
            # with both counters reset; the target state comes from
            # the CURRENT evidence via the base instantaneous rule
            # (TCP is REACHABLE here, so the target is HEALTHY only
            # when FRESH with clear resources, otherwise DEGRADED).
            return HostStateResolution(
                state=_base_state(freshness, resources, reachability),
                down_failures=0,
                recovery_successes=0,
            )
        # Below the recovery threshold: remain DOWN carrying the
        # pending recovery streak; down_failures stays 0.
        return HostStateResolution(
            state=HostState.DOWN,
            down_failures=0,
            recovery_successes=successes,
        )

    # Not in confirmed DOWN (possibly first evaluation): apply the
    # down debounce against the combined qualifying predicate.
    if _is_down_observation(freshness, reachability):
        failures = (0 if previous is None else previous.down_failures) + 1
        if failures >= settings.down_confirmations:
            # Exact threshold reached: confirm DOWN immediately on
            # this evaluation and reset the down streak.
            return HostStateResolution(
                state=HostState.DOWN,
                down_failures=0,
                recovery_successes=0,
            )
        # Below the threshold: stay DEGRADED (the qualifying evidence
        # is itself non-healthy) carrying the incremented streak.
        return HostStateResolution(
            state=HostState.DEGRADED,
            down_failures=failures,
            recovery_successes=0,
        )

    # Non-qualifying observation: any pending down streak is broken
    # and both counters reset; the state is the base instantaneous
    # resolution of the current evidence.
    return HostStateResolution(
        state=_base_state(freshness, resources, reachability),
        down_failures=0,
        recovery_successes=0,
    )
