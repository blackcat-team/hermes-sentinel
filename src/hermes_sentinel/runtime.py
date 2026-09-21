"""Single-threaded cooperative central runtime loop (Stage E5).

The minimal long-running composition that lets ONE thread serve the
accepted Stage B5 serial heartbeat HTTP server while periodically
executing the accepted Stage E4 ``MonitoringCycle`` — reopening
neither contract and adding no background workers of any kind:

    SentinelRuntime(server, cycle, interval).run_forever(should_stop)
                |
    one cooperative iteration (the same thread, until stop or failure):
                |
    1. stop predicate check          (bounded loop boundary)
    2. schedule check on the injected monotonic clock:
       ``MonitoringCycle.run()`` when due — exactly once, then
       next due = post-completion monotonic reading + interval
    3. server.timeout = min(poll_interval_seconds,
                            remaining time to next due)   (>= 0)
    4. server.handle_request()       (at most ONE heartbeat request)
                |
    back to 1

E5 owns exactly this interleaving and nothing else. The heartbeat
server keeps every B5 semantic (parsing, routing, framing, adapter
handoff, request handling) untouched and is driven ONLY through its
standard one-request ``handle_request`` mechanism plus the stdlib
accept-wait bound (``server.timeout``) that this mechanism respects;
the monitoring cycle is driven ONLY through ``MonitoringCycle.run``.
The repository's single-thread / non-thread-safe persistence boundary
is preserved by construction: no threads, no event loop, no worker
pools, no executors — one loop yielding between two frozen contracts.

Contract points (see docs/ARCHITECTURE.md section 26):

- **Injection only**: the runtime constructs NOTHING — no HTTP
  server, no SQLite/repositories, no ``HealthEngine``, no
  ``MonitoringCycle``, no ``TelegramSender``, no
  ``NotificationCoordinator``, no configuration, environment or
  secrets access. The heartbeat server, the monitoring cycle, the
  monotonic clock and the stop predicate are all injected; the
  caller retains ownership of resource construction and final
  cleanup (E5 never calls ``server_close`` and owns no service
  lifecycle);
- **Cooperative schedule**: the first monitoring cycle is due
  immediately when ``run_forever`` starts and runs before the first
  heartbeat accept wait. After a successful cycle the next due time
  is the post-completion monotonic reading plus
  ``monitor_interval_seconds`` — or, when that sum is not
  representably later than the completion reading (an interval below
  one float ULP at a very large clock value, where ordinary addition
  collapses), the next representable float — so a cycle is never
  scheduled at an unchanged clock instant. Missed time NEVER creates
  catch-up bursts: a delayed iteration performs exactly one cycle and
  reschedules from that completion;
- **One request per iteration**: between schedule checks at most one
  heartbeat request is serviced via ``handle_request()``. After every
  handled request control returns to the schedule check, and a due
  cycle runs before the next accept wait — continuous incoming
  heartbeat traffic therefore cannot starve monitoring;
- **Bounded accept wait**: before every ``handle_request`` the
  server's standard accept-wait timeout (``server.timeout``, the
  attribute the stdlib one-request mechanism respects) is set to a
  non-negative value that is never larger than BOTH
  ``poll_interval_seconds`` and the remaining time to the next
  monitoring due point. Heartbeat waiting can never outrun the
  schedule, and stop checks stay bounded by the poll interval;
- **Fail-fast interval validation**: ``monitor_interval_seconds`` and
  ``poll_interval_seconds`` must each be finite and strictly
  positive; anything else raises ``ValueError`` at construction and
  is never silently coerced;
- **Stop contract**: ``run_forever(should_stop=...)`` takes a small
  injectable stop predicate (for deterministic tests and later
  lifecycle wiring); the default never requests stop. If stop is
  already requested before work starts, the call returns having done
  no monitoring, no request handling and no monotonic clock access —
  the initial stop boundary precedes the first schedule access;
  otherwise stop is checked at every bounded loop boundary. E5 owns
  no process-signal handling and no systemd lifecycle — later units
  do;
- **Deliberately simple failure semantics**: no retry, no backoff, no
  exception translation, no exception swallowing, no per-host
  isolation, no delivery recovery queues. An exception raised by
  ``MonitoringCycle.run()``, ``handle_request()`` or the injected
  monotonic/stop collaborators propagates unchanged and the loop
  exits naturally through that propagation (this module contains no
  ``try``/``except`` statement at all). Production resilience
  belongs to later hardening.

Out of scope for E5: configuration file/environment parsing, Telegram
bot-token/settings loading, the full application composition root,
the central Sentinel systemd unit, TLS termination, reverse proxy
configuration, logging/metrics, retry/backoff, queueing, incident
persistence, dedupe/flap suppression, new database schema, service
monitoring, Hermes integration, remote remediation, deployment and
Stage F production hardening.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Protocol

from hermes_sentinel.monitoring import MonitoringCycle

__all__ = [
    "HeartbeatServer",
    "MonotonicClock",
    "SentinelRuntime",
    "StopPredicate",
]

#: Default upper bound for one heartbeat accept wait between schedule
#: checks: the standard stdlib serve-loop poll interval (0.5 seconds).
#: It bounds both stop-check latency and heartbeat waiting, never the
#: monitoring schedule itself.
_DEFAULT_POLL_INTERVAL_SECONDS = 0.5


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")


def _never_stop() -> bool:
    """The default stop predicate: stop is never requested."""
    return False


def _deadline_after(completion: float, interval: float) -> float:
    """The next due point for a cycle completed at ``completion``.

    Normally ``completion + interval``. When the interval is below
    one float ULP at a very large clock value, that ordinary sum
    collapses to ``completion`` itself and would re-mark the schedule
    immediately due — so the next representable float is used
    instead, keeping the stored deadline STRICTLY later than the
    completion instant. Normal arithmetic (where the sum already
    exceeds ``completion``) is untouched.
    """
    deadline = completion + interval
    if deadline <= completion:
        deadline = math.nextafter(completion, math.inf)
    return deadline


#: Injectable monotonic clock seam: returns strictly non-decreasing
#: seconds. The default is the standard library monotonic clock.
MonotonicClock = Callable[[], float]

#: Injectable stop predicate: truthy means the loop should return at
#: the next bounded loop boundary.
StopPredicate = Callable[[], bool]


class HeartbeatServer(Protocol):
    """The minimal structural heartbeat-server seam E5 drives.

    Exactly the standard serial-server one-request serving mechanism:
    a settable accept-wait bound ``timeout`` (the attribute the stdlib
    ``handle_request`` respects when the listening socket carries no
    timeout of its own) and ``handle_request()`` — serve at most one
    pending request, or return when the accept wait expires. The
    Stage B5 ``HTTPServer`` subclass satisfies this structurally
    without any modification, and so does any deterministic test
    double. Deliberately not ``runtime_checkable``: the data member
    makes structural instance checks meaningless, so this protocol
    exists for static typing only.
    """

    timeout: float | None

    def handle_request(self) -> None: ...


class SentinelRuntime:
    """Single-threaded cooperative loop over two frozen contracts.

    One public operation, ``run_forever``: interleave the injected
    heartbeat server's standard one-request serving with the injected
    monitoring cycle on a completion-anchored monotonic schedule,
    checking the injected stop predicate at every bounded loop
    boundary. The runtime holds no state beyond the injected
    collaborators and the two validated intervals; it constructs
    nothing and closes nothing.
    """

    __slots__ = (
        "_heartbeat_server",
        "_monitor_interval_seconds",
        "_monitoring_cycle",
        "_monotonic",
        "_poll_interval_seconds",
    )

    def __init__(
        self,
        heartbeat_server: HeartbeatServer,
        monitoring_cycle: MonitoringCycle,
        monitor_interval_seconds: float,
        *,
        monotonic: MonotonicClock = time.monotonic,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Wire the frozen collaborators; validate both intervals.

        ``monitor_interval_seconds`` and ``poll_interval_seconds``
        must each be finite and strictly positive — anything else
        fails fast with ``ValueError`` before any collaborator is
        touched. Neither value is ever silently coerced.
        """
        _require_positive("monitor_interval_seconds", monitor_interval_seconds)
        _require_positive("poll_interval_seconds", poll_interval_seconds)
        self._heartbeat_server = heartbeat_server
        self._monitoring_cycle = monitoring_cycle
        self._monitor_interval_seconds = monitor_interval_seconds
        self._monotonic = monotonic
        self._poll_interval_seconds = poll_interval_seconds

    def run_forever(self, should_stop: StopPredicate | None = None) -> None:
        """Serve heartbeats and run monitoring cooperatively, forever.

        Deterministic per-iteration order:

        1. stop predicate check (the loop-boundary check; the default
           predicate never stops, and a stop already requested before
           the first boundary returns with zero monitoring calls,
           zero request handling and zero monotonic clock access);
        2. schedule check on the injected monotonic clock: when due,
           exactly one ``MonitoringCycle.run()`` — never several, no
           matter how late the iteration is — and the next due time
           becomes a point representably STRICTLY later than the
           post-completion monotonic reading (normally that reading
           plus the monitor interval; the next representable float
           when the interval is below one float ULP — completion-
           anchored, no catch-up, never due at an unchanged clock
           instant);
        3. the server's standard accept-wait timeout is set to a
           non-negative value that is the minimum of
           ``poll_interval_seconds`` and the remaining time to the
           next due point;
        4. exactly one ``handle_request()`` — at most one heartbeat
           request per iteration — then control returns to 1.

        This method contains no ``try``/``except`` statement at all:
        an exception from the monitoring cycle, the heartbeat server
        or the injected monotonic/stop collaborators escapes as the
        original exception object, the loop exits through that
        propagation, and nothing is retried.
        """
        if should_stop is None:
            stop: StopPredicate = _never_stop
        else:
            stop = should_stop
        # Nothing is scheduled and no clock is read until a loop
        # boundary has passed: a pre-requested stop returns without
        # any clock access, monitoring or request handling. The
        # first monitoring cycle is due immediately at start — the
        # unscheduled sentinel below fires it before any heartbeat
        # accept wait.
        next_due: float | None = None
        while not stop():
            now = self._monotonic()
            if next_due is None:
                next_due = now
            if now >= next_due:
                self._monitoring_cycle.run()
                # Reschedule from COMPLETION, not from the due point:
                # late iterations never accumulate catch-up cycles,
                # and the stored deadline is representably STRICTLY
                # later than the completion instant even for sub-ULP
                # intervals at large clock values.
                now = self._monotonic()
                next_due = _deadline_after(
                    now, self._monitor_interval_seconds
                )
            remaining = max(next_due - now, 0.0)
            # Bound the accept wait by both the poll interval and the
            # remaining schedule time, never below zero: heartbeat
            # waiting can neither outrun monitoring nor block a
            # boundary stop check for long.
            self._heartbeat_server.timeout = min(
                self._poll_interval_seconds, remaining
            )
            self._heartbeat_server.handle_request()
