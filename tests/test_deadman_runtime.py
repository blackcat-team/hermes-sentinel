"""Deterministic runtime-ordering tests for the Stage H1B-2 dead-man
oneshot cycle.

These tests prove the SAFETY-CRITICAL ordering of
``run_deadman_cycle`` against fake adapters and a fixed injected
clock (the accepted H1A core itself and the H1B-1 adapters carry
their own accepted suites):

    A. no pending: load -> probe -> advance -> persist -> finish,
       no Telegram call;
    B. new DOWN: failed observation confirms DOWN, the new DOWN
       intent is persisted BEFORE delivery, then delivered, then the
       exact id acknowledged and the acknowledged state persisted;
    C. Telegram failure: the transitioned state is persisted first,
       delivery fails, no ACK, no second post-delivery persist, the
       pending intent survives;
    D. pre-existing pending: the OLDEST pending intent is delivered
       before any newer one, at most one send per cycle;
    E. crash-window: the persisted pre-delivery state is sufficient
       for the next run to retry the SAME notification when ACK
       persistence never happened (at-least-once delivery);
    F. successful delivery acknowledges the exact notification_id of
       the oldest pending intent;
    G. a probe transport failure outcome feeds H1A as a failed
       observation and never becomes a runtime exception;
    H. a store load/decode failure fails closed BEFORE any probe or
       Telegram call;
    I. a first-persist failure happens BEFORE any Telegram delivery
       is attempted;
    J. an acknowledgement-persist failure is a bounded runtime
       failure whose at-least-once consequence is the still-persisted
       pending intent.

Every seeded status is produced through the ACCEPTED H1A state
machine (never hand-built documents), so the fixtures can never
drift from real reachable states.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.deadman import (  # noqa: E402
    INITIAL_DEADMAN_STATUS,
    DeadManProbeOutcome,
    DeadManState,
    acknowledge_deadman_notification,
    advance_deadman_status,
)
from hermes_sentinel.deadman_runtime import (  # noqa: E402
    DeadManRuntimeError,
    run_deadman_cycle,
    utc_now,
)
from hermes_sentinel.deadman_store import (  # noqa: E402
    DeadManStateStoreError,
)
from hermes_sentinel.deadman_telegram import (  # noqa: E402
    DeadManTelegramDeliveryError,
)


def _at(minute: int) -> datetime:
    """One distinct timezone-aware evaluation moment per minute."""
    return datetime(2026, 9, 25, 12, minute, tzinfo=timezone.utc)


T1, T2, T3, T4 = _at(1), _at(2), _at(3), _at(4)
T5, T6, T7, T8, T9, T10 = (
    _at(5), _at(6), _at(7), _at(8), _at(9), _at(10),
)


class FakeStore:
    """Deterministic store double.

    ``save`` appends to ``save_calls`` BEFORE the configured failure
    check and only updates the persisted state when the save
    succeeds — the real atomic store leaves the previous target
    content untouched when a write fails, and the crash-window tests
    depend on exactly that semantics.
    """

    def __init__(
        self,
        initial,
        failing_saves: tuple[int, ...] = (),
        journal: list[str] | None = None,
    ) -> None:
        self._persisted = initial
        self._failing_saves = set(failing_saves)
        self._journal = journal
        self.load_calls = 0
        self.save_calls: list = []

    def load(self):
        self.load_calls += 1
        if self._journal is not None:
            self._journal.append("load")
        return self._persisted

    def save(self, status) -> None:
        self.save_calls.append(status)
        if self._journal is not None:
            self._journal.append("save")
        index = len(self.save_calls) - 1
        if index in self._failing_saves:
            # One-shot simulated failure (consumed when fired): the
            # next save attempt at the same absolute position — e.g.
            # the next RUN's first persist — succeeds, exactly like a
            # crash-window that happened once.
            self._failing_saves.discard(index)
            raise DeadManStateStoreError(
                "simulated dead-man state store write failure"
            )
        self._persisted = status


class FailingLoadStore:
    """A store double whose load always fails (read/decode failure)."""

    def __init__(self) -> None:
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        raise DeadManStateStoreError(
            "simulated dead-man state store read failure"
        )

    def save(self, status) -> None:  # pragma: no cover - never reached
        raise AssertionError("save must never be reached after a failed load")


class FakeProber:
    """Deterministic prober double returning one fixed outcome."""

    def __init__(self, outcome, journal: list[str] | None = None) -> None:
        self._outcome = outcome
        self._journal = journal
        self.calls = 0

    def probe(self):
        self.calls += 1
        if self._journal is not None:
            self._journal.append("probe")
        return self._outcome


class FakeSender:
    """Deterministic sender double recording every delivery attempt."""

    def __init__(
        self,
        failure: Exception | None = None,
        journal: list[str] | None = None,
    ) -> None:
        self._failure = failure
        self._journal = journal
        self.sent: list = []

    def send(self, notification) -> None:
        self.sent.append(notification)
        if self._journal is not None:
            self._journal.append("send")
        if self._failure is not None:
            raise self._failure


class FakeClock:
    """Deterministic clock boundary returning one fixed moment."""

    def __init__(self, now) -> None:
        self._now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._now


def _up_with_two_failures():
    """An UP status carrying two consecutive failed observations: the
    next failure (the third) confirms DOWN on that evaluation."""
    status = advance_deadman_status(
        status=INITIAL_DEADMAN_STATUS,
        outcome=DeadManProbeOutcome.HEALTHY,
        now=T1,
    )
    status = advance_deadman_status(
        status=status, outcome=DeadManProbeOutcome.FAILED, now=T2
    )
    return advance_deadman_status(
        status=status, outcome=DeadManProbeOutcome.FAILED, now=T3
    )


def _down_with_two_pending():
    """A DOWN status whose pending queue holds TWO unacknowledged
    DOWN intents: id 1 from an older outage that recovered before its
    alert was ever acknowledged (no RECOVERED was emitted for it), and
    id 2 from the current outage."""
    status = advance_deadman_status(
        status=INITIAL_DEADMAN_STATUS,
        outcome=DeadManProbeOutcome.HEALTHY,
        now=T1,
    )
    for moment in (T2, T3, T4):
        status = advance_deadman_status(
            status=status, outcome=DeadManProbeOutcome.FAILED, now=moment
        )
    for moment in (T5, T6):
        status = advance_deadman_status(
            status=status, outcome=DeadManProbeOutcome.HEALTHY, now=moment
        )
    for moment in (T7, T8, T9):
        status = advance_deadman_status(
            status=status, outcome=DeadManProbeOutcome.FAILED, now=moment
        )
    return status


def _ids(status) -> list[int]:
    return [n.notification_id for n in status.pending_notifications]


class RuntimeOrderingTest(unittest.TestCase):
    def test_a_no_pending_completes_without_telegram(self) -> None:
        journal: list[str] = []
        store = FakeStore(INITIAL_DEADMAN_STATUS, journal=journal)
        prober = FakeProber(DeadManProbeOutcome.HEALTHY, journal=journal)
        sender = FakeSender(journal=journal)
        clock = FakeClock(T1)

        result = run_deadman_cycle(
            store=store, prober=prober, sender=sender, clock=clock
        )

        self.assertEqual(journal, ["load", "probe", "save"])
        self.assertEqual(sender.sent, [])
        self.assertEqual(len(store.save_calls), 1)
        self.assertEqual(result.state, DeadManState.UP)
        self.assertEqual(result.pending_count, 0)
        self.assertIsNone(result.delivered_notification_id)
        self.assertIsNone(result.acknowledged_notification_id)
        self.assertEqual(clock.calls, 1)

    def test_b_new_down_persisted_before_delivery_then_ack(self) -> None:
        journal: list[str] = []
        seed = _up_with_two_failures()
        store = FakeStore(seed, journal=journal)
        prober = FakeProber(DeadManProbeOutcome.FAILED, journal=journal)
        sender = FakeSender(journal=journal)
        clock = FakeClock(T4)

        result = run_deadman_cycle(
            store=store, prober=prober, sender=sender, clock=clock
        )

        # Exact ordering: persist BEFORE delivery, ACK persist after.
        self.assertEqual(
            journal, ["load", "probe", "save", "send", "save"]
        )
        # The first persist already carries the new DOWN intent.
        first = store.save_calls[0]
        self.assertIs(first.state, DeadManState.DOWN)
        self.assertEqual(_ids(first), [1])
        self.assertEqual(first.state_changed_at, T4)
        self.assertEqual(first.pending_notifications[0].at, T4)
        # The delivered notification is exactly that persisted intent.
        self.assertEqual(_ids_of(sender.sent), [1])
        # The second persist is the acknowledged state: queue drained,
        # the outage binding marked acknowledged for that exact id.
        second = store.save_calls[1]
        self.assertEqual(second.pending_notifications, ())
        self.assertIsNotNone(second.current_down)
        assert second.current_down is not None
        self.assertTrue(second.current_down.acknowledged)
        self.assertEqual(second.current_down.notification_id, 1)
        self.assertEqual(result.state, DeadManState.DOWN)
        self.assertEqual(result.pending_count, 0)
        self.assertEqual(result.delivered_notification_id, 1)
        self.assertEqual(result.acknowledged_notification_id, 1)
        self.assertEqual(clock.calls, 1)

    def test_c_telegram_failure_leaves_pending_intact(self) -> None:
        journal: list[str] = []
        store = FakeStore(_up_with_two_failures(), journal=journal)
        prober = FakeProber(DeadManProbeOutcome.FAILED, journal=journal)
        sender = FakeSender(
            failure=DeadManTelegramDeliveryError(
                "Dead-man Telegram delivery failed: HTTP status 502"
            ),
            journal=journal,
        )

        with self.assertRaises(DeadManTelegramDeliveryError):
            run_deadman_cycle(
                store=store,
                prober=prober,
                sender=sender,
                clock=FakeClock(T4),
            )

        # Transition state persisted first, then exactly one failed
        # delivery attempt — no ACK, no second post-delivery persist,
        # the pending intent survives in the persisted state.
        self.assertEqual(journal, ["load", "probe", "save", "send"])
        self.assertEqual(_ids_of(sender.sent), [1])
        self.assertEqual(len(store.save_calls), 1)
        persisted = store.save_calls[0]
        self.assertIs(persisted.state, DeadManState.DOWN)
        self.assertEqual(_ids(persisted), [1])

    def test_d_oldest_pending_delivered_at_most_once(self) -> None:
        journal: list[str] = []
        seed = _down_with_two_pending()
        self.assertEqual(_ids(seed), [1, 2])
        store = FakeStore(seed, journal=journal)
        prober = FakeProber(DeadManProbeOutcome.FAILED, journal=journal)
        sender = FakeSender(journal=journal)

        result = run_deadman_cycle(
            store=store, prober=prober, sender=sender, clock=FakeClock(T10)
        )

        self.assertEqual(
            journal, ["load", "probe", "save", "send", "save"]
        )
        # Exactly one send, and it delivered the OLDEST pending id.
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(sender.sent[0].notification_id, 1)
        # The acknowledged persist keeps the newer intent pending.
        second = store.save_calls[1]
        self.assertEqual(_ids(second), [2])
        self.assertEqual(result.delivered_notification_id, 1)
        self.assertEqual(result.acknowledged_notification_id, 1)
        self.assertEqual(result.pending_count, 1)
        self.assertIs(result.state, DeadManState.DOWN)

    def test_e_crash_window_retries_same_notification(self) -> None:
        # Run 1: delivery SUCCEEDS, but the acknowledgement persist
        # fails (the crash window: Telegram accepted the message, the
        # acknowledged state never reached the disk).
        store = FakeStore(
            _up_with_two_failures(), failing_saves=(1,)
        )
        first_sender = FakeSender()
        with self.assertRaises(DeadManStateStoreError):
            run_deadman_cycle(
                store=store,
                prober=FakeProber(DeadManProbeOutcome.FAILED),
                sender=first_sender,
                clock=FakeClock(T4),
            )
        self.assertEqual(_ids_of(first_sender.sent), [1])
        # The persisted state is still the pre-acknowledgement state
        # with the pending intent (the second save failed and left the
        # previous content untouched, like the real atomic store).
        self.assertEqual(_ids(store.save_calls[0]), [1])

        # Run 2 (next invocation): the SAME notification id is
        # delivered again — at-least-once external delivery after the
        # crash window, never a silent loss.
        second_sender = FakeSender()
        result = run_deadman_cycle(
            store=store,
            prober=FakeProber(DeadManProbeOutcome.FAILED),
            sender=second_sender,
            clock=FakeClock(T5),
        )
        self.assertEqual(_ids_of(second_sender.sent), [1])
        self.assertEqual(result.delivered_notification_id, 1)
        self.assertEqual(result.acknowledged_notification_id, 1)
        self.assertEqual(result.pending_count, 0)

    def test_f_acknowledgement_uses_exact_oldest_id(self) -> None:
        seed = _down_with_two_pending()
        store = FakeStore(seed)
        sender = FakeSender()

        result = run_deadman_cycle(
            store=store,
            prober=FakeProber(DeadManProbeOutcome.FAILED),
            sender=sender,
            clock=FakeClock(T10),
        )

        self.assertEqual(sender.sent[0].notification_id, 1)
        self.assertEqual(result.delivered_notification_id, 1)
        self.assertEqual(result.acknowledged_notification_id, 1)
        # The acknowledged persist is EXACTLY the accepted H1A
        # acknowledgement of the oldest id over the advanced state.
        advanced = advance_deadman_status(
            status=seed, outcome=DeadManProbeOutcome.FAILED, now=T10
        )
        expected = acknowledge_deadman_notification(
            status=advanced, notification_id=1
        )
        self.assertEqual(store.save_calls[1], expected)
        # A wrong (non-head) id would have failed closed inside H1A —
        # the runtime never attempts it: the delivered id is the ack.
        self.assertNotEqual(result.acknowledged_notification_id, 2)

    def test_g_probe_transport_failure_is_observation(self) -> None:
        # The accepted prober maps DNS/connect/TLS failures to the
        # FAILED outcome; the runtime treats it as dead-man input.
        journal: list[str] = []
        store = FakeStore(INITIAL_DEADMAN_STATUS, journal=journal)
        prober = FakeProber(DeadManProbeOutcome.FAILED, journal=journal)
        sender = FakeSender(journal=journal)

        result = run_deadman_cycle(
            store=store,
            prober=prober,
            sender=sender,
            clock=FakeClock(T1),
        )

        # A NORMAL completed cycle: no exception, debounce advanced.
        self.assertEqual(journal, ["load", "probe", "save"])
        self.assertEqual(sender.sent, [])
        self.assertIs(result.state, DeadManState.UNKNOWN)
        self.assertEqual(result.pending_count, 0)
        self.assertEqual(store.save_calls[0].consecutive_failures, 1)

    def test_h_store_load_failure_fails_closed_before_probe(self) -> None:
        store = FailingLoadStore()
        prober = FakeProber(DeadManProbeOutcome.HEALTHY)
        sender = FakeSender()

        with self.assertRaises(DeadManStateStoreError):
            run_deadman_cycle(
                store=store,
                prober=prober,
                sender=sender,
                clock=FakeClock(T1),
            )

        self.assertEqual(store.load_calls, 1)
        self.assertEqual(prober.calls, 0)
        self.assertEqual(sender.sent, [])

    def test_i_first_persist_failure_prevents_delivery(self) -> None:
        store = FakeStore(_up_with_two_failures(), failing_saves=(0,))
        prober = FakeProber(DeadManProbeOutcome.FAILED)
        sender = FakeSender()

        with self.assertRaises(DeadManStateStoreError):
            run_deadman_cycle(
                store=store,
                prober=prober,
                sender=sender,
                clock=FakeClock(T4),
            )

        # The new DOWN intent was never durable, so NO Telegram
        # delivery may be attempted at all.
        self.assertEqual(len(store.save_calls), 1)
        self.assertEqual(sender.sent, [])


def _ids_of(sent: list) -> list[int]:
    return [notification.notification_id for notification in sent]


class RuntimeBoundaryTest(unittest.TestCase):
    def test_j_ack_persist_failure_is_bounded_at_least_once(
        self,
    ) -> None:
        # Delivery succeeded; persisting the acknowledged state fails.
        # The bounded failure surfaces, and the persisted state still
        # holds the pending intent — the next run necessarily delivers
        # it again (the at-least-once crash window, by design).
        store = FakeStore(_up_with_two_failures(), failing_saves=(1,))
        sender = FakeSender()

        with self.assertRaises(DeadManStateStoreError):
            run_deadman_cycle(
                store=store,
                prober=FakeProber(DeadManProbeOutcome.FAILED),
                sender=sender,
                clock=FakeClock(T4),
            )

        self.assertEqual(_ids_of(sender.sent), [1])
        self.assertEqual(len(store.save_calls), 2)
        self.assertEqual(_ids(store._persisted), [1])

    def test_clock_called_exactly_once_per_cycle(self) -> None:
        clock = FakeClock(T1)
        run_deadman_cycle(
            store=FakeStore(INITIAL_DEADMAN_STATUS),
            prober=FakeProber(DeadManProbeOutcome.HEALTHY),
            sender=FakeSender(),
            clock=clock,
        )
        self.assertEqual(clock.calls, 1)

    def test_naive_clock_fails_closed_before_any_persist(self) -> None:
        store = FakeStore(INITIAL_DEADMAN_STATUS)
        sender = FakeSender()
        with self.assertRaises(DeadManRuntimeError):
            run_deadman_cycle(
                store=store,
                prober=FakeProber(DeadManProbeOutcome.HEALTHY),
                sender=sender,
                clock=FakeClock(datetime(2026, 9, 25, 12, 0)),
            )
        self.assertEqual(store.save_calls, [])
        self.assertEqual(sender.sent, [])

    def test_non_datetime_clock_fails_closed(self) -> None:
        with self.assertRaises(DeadManRuntimeError):
            run_deadman_cycle(
                store=FakeStore(INITIAL_DEADMAN_STATUS),
                prober=FakeProber(DeadManProbeOutcome.HEALTHY),
                sender=FakeSender(),
                clock=FakeClock("2026-09-25T12:00:00+00:00"),
            )

    def test_utc_now_is_timezone_aware(self) -> None:
        now = utc_now()
        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())


if __name__ == "__main__":
    unittest.main()
