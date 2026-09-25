"""External dead-man oneshot runtime cycle (Stage H1B-2).

The single bounded orchestration that composes the accepted H1A core
and the accepted H1B-1 adapters into exactly ONE deterministic cycle:

    run_deadman_cycle(store=..., prober=..., sender=..., clock=...)
                    |
    1. load persisted state        (H1B-1 DeadManStateStore.load)
    2. perform exactly one probe   (H1B-1 DeadManProber.probe)
    3. obtain explicit now         (injectable clock, called once)
    4. advance the H1A machine     (advance_deadman_status)
    5. persist the resulting state (store.save — BEFORE delivery)
    6. inspect the OLDEST pending notification only
       (none -> finish successfully)
    7. attempt exactly one delivery (H1B-1 DeadManTelegramSender.send)
    8. on success: acknowledge that exact id and persist again
       (ACK AFTER successful delivery)

This module owns ONLY the ordering. The H1A state machine stays the
sole authority for debounce, transitions, notification intents and
acknowledgement; the H1B-1 adapters stay the sole I/O boundaries. No
daemon loop, no sleep, no retry, no backoff and no second probe or
delivery attempt exist here: run CADENCE belongs to the systemd timer
(packaging), never to the process.

Contract points (see docs/ARCHITECTURE.md section 33):

- **Exact ordering is safety-critical**: the state produced by step 4
  — including any newly created notification intent — is persisted
  in step 5 BEFORE any Telegram delivery is attempted, and the
  acknowledgement is persisted in step 8 only AFTER a successful
  delivery. Therefore a process crash after Telegram accepted the
  message but before the acknowledged state is persisted leaves the
  same pending intent durable, and the next run sends it again: this
  is intentional AT-LEAST-ONCE external delivery. A possible
  duplicate alert after a crash is always preferred over a silently
  lost one; exactly-once Telegram delivery is deliberately NOT
  attempted;
- **At most one delivery attempt per cycle**: always the OLDEST
  pending intent, never a drain loop. A bounded oneshot keeps strict
  oldest-first H1A semantics, and the timer naturally retries the
  next cycle; a Telegram failure leaves the persisted pending queue
  unchanged (no acknowledgement, no second post-delivery persist);
- **A probe failure is an observation, not a crash**: a DNS/connect/
  TLS/HTTP liveness failure already maps to the FAILED outcome
  inside the accepted prober and simply flows into the H1A debounce.
  "The target is unreachable" is normal dead-man input; only a
  failure of the runtime's own infrastructure (state store, delivery
  transport, clock boundary, unexpected adapter defect) is a runtime
  failure;
- **One coherent timestamp**: the injectable clock is called exactly
  once per cycle, after the probe, and that single timezone-aware
  value is the explicit ``now`` of the one state-machine evaluation
  (no clock logic inside H1A, no naive datetimes). The production
  clock boundary :func:`utc_now` reads UTC; deterministic tests
  inject their own;
- **Bounded runtime failures**: state-store read/decode/write errors
  (``DeadManStateStoreError``), Telegram delivery failures
  (``DeadManTelegramDeliveryError``), a defective clock boundary
  (:class:`DeadManRuntimeError`) and unexpected adapter failures
  propagate to the process boundary as bounded categories — a load
  or first-persist failure happens strictly before any Telegram
  attempt. A target DOWN observation with successful state
  processing is a NORMAL completed cycle, never an execution
  failure.

Out of scope for H1B-2: environment loading (deadman_config), the
process/CLI boundary (deadman_process), systemd packaging, run
cadence, deployment and H2 live acceptance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from hermes_sentinel.deadman import (
    DeadManNotification,
    DeadManProbeOutcome,
    DeadManState,
    DeadManStatus,
    acknowledge_deadman_notification,
    advance_deadman_status,
)
from hermes_sentinel.deadman_probe import DeadManProber
from hermes_sentinel.deadman_store import DeadManStateStore
from hermes_sentinel.deadman_telegram import DeadManTelegramSender

__all__ = [
    "DeadManCycleResult",
    "DeadManRuntimeError",
    "run_deadman_cycle",
    "utc_now",
]


class DeadManRuntimeError(Exception):
    """A bounded dead-man runtime failure.

    Concise and secret-safe by construction: the message names only a
    bounded runtime category — never the bot token, an authenticated
    URL, a response body or the persisted document contents.
    """


def utc_now() -> datetime:
    """The production clock boundary: one timezone-aware UTC read.

    Deliberately the entire clock framework: the process passes this
    callable into :func:`run_deadman_cycle`, which calls it exactly
    once per cycle, and deterministic tests inject their own callable
    instead. No H1A clock logic exists anywhere else.
    """
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DeadManCycleResult:
    """The bounded outcome of one completed dead-man cycle.

    Exactly the operator-relevant facts of a NORMAL cycle: the
    dead-man state after the cycle, how many notification intents
    remain pending, and — when a delivery was attempted — the exact
    delivered and acknowledged notification id (identical values:
    acknowledgement always targets the id that was just delivered).
    No secrets and no persisted document contents appear here.
    """

    state: DeadManState
    pending_count: int
    delivered_notification_id: int | None = None
    acknowledged_notification_id: int | None = None


def _require_aware_now(now: object) -> datetime:
    """Validate the clock boundary result (fail closed, bounded).

    The one timestamp of this cycle must be a truly timezone-aware
    datetime (``tzinfo`` AND ``utcoffset()`` not None — the H1A
    rule); a defective injected clock is a bounded runtime failure,
    never a naive datetime handed to H1A.
    """
    if not isinstance(now, datetime):
        raise DeadManRuntimeError(
            "dead-man clock boundary must produce a datetime, got"
            f" {type(now).__name__}"
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise DeadManRuntimeError(
            "dead-man clock boundary must produce a timezone-aware"
            " datetime (never naive)"
        )
    return now


def run_deadman_cycle(
    *,
    store: DeadManStateStore,
    prober: DeadManProber,
    sender: DeadManTelegramSender,
    clock: Callable[[], datetime],
) -> DeadManCycleResult:
    """Run exactly ONE bounded dead-man cycle and return its outcome.

    The exact safety-critical ordering (load -> probe -> now ->
    advance -> persist -> inspect oldest -> deliver -> acknowledge ->
    persist) is fixed here; every semantic decision stays with the
    accepted H1A core and every I/O action with the accepted H1B-1
    adapters. At most ONE pending notification — the OLDEST — is
    delivered, and only after the transitioned state was persisted.
    A delivery failure propagates as
    ``DeadManTelegramDeliveryError`` leaving the persisted pending
    intent intact for the next cycle; store failures propagate as
    ``DeadManStateStoreError`` (a load or first-persist failure
    happens strictly before any Telegram attempt).
    """
    status: DeadManStatus = store.load()
    outcome: DeadManProbeOutcome = prober.probe()
    now = _require_aware_now(clock())
    advanced = advance_deadman_status(
        status=status, outcome=outcome, now=now
    )
    # Persist BEFORE any delivery: a newly created notification
    # intent must already be durable when Telegram is attempted, so a
    # crash before acknowledgement can only ever duplicate delivery,
    # never lose the intent.
    store.save(advanced)
    if not advanced.pending_notifications:
        return DeadManCycleResult(
            state=advanced.state, pending_count=0
        )
    oldest: DeadManNotification = advanced.pending_notifications[0]
    # Exactly one delivery attempt for the OLDEST pending intent.
    sender.send(oldest)
    # Delivery succeeded: acknowledge that EXACT id and persist the
    # acknowledged state only now (ACK-after-success).
    acknowledged = acknowledge_deadman_notification(
        status=advanced, notification_id=oldest.notification_id
    )
    store.save(acknowledged)
    return DeadManCycleResult(
        state=acknowledged.state,
        pending_count=len(acknowledged.pending_notifications),
        delivered_notification_id=oldest.notification_id,
        acknowledged_notification_id=oldest.notification_id,
    )
