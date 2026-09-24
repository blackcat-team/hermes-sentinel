"""Deterministic tests for the Stage H1A external dead-man core.

Covers the probe classification contract (the exact canonical
healthy signature and every rejection family), the UNKNOWN/UP/DOWN
debounce state machine (silent UP establishment, 3-failure DOWN
confirmation, 2-success recovery, counter resets), the durable
ORDERED pending-notification model (notification ids allocated once,
oldest-first head-only acknowledgement, no intent ever replaced,
dropped, reordered or duplicated, RECOVERED only for an acknowledged
DOWN), the CANONICAL OPERATIONAL SNAPSHOT persistence (state-change
time anchor, current-outage binding, no historical provenance —
paths with identical future behavior encode identically), the strict
versioned encode/decode boundary, the bounded model regression and
the H1A architectural boundaries. All tests exercise the real
production core with synthetic observations — no network, no files,
no real clock, no sleeps.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import deadman  # noqa: E402
from hermes_sentinel.deadman import (  # noqa: E402
    DOWN_CONFIRMATIONS,
    INITIAL_DEADMAN_STATUS,
    RECOVERY_CONFIRMATIONS,
    DeadManCurrentDown,
    DeadManNotification,
    DeadManNotificationKind,
    DeadManProbeOutcome,
    DeadManProbeResponse,
    DeadManState,
    DeadManStateDecodeError,
    DeadManStatus,
    DeadManTransition,
    acknowledge_deadman_notification,
    advance_deadman_status,
    classify_deadman_probe,
    decode_deadman_status,
    encode_deadman_status,
)

_UTC = timezone.utc
_PLUS3 = timezone(timedelta(hours=3))

_T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=_UTC)
_MINUTE = timedelta(seconds=60)


class _NoneOffsetZone(tzinfo):
    """A tzinfo whose utcoffset() is None (effectively naive).

    Mirrors the effectively-naive probe used by the Stage B/D
    boundaries: tzinfo is set but the full-awareness rule still
    rejects the value.
    """

    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "none-offset"


def _healthy_response() -> DeadManProbeResponse:
    """The exact canonical healthy probe signature."""
    return DeadManProbeResponse(
        status=405, allow_headers=("POST",), body=b""
    )


def _probe(
    status: int = 405,
    allow_headers: tuple[str, ...] = ("POST",),
    body: bytes = b"",
) -> DeadManProbeResponse:
    return DeadManProbeResponse(
        status=status, allow_headers=allow_headers, body=body
    )


def _classify(
    status: int = 405,
    allow_headers: tuple[str, ...] = ("POST",),
    body: bytes = b"",
) -> DeadManProbeOutcome:
    return classify_deadman_probe(_probe(status, allow_headers, body))


def _transition(
    from_state: DeadManState,
    to_state: DeadManState,
    at: datetime = _T0,
) -> DeadManTransition:
    return DeadManTransition(
        from_state=from_state, to_state=to_state, at=at
    )


def _notification(
    notification_id: int,
    kind: DeadManNotificationKind,
    from_state: DeadManState,
    to_state: DeadManState,
    at: datetime = _T0,
) -> DeadManNotification:
    return DeadManNotification(
        notification_id=notification_id,
        kind=kind,
        transition=_transition(from_state, to_state, at),
    )


def _down_intent(
    notification_id: int, at: datetime = _T0
) -> DeadManNotification:
    return _notification(
        notification_id, DeadManNotificationKind.DOWN,
        DeadManState.UP, DeadManState.DOWN, at,
    )


def _recovered_intent(
    notification_id: int, at: datetime = _T0
) -> DeadManNotification:
    return _notification(
        notification_id, DeadManNotificationKind.RECOVERED,
        DeadManState.DOWN, DeadManState.UP, at,
    )


def _up_status(
    at: datetime = _T0,
    pending: tuple[DeadManNotification, ...] = (),
    failures: int = 0,
    next_notification_id: int | None = None,
) -> DeadManStatus:
    """An established UP snapshot (anchor ``at``)."""
    computed = next_notification_id if next_notification_id is not None else (
        max((n.notification_id for n in pending), default=0) + 1
    )
    return DeadManStatus(
        state=DeadManState.UP,
        state_changed_at=at,
        pending_notifications=pending,
        consecutive_failures=failures,
        next_notification_id=computed,
    )


def _down_status(
    at: datetime = _T0,
    *,
    from_state: DeadManState = DeadManState.UP,
    acked: bool = False,
    successes: int = 0,
    notification_id: int = 1,
) -> DeadManStatus:
    """A confirmed DOWN snapshot; its DOWN intent is id
    ``notification_id`` (pending unless ``acked``), bound via
    ``current_down`` either way."""
    queue = (
        ()
        if acked
        else (_notification(
            notification_id, DeadManNotificationKind.DOWN,
            from_state, DeadManState.DOWN, at,
        ),)
    )
    return DeadManStatus(
        state=DeadManState.DOWN,
        state_changed_at=at,
        pending_notifications=queue,
        consecutive_successes=successes,
        next_notification_id=notification_id + 1,
        current_down=DeadManCurrentDown(
            notification_id=notification_id,
            acknowledged=acked,
        ),
    )


def _advance(
    status: DeadManStatus,
    outcome: DeadManProbeOutcome,
    at: datetime = _T0,
) -> DeadManStatus:
    return advance_deadman_status(
        status=status, outcome=outcome, now=at
    )


def _ack(status: DeadManStatus, notification_id: int) -> DeadManStatus:
    return acknowledge_deadman_notification(
        status=status, notification_id=notification_id
    )


def _run(
    start: DeadManStatus,
    outcomes: tuple[DeadManProbeOutcome, ...],
    *,
    first_at: datetime = _T0,
    step: timedelta = _MINUTE,
) -> list[DeadManStatus]:
    """Thread the immutable status through pure evaluations."""
    current = start
    results: list[DeadManStatus] = []
    moment = first_at
    for outcome in outcomes:
        current = _advance(current, outcome, moment)
        results.append(current)
        moment += step
    return results


def _outage(
    start: DeadManStatus,
    first_at: datetime,
) -> DeadManStatus:
    """Three consecutive failures: enter DOWN from ``start``."""
    return _run(
        start,
        (DeadManProbeOutcome.FAILED,) * DOWN_CONFIRMATIONS,
        first_at=first_at,
    )[-1]


def _recovery(
    start: DeadManStatus,
    first_at: datetime,
) -> DeadManStatus:
    """Two consecutive successes: leave DOWN."""
    return _run(
        start,
        (DeadManProbeOutcome.HEALTHY,) * RECOVERY_CONFIRMATIONS,
        first_at=first_at,
    )[-1]


class ProbeResponseContractTest(unittest.TestCase):
    """DeadManProbeResponse shape and secret/reflection safety."""

    def test_is_frozen_dataclass_with_slots(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(DeadManProbeResponse))
        self.assertTrue(
            DeadManProbeResponse.__dataclass_params__.frozen
        )
        response = _healthy_response()
        self.assertNotIn("__dict__", dir(response))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            response.status = 200  # type: ignore[misc]

    def test_structural_defaults_have_no_defaults(self) -> None:
        # Every field is mandatory: an observation must be complete.
        with self.assertRaises(TypeError):
            DeadManProbeResponse(status=405)  # type: ignore[call-arg]

    def test_bool_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManProbeResponse(
                status=True, allow_headers=("POST",), body=b""
            )

    def test_non_integer_status_rejected(self) -> None:
        for bad in ("405", 405.0, None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManProbeResponse(
                        status=bad,  # type: ignore[arg-type]
                        allow_headers=("POST",),
                        body=b"",
                    )

    def test_status_range_enforced(self) -> None:
        for bad in (99, 600, 0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManProbeResponse(
                        status=bad,
                        allow_headers=("POST",),
                        body=b"",
                    )

    def test_non_tuple_allow_headers_rejected(self) -> None:
        for bad in (["POST"], "POST", None, ("POST", 1)):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManProbeResponse(
                        status=405,
                        allow_headers=bad,  # type: ignore[arg-type]
                        body=b"",
                    )

    def test_non_bytes_body_rejected(self) -> None:
        for bad in ("", None, bytearray(b""), 0):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManProbeResponse(
                        status=405,
                        allow_headers=("POST",),
                        body=bad,  # type: ignore[arg-type]
                    )

    def test_repr_never_reflects_content(self) -> None:
        # The B4 HttpRequest repr precedent: network-controlled
        # Allow values and body content never appear in repr.
        response = DeadManProbeResponse(
            status=405,
            allow_headers=("X-SecretValue",),
            body=b"secret-response-body",
        )
        rendered = repr(response)
        self.assertIn("405", rendered)
        self.assertNotIn("X-SecretValue", rendered)
        self.assertNotIn("secret-response-body", rendered)


class ProbeClassificationTest(unittest.TestCase):
    """The canonical healthy signature and every rejection family."""

    def test_exact_healthy_signature_accepted(self) -> None:
        self.assertIs(
            _classify(status=405, allow_headers=("POST",), body=b""),
            DeadManProbeOutcome.HEALTHY,
        )

    def test_404_rejected(self) -> None:
        self.assertIs(
            _classify(status=404, allow_headers=("POST",), body=b""),
            DeadManProbeOutcome.FAILED,
        )

    def test_502_rejected(self) -> None:
        self.assertIs(
            _classify(status=502, allow_headers=("POST",), body=b""),
            DeadManProbeOutcome.FAILED,
        )

    def test_success_statuses_rejected(self) -> None:
        # A 2xx is never healthy dead-man evidence.
        for status in (200, 204, 301, 302):
            with self.subTest(status=status):
                self.assertIs(
                    _classify(
                        status=status, allow_headers=("POST",), body=b""
                    ),
                    DeadManProbeOutcome.FAILED,
                )

    def test_405_with_wrong_allow_rejected(self) -> None:
        for allow in ("GET", ("GET",), ("GET", "OPTIONS"), ("PUT",)):
            with self.subTest(allow=allow):
                self.assertIs(
                    _classify(status=405, allow_headers=tuple(allow)),
                    DeadManProbeOutcome.FAILED,
                )

    def test_405_without_allow_rejected(self) -> None:
        self.assertIs(
            _classify(status=405, allow_headers=(), body=b""),
            DeadManProbeOutcome.FAILED,
        )

    def test_405_with_empty_allow_value_rejected(self) -> None:
        self.assertIs(
            _classify(status=405, allow_headers=("",), body=b""),
            DeadManProbeOutcome.FAILED,
        )

    def test_405_with_lowercase_post_rejected(self) -> None:
        # HTTP methods are case-sensitive (the B4 contract): "post"
        # is not POST.
        self.assertIs(
            _classify(status=405, allow_headers=("post",), body=b""),
            DeadManProbeOutcome.FAILED,
        )

    def test_405_with_post_in_method_list_accepted(self) -> None:
        self.assertIs(
            _classify(
                status=405, allow_headers=("GET, POST",), body=b""
            ),
            DeadManProbeOutcome.HEALTHY,
        )

    def test_405_with_multiple_allow_headers_accepted(self) -> None:
        self.assertIs(
            _classify(
                status=405,
                allow_headers=("GET", "POST"),
                body=b"",
            ),
            DeadManProbeOutcome.HEALTHY,
        )

    def test_405_with_surrounding_whitespace_tokens_accepted(self) -> None:
        self.assertIs(
            _classify(
                status=405, allow_headers=("  GET ,\tPOST  ",), body=b""
            ),
            DeadManProbeOutcome.HEALTHY,
        )

    def test_non_empty_body_rejected(self) -> None:
        self.assertIs(
            _classify(
                status=405,
                allow_headers=("POST",),
                body=b"x",
            ),
            DeadManProbeOutcome.FAILED,
        )

    def test_whitespace_only_body_rejected(self) -> None:
        self.assertIs(
            _classify(
                status=405, allow_headers=("POST",), body=b" \n"
            ),
            DeadManProbeOutcome.FAILED,
        )

    def test_wrong_allow_and_body_combined_rejected(self) -> None:
        self.assertIs(
            _classify(status=405, allow_headers=("GET",), body=b"x"),
            DeadManProbeOutcome.FAILED,
        )

    def test_classification_requires_response_type(self) -> None:
        with self.assertRaises(ValueError):
            classify_deadman_probe(
                (405, ("POST",), b"")  # type: ignore[arg-type]
            )

    def test_classification_is_pure(self) -> None:
        response = _healthy_response()
        self.assertEqual(
            classify_deadman_probe(response),
            classify_deadman_probe(response),
        )
        self.assertEqual(response, _healthy_response())


class StatusContractTest(unittest.TestCase):
    """DeadManStatus canonical-snapshot invariants and the
    transition/notification/current-down value contracts."""

    def test_state_vocabulary_is_own_model(self) -> None:
        # Deliberately NOT the host HEALTHY/DEGRADED/DOWN model.
        self.assertEqual(
            {member.name for member in DeadManState},
            {"UNKNOWN", "UP", "DOWN"},
        )

    def test_is_frozen_dataclass_with_slots(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(DeadManStatus))
        self.assertTrue(DeadManStatus.__dataclass_params__.frozen)
        status = INITIAL_DEADMAN_STATUS
        self.assertNotIn("__dict__", dir(status))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            status.state = DeadManState.UP  # type: ignore[misc]

    def test_initial_status_shape(self) -> None:
        self.assertEqual(
            INITIAL_DEADMAN_STATUS,
            DeadManStatus(state=DeadManState.UNKNOWN),
        )
        self.assertEqual(
            INITIAL_DEADMAN_STATUS.consecutive_failures, 0
        )
        self.assertEqual(
            INITIAL_DEADMAN_STATUS.consecutive_successes, 0
        )
        self.assertIsNone(INITIAL_DEADMAN_STATUS.state_changed_at)
        self.assertEqual(
            INITIAL_DEADMAN_STATUS.pending_notifications, ()
        )
        self.assertEqual(INITIAL_DEADMAN_STATUS.next_notification_id, 1)
        self.assertIsNone(INITIAL_DEADMAN_STATUS.current_down)

    def test_non_enum_state_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(state="up")  # type: ignore[arg-type]

    def test_bool_and_non_integer_counters_rejected(self) -> None:
        for bad in (True, False, 1.0, "1", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManStatus(
                        state=DeadManState.UNKNOWN,
                        consecutive_failures=bad,  # type: ignore[arg-type]
                    )
                with self.assertRaises(ValueError):
                    DeadManStatus(
                        state=DeadManState.DOWN,
                        state_changed_at=_T0,
                        next_notification_id=2,
                        current_down=DeadManCurrentDown(1, False),
                        consecutive_successes=bad,  # type: ignore[arg-type]
                    )

    def test_negative_counters_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UNKNOWN, consecutive_failures=-1
            )

    def test_down_requires_zero_failures(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
                consecutive_failures=1,
            )

    def test_non_down_requires_zero_successes(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                consecutive_successes=1,
            )

    def test_resting_counter_never_reaches_threshold(self) -> None:
        # A streak at its threshold must have transitioned instead.
        self.assertEqual(DOWN_CONFIRMATIONS, 3)
        self.assertEqual(RECOVERY_CONFIRMATIONS, 2)
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                consecutive_failures=3,
            )
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, True),
                consecutive_successes=2,
            )

    def test_up_and_down_require_time_anchor(self) -> None:
        for builder in (
            lambda: DeadManStatus(state=DeadManState.UP),
            lambda: DeadManStatus(
                state=DeadManState.DOWN,
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
            ),
        ):
            with self.assertRaises(ValueError):
                builder()

    def test_unknown_rejects_time_anchor(self) -> None:
        # The anchor biconditional: None exactly in UNKNOWN.
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UNKNOWN, state_changed_at=_T0
            )

    def test_naive_anchor_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=datetime(2026, 9, 24, 12, 0, 0),
            )

    def test_effectively_naive_anchor_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=datetime(
                    2026, 9, 24, 12, 0, 0, tzinfo=_NoneOffsetZone()
                ),  # type: ignore[arg-type]
            )

    def test_non_datetime_anchor_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at="2026-09-24T12:00:00+00:00",  # type: ignore[arg-type]
            )

    def test_unknown_is_canonical_initial_only(self) -> None:
        # No pending intents, untouched allocator, no binding.
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UNKNOWN,
                pending_notifications=(_down_intent(1),),
                next_notification_id=2,
            )
        for bad_next in (2, 5):
            with self.subTest(next_notification_id=bad_next):
                with self.assertRaises(ValueError):
                    DeadManStatus(
                        state=DeadManState.UNKNOWN,
                        next_notification_id=bad_next,
                    )
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UNKNOWN,
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
            )

    def test_up_rejects_current_down(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
            )

    def test_down_requires_current_down(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                pending_notifications=(_down_intent(1),),
                next_notification_id=2,
            )

    def test_down_binding_must_be_latest_allocation(self) -> None:
        for bad_id in (0, 3, True, 1.0, "2"):
            with self.subTest(notification_id=bad_id):
                with self.assertRaises(ValueError):
                    DeadManStatus(
                        state=DeadManState.DOWN,
                        state_changed_at=_T0,
                        pending_notifications=(_down_intent(1),),
                        next_notification_id=2,
                        current_down=DeadManCurrentDown(bad_id, False),  # type: ignore[arg-type]
                    )

    def test_down_unacknowledged_requires_pending_tail(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                pending_notifications=(),
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
            )

    def test_down_tail_must_be_down_kind(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                pending_notifications=(_recovered_intent(1),),
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
            )

    def test_down_tail_must_match_anchor_time(self) -> None:
        # The binding's "transition facts": the pending tail must be
        # dated exactly at the outage entry moment (state_changed_at).
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                pending_notifications=(_down_intent(1, at=_T0 - _MINUTE),),
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, False),
            )

    def test_down_acknowledged_requires_drained_queue(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.DOWN,
                state_changed_at=_T0,
                pending_notifications=(_down_intent(1),),
                next_notification_id=2,
                current_down=DeadManCurrentDown(1, True),
            )

    def test_current_down_shape(self) -> None:
        for bad_ack in (0, 1, "false", None):
            with self.subTest(acknowledged=bad_ack):
                with self.assertRaises(ValueError):
                    DeadManCurrentDown(
                        notification_id=1,
                        acknowledged=bad_ack,  # type: ignore[arg-type]
                    )
        for bad_id in (True, 0, -1, 1.0, "1"):
            with self.subTest(notification_id=bad_id):
                with self.assertRaises(ValueError):
                    DeadManCurrentDown(
                        notification_id=bad_id,  # type: ignore[arg-type]
                        acknowledged=False,
                    )

    def test_pending_must_be_a_tuple(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=[_down_intent(1)],  # type: ignore[arg-type]
                next_notification_id=2,
            )

    def test_pending_elements_must_be_notifications(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=("not-a-notification",),  # type: ignore[arg-type]
                next_notification_id=2,
            )

    def test_pending_ids_strictly_increasing(self) -> None:
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=(
                    _down_intent(2),
                    _down_intent(1),
                ),
                next_notification_id=3,
            )
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=(
                    _down_intent(1),
                    _down_intent(1),
                ),
                next_notification_id=3,
            )

    def test_next_notification_id_shape(self) -> None:
        for bad in (True, 0, -1, 1.0, "2", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManStatus(
                        state=DeadManState.UNKNOWN,
                        next_notification_id=bad,  # type: ignore[arg-type]
                    )

    def test_pending_dated_after_anchor_rejected(self) -> None:
        # Ledger time sanity: pending intents cannot be newer than
        # the current state's beginning.
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=(
                    _down_intent(1, at=_T0 + _MINUTE),
                ),
                next_notification_id=2,
            )

    def test_transition_construction_rules(self) -> None:
        # The three intent-observable families are constructible.
        for from_state, to_state in (
            (DeadManState.UNKNOWN, DeadManState.DOWN),
            (DeadManState.UP, DeadManState.DOWN),
            (DeadManState.DOWN, DeadManState.UP),
        ):
            with self.subTest(pair=(from_state, to_state)):
                self.assertIsInstance(
                    _transition(from_state, to_state),
                    DeadManTransition,
                )
        # Everything else is rejected (including UNKNOWN -> UP,
        # which no intent can carry).
        for from_state in DeadManState:
            for to_state in DeadManState:
                if (from_state, to_state) in (
                    (DeadManState.UNKNOWN, DeadManState.DOWN),
                    (DeadManState.UP, DeadManState.DOWN),
                    (DeadManState.DOWN, DeadManState.UP),
                ):
                    continue
                with self.subTest(pair=(from_state, to_state)):
                    with self.assertRaises(ValueError):
                        _transition(from_state, to_state)

    def test_transition_naive_at_rejected(self) -> None:
        naive = datetime(2026, 9, 24, 12, 0, 0)
        with self.assertRaises(ValueError):
            DeadManTransition(
                from_state=DeadManState.UP,
                to_state=DeadManState.DOWN,
                at=naive,
            )

    def test_transition_effectively_naive_at_rejected(self) -> None:
        effectively_naive = datetime(
            2026, 9, 24, 12, 0, 0, tzinfo=_NoneOffsetZone()
        )
        with self.assertRaises(ValueError):
            DeadManTransition(
                from_state=DeadManState.UP,
                to_state=DeadManState.DOWN,
                at=effectively_naive,  # type: ignore[arg-type]
            )

    def test_notification_id_shape(self) -> None:
        for bad in (True, 0, -1, 1.0, "1", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DeadManNotification(
                        notification_id=bad,  # type: ignore[arg-type]
                        kind=DeadManNotificationKind.DOWN,
                        transition=_transition(
                            DeadManState.UP, DeadManState.DOWN
                        ),
                    )

    def test_recovered_id_one_rejected(self) -> None:
        # The first allocation of any real timeline is necessarily
        # a DOWN intent.
        with self.assertRaises(ValueError):
            _recovered_intent(1)

    def test_notification_kind_must_agree_with_transition(self) -> None:
        down_event = _transition(DeadManState.UP, DeadManState.DOWN)
        recovery_event = _transition(DeadManState.DOWN, DeadManState.UP)
        with self.assertRaises(ValueError):
            DeadManNotification(
                notification_id=2,
                kind=DeadManNotificationKind.DOWN,
                transition=recovery_event,
            )
        with self.assertRaises(ValueError):
            DeadManNotification(
                notification_id=2,
                kind=DeadManNotificationKind.RECOVERED,
                transition=down_event,
            )
        with self.assertRaises(ValueError):
            DeadManNotification(
                notification_id=2,
                kind="down",  # type: ignore[arg-type]
                transition=down_event,
            )

    def test_notification_projects_transition(self) -> None:
        down_event = _transition(DeadManState.UP, DeadManState.DOWN)
        notification = DeadManNotification(
            notification_id=1,
            kind=DeadManNotificationKind.DOWN,
            transition=down_event,
        )
        self.assertEqual(notification.notification_id, 1)
        self.assertIs(notification.at, down_event.at)
        self.assertIs(notification.from_state, DeadManState.UP)
        self.assertIs(notification.to_state, DeadManState.DOWN)


class UnknownEstablishmentTest(unittest.TestCase):
    """UNKNOWN: silent UP baseline or 3-failure DOWN confirmation."""

    def test_first_success_establishes_up_silently(self) -> None:
        advanced = _advance(
            INITIAL_DEADMAN_STATUS, DeadManProbeOutcome.HEALTHY
        )
        self.assertIs(advanced.state, DeadManState.UP)
        self.assertEqual(advanced.consecutive_failures, 0)
        self.assertEqual(advanced.consecutive_successes, 0)
        self.assertEqual(advanced.state_changed_at, _T0)
        self.assertEqual(advanced.pending_notifications, ())
        self.assertEqual(advanced.next_notification_id, 1)

    def test_no_startup_notification_on_establishment(self) -> None:
        advanced = _advance(
            INITIAL_DEADMAN_STATUS, DeadManProbeOutcome.HEALTHY
        )
        self.assertEqual(advanced.pending_notifications, ())
        self.assertEqual(advanced.next_notification_id, 1)

    def test_failures_one_and_two_stay_unknown(self) -> None:
        results = _run(
            INITIAL_DEADMAN_STATUS,
            (DeadManProbeOutcome.FAILED,) * 2,
        )
        self.assertEqual(
            [(r.state, r.consecutive_failures) for r in results],
            [
                (DeadManState.UNKNOWN, 1),
                (DeadManState.UNKNOWN, 2),
            ],
        )
        for result in results:
            self.assertEqual(result.pending_notifications, ())
            self.assertIsNone(result.state_changed_at)

    def test_third_failure_confirms_down_with_intent(self) -> None:
        results = _run(
            INITIAL_DEADMAN_STATUS,
            (DeadManProbeOutcome.FAILED,) * 3,
        )
        final = results[-1]
        self.assertIs(final.state, DeadManState.DOWN)
        self.assertEqual(final.consecutive_failures, 0)
        self.assertEqual(final.consecutive_successes, 0)
        self.assertEqual(final.state_changed_at, _T0 + 2 * _MINUTE)
        self.assertEqual(final.next_notification_id, 2)
        self.assertEqual(len(final.pending_notifications), 1)
        pending = final.pending_notifications[0]
        self.assertEqual(pending.notification_id, 1)
        self.assertIs(pending.kind, DeadManNotificationKind.DOWN)
        self.assertEqual(pending.at, _T0 + 2 * _MINUTE)
        current = final.current_down
        assert current is not None
        self.assertEqual(current.notification_id, 1)
        self.assertIs(current.acknowledged, False)

    def test_success_during_unknown_failures_resets_counter(self) -> None:
        results = _run(
            INITIAL_DEADMAN_STATUS,
            (
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.HEALTHY,
            ),
        )
        self.assertIs(results[2].state, DeadManState.UP)
        self.assertEqual(results[2].consecutive_failures, 0)
        # The reset is real: two more failures after the reset do
        # not confirm DOWN.
        tail = _run(
            results[2],
            (DeadManProbeOutcome.FAILED,) * 2,
            first_at=_T0 + 3 * _MINUTE,
        )
        self.assertIs(tail[-1].state, DeadManState.UP)
        self.assertEqual(tail[-1].consecutive_failures, 2)


class UpDebounceTest(unittest.TestCase):
    """UP: failure debounce and counter reset semantics."""

    def test_first_and_second_failure_stay_up(self) -> None:
        results = _run(
            _up_status(),
            (DeadManProbeOutcome.FAILED,) * 2,
        )
        self.assertEqual(
            [(r.state, r.consecutive_failures) for r in results],
            [
                (DeadManState.UP, 1),
                (DeadManState.UP, 2),
            ],
        )
        for result in results:
            self.assertEqual(result.pending_notifications, ())

    def test_third_failure_confirms_down_with_intent(self) -> None:
        results = _run(
            _up_status(),
            (DeadManProbeOutcome.FAILED,) * 3,
        )
        final = results[-1]
        self.assertIs(final.state, DeadManState.DOWN)
        self.assertEqual(final.state_changed_at, _T0 + 2 * _MINUTE)
        self.assertEqual(final.next_notification_id, 2)
        pending = final.pending_notifications
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].notification_id, 1)
        self.assertIs(pending[0].kind, DeadManNotificationKind.DOWN)
        self.assertEqual(pending[0].at, _T0 + 2 * _MINUTE)

    def test_success_resets_failure_counter(self) -> None:
        results = _run(
            _up_status(),
            (
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.HEALTHY,
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.FAILED,
            ),
        )
        self.assertEqual(
            [(r.state, r.consecutive_failures) for r in results],
            [
                (DeadManState.UP, 1),
                (DeadManState.UP, 2),
                (DeadManState.UP, 0),
                (DeadManState.UP, 1),
                (DeadManState.UP, 2),
            ],
        )

    def test_repeated_successes_stay_up(self) -> None:
        results = _run(
            _up_status(),
            (DeadManProbeOutcome.HEALTHY,) * 3,
        )
        for result in results:
            self.assertIs(result.state, DeadManState.UP)
            self.assertEqual(
                (result.consecutive_failures,
                 result.consecutive_successes),
                (0, 0),
            )
            self.assertEqual(result.pending_notifications, ())
        # The anchor is preserved, not restamped.
        self.assertEqual(
            results[0].state_changed_at, results[2].state_changed_at
        )


class DownRecoveryTest(unittest.TestCase):
    """DOWN: hold, recovery debounce and RECOVERED gating."""

    def test_failure_keeps_down_and_resets_successes(self) -> None:
        pending_recovery = _down_status(successes=1)
        advanced = _advance(pending_recovery, DeadManProbeOutcome.FAILED)
        self.assertIs(advanced.state, DeadManState.DOWN)
        self.assertEqual(advanced.consecutive_successes, 0)
        self.assertEqual(advanced.consecutive_failures, 0)
        self.assertEqual(
            advanced.current_down,
            pending_recovery.current_down,
        )

    def test_first_success_stays_down(self) -> None:
        advanced = _advance(
            _down_status(), DeadManProbeOutcome.HEALTHY
        )
        self.assertIs(advanced.state, DeadManState.DOWN)
        self.assertEqual(advanced.consecutive_successes, 1)
        self.assertEqual(advanced.consecutive_failures, 0)

    def test_second_success_confirms_up(self) -> None:
        results = _run(
            _down_status(),
            (DeadManProbeOutcome.HEALTHY,) * 2,
        )
        final = results[-1]
        self.assertIs(final.state, DeadManState.UP)
        self.assertEqual(
            (final.consecutive_failures, final.consecutive_successes),
            (0, 0),
        )
        self.assertEqual(final.state_changed_at, _T0 + _MINUTE)

    def test_recovered_intent_after_acknowledged_down(self) -> None:
        results = _run(
            _down_status(acked=True),
            (DeadManProbeOutcome.HEALTHY,) * 2,
        )
        final = results[-1]
        self.assertEqual(len(final.pending_notifications), 1)
        pending = final.pending_notifications[0]
        self.assertIs(pending.kind, DeadManNotificationKind.RECOVERED)
        self.assertEqual(pending.notification_id, 2)
        self.assertEqual(pending.at, _T0 + _MINUTE)
        self.assertIs(pending.from_state, DeadManState.DOWN)
        self.assertIs(pending.to_state, DeadManState.UP)
        self.assertEqual(final.next_notification_id, 3)

    def test_unacknowledged_down_recovery_emits_no_recovered(self) -> None:
        results = _run(
            _down_status(acked=False),
            (DeadManProbeOutcome.HEALTHY,) * 2,
        )
        final = results[-1]
        self.assertIs(final.state, DeadManState.UP)
        # No misleading RECOVERED: the outstanding DOWN intent stays.
        self.assertEqual(len(final.pending_notifications), 1)
        self.assertIs(
            final.pending_notifications[0].kind,
            DeadManNotificationKind.DOWN,
        )
        # No id was consumed by a suppressed RECOVERED.
        self.assertEqual(final.next_notification_id, 2)

    def test_unacknowledged_down_intent_survives_recovery(self) -> None:
        # The DOWN alert is never silently discarded by recovery.
        down = _down_status(acked=False)
        results = _run(down, (DeadManProbeOutcome.HEALTHY,) * 2)
        self.assertEqual(
            results[-1].pending_notifications,
            down.pending_notifications,
        )
        self.assertIs(
            results[-1].pending_notifications,
            down.pending_notifications,
        )

    def test_recovery_requires_consecutive_successes(self) -> None:
        results = _run(
            _down_status(),
            (
                DeadManProbeOutcome.HEALTHY,
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.HEALTHY,
                DeadManProbeOutcome.HEALTHY,
            ),
        )
        self.assertEqual(
            [(r.state, r.consecutive_successes) for r in results],
            [
                (DeadManState.DOWN, 1),
                (DeadManState.DOWN, 0),
                (DeadManState.DOWN, 1),
                (DeadManState.UP, 0),
            ],
        )


class NotificationDurabilityTest(unittest.TestCase):
    """Adversarial sequences: no intent is ever lost, replaced or
    reordered; ids are durable identity independent of timestamps."""

    def test_scenario_a_unacked_down_survives_recovery(self) -> None:
        # A: DOWN A pending -> recovery before acknowledgement ->
        # A remains pending -> no RECOVERED for A.
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        self.assertEqual(
            [n.notification_id for n in down_a.pending_notifications],
            [1],
        )
        recovered = _recovery(down_a, _T0 + 3 * _MINUTE)
        self.assertIs(recovered.state, DeadManState.UP)
        self.assertEqual(
            [n.notification_id for n in recovered.pending_notifications],
            [1],
        )
        self.assertIs(
            recovered.pending_notifications[0].kind,
            DeadManNotificationKind.DOWN,
        )

    def test_scenario_b_second_outage_preserves_order(self) -> None:
        # B: DOWN A pending -> recovery -> new outage -> DOWN B
        # appended -> pending order preserves A then B; neither is
        # lost.
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down_a, _T0 + 3 * _MINUTE)
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        self.assertIs(down_b.state, DeadManState.DOWN)
        queue = down_b.pending_notifications
        self.assertEqual(
            [(n.notification_id, n.kind) for n in queue],
            [
                (1, DeadManNotificationKind.DOWN),
                (2, DeadManNotificationKind.DOWN),
            ],
        )
        # A is untouched — same object, same transition moment.
        self.assertIs(queue[0], down_a.pending_notifications[0])
        self.assertEqual(down_b.next_notification_id, 3)

    def test_scenario_c_recovered_and_down_pending_in_order(self) -> None:
        # C: DOWN A acknowledged -> recovery creates RECOVERED A ->
        # RECOVERED A unacknowledged -> new outage creates DOWN B ->
        # both remain pending in deterministic order.
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        acknowledged = _ack(down_a, 1)
        up = _recovery(acknowledged, _T0 + 3 * _MINUTE)
        self.assertEqual(
            [(n.notification_id, n.kind) for n in up.pending_notifications],
            [(2, DeadManNotificationKind.RECOVERED)],
        )
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        queue = down_b.pending_notifications
        self.assertEqual(
            [(n.notification_id, n.kind) for n in queue],
            [
                (2, DeadManNotificationKind.RECOVERED),
                (3, DeadManNotificationKind.DOWN),
            ],
        )
        self.assertEqual(down_b.next_notification_id, 4)

    def test_scenario_d_identical_datetimes_distinct_ids(self) -> None:
        # D: two distinct DOWN events with the identical explicit
        # datetime allocate distinct ids.
        current = INITIAL_DEADMAN_STATUS
        for _ in range(DOWN_CONFIRMATIONS):
            current = _advance(current, DeadManProbeOutcome.FAILED, _T0)
        down_a = current
        first = down_a.pending_notifications[0]
        current = _ack(current, first.notification_id)
        for _ in range(RECOVERY_CONFIRMATIONS):
            current = _advance(current, DeadManProbeOutcome.HEALTHY, _T0)
        for _ in range(DOWN_CONFIRMATIONS):
            current = _advance(current, DeadManProbeOutcome.FAILED, _T0)
        down_b = current
        intents = {
            n.notification_id: n for n in down_b.pending_notifications
        }
        second = intents[3]
        self.assertIs(first.kind, DeadManNotificationKind.DOWN)
        self.assertIs(second.kind, DeadManNotificationKind.DOWN)
        self.assertEqual(first.at, second.at)
        self.assertNotEqual(
            first.notification_id, second.notification_id
        )
        self.assertNotEqual(first, second)

    def test_scenario_j_repeated_ticks_never_duplicate(self) -> None:
        # J: repeated same-state ticks create no duplicate intents
        # and never rewrite the queue or the binding.
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        queue = down.pending_notifications
        current = down
        moment = _T0 + 3 * _MINUTE
        for _ in range(5):
            current = _advance(current, DeadManProbeOutcome.FAILED, moment)
            self.assertIs(current.state, DeadManState.DOWN)
            self.assertIs(current.pending_notifications, queue)
            self.assertEqual(current.next_notification_id, 2)
            self.assertEqual(
                current.current_down, down.current_down
            )
            moment += _MINUTE


class CurrentDownBindingTest(unittest.TestCase):
    """The explicit current-outage binding semantics."""

    def test_down_entry_binds_allocated_id(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        current = down.current_down
        assert current is not None
        self.assertEqual(
            current.notification_id,
            down.next_notification_id - 1,
        )
        self.assertIs(current.acknowledged, False)

    def test_binding_stable_across_failed_probes(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        binding = down.current_down
        current = down
        moment = _T0 + 3 * _MINUTE
        for _ in range(4):
            current = _advance(current, DeadManProbeOutcome.FAILED, moment)
            self.assertEqual(current.current_down, binding)
            moment += _MINUTE

    def test_acknowledging_bound_down_records_fact(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        acknowledged = _ack(down, 1)
        # Queue removal does not lose the acknowledgement fact.
        self.assertEqual(acknowledged.pending_notifications, ())
        current = acknowledged.current_down
        assert current is not None
        self.assertEqual(current.notification_id, 1)
        self.assertIs(current.acknowledged, True)

    def test_acknowledging_older_intent_keeps_binding(self) -> None:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down_a, _T0 + 3 * _MINUTE)
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        partially = _ack(down_b, 1)  # ack the OLDER outage only.
        current = partially.current_down
        assert current is not None
        self.assertEqual(current.notification_id, 2)
        self.assertIs(current.acknowledged, False)
        self.assertEqual(
            [n.notification_id for n in partially.pending_notifications],
            [2],
        )

    def test_recovery_clears_binding_when_acknowledged(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        acknowledged = _ack(down, 1)
        up = _recovery(acknowledged, _T0 + 3 * _MINUTE)
        self.assertIsNone(up.current_down)
        # The RECOVERED intent WAS emitted for the acked outage.
        self.assertEqual(
            [(n.notification_id, n.kind) for n in up.pending_notifications],
            [(2, DeadManNotificationKind.RECOVERED)],
        )

    def test_recovery_clears_binding_when_unacknowledged(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down, _T0 + 3 * _MINUTE)
        self.assertIsNone(up.current_down)
        # No RECOVERED; the DOWN intent of that outage stays pending.
        self.assertEqual(
            [(n.notification_id, n.kind) for n in up.pending_notifications],
            [(1, DeadManNotificationKind.DOWN)],
        )


class ProcessPersistenceRegressionTest(unittest.TestCase):
    """Encode/decode restart must preserve the recovery decision
    (the canonical snapshot roundtrips the binding exactly)."""

    def test_restart_after_acknowledged_down_emits_recovered(self) -> None:
        up = _advance(
            INITIAL_DEADMAN_STATUS, DeadManProbeOutcome.HEALTHY, _T0
        )
        down = _outage(up, _T0 + _MINUTE)
        acknowledged = _ack(down, 1)
        restarted = decode_deadman_status(
            encode_deadman_status(acknowledged)
        )
        self.assertEqual(restarted, acknowledged)
        recovered = _recovery(restarted, _T0 + 5 * _MINUTE)
        self.assertIs(recovered.state, DeadManState.UP)
        self.assertEqual(
            [(n.notification_id, n.kind) for n in recovered.pending_notifications],
            [(2, DeadManNotificationKind.RECOVERED)],
        )

    def test_restart_with_unacknowledged_down_emits_no_recovered(self):
        up = _advance(
            INITIAL_DEADMAN_STATUS, DeadManProbeOutcome.HEALTHY, _T0
        )
        down = _outage(up, _T0 + _MINUTE)
        restarted = decode_deadman_status(encode_deadman_status(down))
        self.assertEqual(restarted, down)
        recovered = _recovery(restarted, _T0 + 5 * _MINUTE)
        self.assertIs(recovered.state, DeadManState.UP)
        self.assertEqual(
            [(n.notification_id, n.kind) for n in recovered.pending_notifications],
            [(1, DeadManNotificationKind.DOWN)],
        )

    def test_restart_with_multiple_pending_intents(self) -> None:
        # Telegram unavailable across multiple outages, then a
        # restart: the full ordered queue survives.
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down_a, _T0 + 3 * _MINUTE)
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        restarted = decode_deadman_status(encode_deadman_status(down_b))
        self.assertEqual(restarted, down_b)
        self.assertEqual(
            [n.notification_id for n in restarted.pending_notifications],
            [1, 2],
        )
        # Oldest-first delivery resumes after the restart.
        first = _ack(restarted, 1)
        second = _ack(first, 2)
        self.assertEqual(second.pending_notifications, ())
        current = second.current_down
        assert current is not None
        self.assertIs(current.acknowledged, True)

    def test_canonicalization_equal_future_equal_encoding(self) -> None:
        # Two different historical paths with identical future
        # behavior serialize to the identical canonical snapshot:
        # startup-then-down vs establish-then-down, both acked at
        # the same outage moment.
        startup_down = _ack(
            _outage(INITIAL_DEADMAN_STATUS, _T0 - 2 * _MINUTE), 1
        )
        established = _advance(
            INITIAL_DEADMAN_STATUS,
            DeadManProbeOutcome.HEALTHY,
            _T0 - 5 * _MINUTE,
        )
        established_down = _ack(
            _outage(established, _T0 - 2 * _MINUTE), 1
        )
        self.assertEqual(startup_down, established_down)
        self.assertEqual(
            encode_deadman_status(startup_down),
            encode_deadman_status(established_down),
        )

    def test_decode_rejects_down_bound_to_wrong_intent_time(self):
        # The binding's tail intent must be dated exactly at the
        # outage entry (state_changed_at) — a mismatched moment is
        # an inconsistent binding.
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        document = json.loads(encode_deadman_status(down))
        assert document["pending_notifications"]
        document["pending_notifications"][0]["transition"]["at"] = (
            "2026-09-24T11:00:00+00:00"
        )
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_bad_binding_fields(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        for field, bad_values in (
            ("notification_id", (0, -1, True, 1.5, "1")),
            ("acknowledged", (0, 1, "false", None)),
        ):
            for bad in bad_values:
                with self.subTest(field=field, value=bad):
                    document = json.loads(encode_deadman_status(down))
                    document["current_down"][field] = bad
                    with self.assertRaises(DeadManStateDecodeError):
                        decode_deadman_status(json.dumps(document))

    def test_decode_rejects_down_without_binding(self) -> None:
        down = _outage(INITIAL_DEADMAN_STATUS, _T0)
        document = json.loads(encode_deadman_status(down))
        document["current_down"] = None
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_metadata_outside_down(self) -> None:
        up = _up_status(at=_T0)
        for value in (
            {"notification_id": 1, "acknowledged": False},
            {"notification_id": 1, "acknowledged": True},
        ):
            with self.subTest(current_down=value):
                document = json.loads(encode_deadman_status(up))
                document["current_down"] = value
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))

    def test_decode_rejects_unknown_with_allocated_sequence(self) -> None:
        document = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        document["next_notification_id"] = 5
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))


class NotificationIdentityTest(unittest.TestCase):
    """Persisted integer identity: allocation, uniqueness, growth."""

    def test_ids_allocate_once_and_increase(self) -> None:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        self.assertEqual(down_a.next_notification_id, 2)
        acknowledged = _ack(down_a, 1)
        up = _recovery(acknowledged, _T0 + 3 * _MINUTE)
        self.assertEqual(up.next_notification_id, 3)
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        self.assertEqual(down_b.next_notification_id, 4)

    def test_ids_independent_of_timestamps(self) -> None:
        first = _outage(INITIAL_DEADMAN_STATUS, _T0)
        second = _outage(
            _recovery(_ack(first, 1), _T0 + 3 * _MINUTE),
            datetime(2031, 1, 2, 3, 4, 5, tzinfo=_UTC),
        )
        self.assertEqual(
            first.pending_notifications[0].notification_id, 1
        )
        self.assertEqual(
            [n.notification_id for n in second.pending_notifications],
            [2, 3],
        )

    def test_acknowledgement_never_changes_next_id(self) -> None:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        acknowledged = _ack(down_a, 1)
        self.assertEqual(
            acknowledged.next_notification_id,
            down_a.next_notification_id,
        )


class NotificationAcknowledgementTest(unittest.TestCase):
    """Oldest-first, by-id acknowledgement semantics."""

    def _two_pending(self) -> DeadManStatus:
        """DOWN B carrying (older DOWN A@1, current DOWN B@2)."""
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down_a, _T0 + 3 * _MINUTE)
        return _outage(up, _T0 + 5 * _MINUTE)

    def test_acknowledgement_clears_head_intent_only(self) -> None:
        status = self._two_pending()
        acknowledged = _ack(status, 1)
        self.assertEqual(
            [n.notification_id for n in acknowledged.pending_notifications],
            [2],
        )
        # Nothing else is touched.
        self.assertIs(acknowledged.state, status.state)
        self.assertEqual(acknowledged.state_changed_at, status.state_changed_at)
        self.assertEqual(
            acknowledged.consecutive_failures,
            status.consecutive_failures,
        )
        self.assertEqual(
            acknowledged.next_notification_id,
            status.next_notification_id,
        )

    def test_acknowledgement_drains_oldest_first(self) -> None:
        status = self._two_pending()
        first = _ack(status, 1)
        second = _ack(first, 2)
        self.assertEqual(second.pending_notifications, ())
        self.assertEqual(second.next_notification_id, 3)

    def test_acknowledgement_with_nothing_pending_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ack(INITIAL_DEADMAN_STATUS, 1)

    def test_acknowledgement_rejects_non_integer_ids(self) -> None:
        status = self._two_pending()
        for bad in (True, "1", 1.0, None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    _ack(status, bad)  # type: ignore[arg-type]

    def test_acknowledgement_is_not_repeatable(self) -> None:
        status = self._two_pending()
        acknowledged = _ack(status, 1)
        with self.assertRaises(ValueError):
            _ack(acknowledged, 1)

    def test_stale_ack_never_clears_newer_down(self) -> None:
        # E: a stale acknowledgement of an already-acknowledged old
        # DOWN id must fail and leave the newer DOWN pending.
        status = self._two_pending()
        drained = _ack(status, 1)  # A was the head: legitimate.
        self.assertEqual(
            [n.notification_id for n in drained.pending_notifications],
            [2],
        )
        with self.assertRaises(ValueError):
            _ack(drained, 1)  # stale: id 1 is already acknowledged.
        self.assertEqual(
            [n.notification_id for n in drained.pending_notifications],
            [2],
        )
        self.assertIs(
            drained.pending_notifications[0].kind,
            DeadManNotificationKind.DOWN,
        )

    def test_non_head_ack_fails_queue_unchanged(self) -> None:
        # F: acknowledging a wrong/non-head id fails explicitly and
        # leaves the queue unchanged.
        status = self._two_pending()
        for bad_id in (2, 99, 0, -1):
            with self.subTest(notification_id=bad_id):
                with self.assertRaises(ValueError):
                    _ack(status, bad_id)
        self.assertEqual(
            [n.notification_id for n in status.pending_notifications],
            [1, 2],
        )

    def test_acknowledgement_requires_status_type(self) -> None:
        with self.assertRaises(ValueError):
            acknowledge_deadman_notification(
                status="down",  # type: ignore[arg-type]
                notification_id=1,
            )

    def test_full_lifecycle(self) -> None:
        # UP established -> 3 failures confirm DOWN (+intent 1) ->
        # acknowledged -> 2 successes confirm UP (+RECOVERED 2) ->
        # acknowledged -> stable UP with an empty queue.
        down = _run(
            INITIAL_DEADMAN_STATUS,
            (
                DeadManProbeOutcome.HEALTHY,
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.FAILED,
                DeadManProbeOutcome.FAILED,
            ),
        )[-1]
        self.assertIs(down.state, DeadManState.DOWN)
        self.assertEqual(down.next_notification_id, 2)

        acknowledged = _ack(down, 1)
        recovered = _recovery(acknowledged, _T0 + 4 * _MINUTE)
        self.assertIs(recovered.state, DeadManState.UP)
        queue = recovered.pending_notifications
        self.assertEqual(len(queue), 1)
        self.assertIs(queue[0].kind, DeadManNotificationKind.RECOVERED)

        settled = _ack(recovered, queue[0].notification_id)
        stable = _run(
            settled,
            (DeadManProbeOutcome.HEALTHY,) * 2,
            first_at=_T0 + 7 * _MINUTE,
        )
        for result in stable:
            self.assertIs(result.state, DeadManState.UP)
            self.assertEqual(result.pending_notifications, ())
        self.assertEqual(stable[-1].next_notification_id, 3)


class LedgerReachabilityTest(unittest.TestCase):
    """Notification-ledger continuity and grammar reachability."""

    def test_pending_ids_with_gap_rejected(self) -> None:
        # [3, 5] with next 6: oldest-first acknowledgement plus
        # gapless allocation can never leave a hole.
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=(
                    _down_intent(3),
                    _down_intent(5),
                ),
                next_notification_id=6,
            )

    def test_pending_ids_with_interior_hole_rejected(self) -> None:
        # [2, 4, 5] with next 6: a hole between pending ids.
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=(
                    _down_intent(2),
                    _down_intent(4),
                    _down_intent(5),
                ),
                next_notification_id=6,
            )

    def test_pending_tail_not_anchored_rejected(self) -> None:
        # [3, 4] with next 6: the newest pending id must equal the
        # most recent allocation (5), not fall short of it.
        with self.assertRaises(ValueError):
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                pending_notifications=(
                    _down_intent(3),
                    _down_intent(4),
                ),
                next_notification_id=6,
            )

    def test_contiguous_anchored_suffix_accepted(self) -> None:
        # [3, 4, 5] with next 6 is a legitimate reachable suffix.
        status = DeadManStatus(
            state=DeadManState.UP,
            state_changed_at=_T0,
            pending_notifications=tuple(
                _down_intent(notification_id)
                for notification_id in (3, 4, 5)
            ),
            next_notification_id=6,
        )
        self.assertEqual(
            [n.notification_id for n in status.pending_notifications],
            [3, 4, 5],
        )

    def test_drained_queue_with_history_accepted(self) -> None:
        # Empty pending with an advanced sequence is legitimate
        # (everything acknowledged) — the snapshot intentionally
        # stores no history to prove or disprove.
        status = DeadManStatus(
            state=DeadManState.UP,
            state_changed_at=_T0,
            pending_notifications=(),
            next_notification_id=6,
        )
        self.assertEqual(status.next_notification_id, 6)

    def test_decode_rejects_pending_gap(self) -> None:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        acknowledged = _ack(down_a, 1)
        up = _recovery(acknowledged, _T0 + 3 * _MINUTE)
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        document = json.loads(encode_deadman_status(down_b))
        assert [n["notification_id"] for n in document[
            "pending_notifications"
        ]] == [2, 3]
        document["pending_notifications"][0]["notification_id"] = 1
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_unanchored_pending_tail(self) -> None:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down_a, _T0 + 3 * _MINUTE)
        down_b = _outage(up, _T0 + 5 * _MINUTE)
        document = json.loads(encode_deadman_status(down_b))
        document["next_notification_id"] = 4
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))


class PendingLedgerGrammarTest(unittest.TestCase):
    """The pending-kind grammar RECOVERED? DOWN*.

    Every kind sequence of length 0..4 over {DOWN, RECOVERED} is
    built with NUMERICALLY VALID contiguous ids (so rejection is
    purely SEMANTIC) and classified against the derived grammar:
    accepted with an exact encode/decode roundtrip when grammar-
    valid, rejected by constructor and decoder when not.
    """

    @staticmethod
    def _status_from_kinds(
        kinds: tuple[str, ...],
    ) -> DeadManStatus:
        """A numerically valid UP snapshot whose pending kinds are
        exactly ``kinds`` (contiguous ids from 2 — id 1 is reserved
        for the necessarily-DOWN first allocation — anchored tail)."""
        return DeadManStatus(
            state=DeadManState.UP,
            state_changed_at=_T0 + _MINUTE,
            pending_notifications=tuple(
                _notification(
                    index + 2,
                    DeadManNotificationKind(kind),
                    DeadManState.UP
                    if kind == DeadManNotificationKind.DOWN.value
                    else DeadManState.DOWN,
                    DeadManState.DOWN
                    if kind == DeadManNotificationKind.DOWN.value
                    else DeadManState.UP,
                    _T0,
                )
                for index, kind in enumerate(kinds)
            ),
            next_notification_id=len(kinds) + 2,
        )

    @staticmethod
    def _pending_document(kinds: tuple[str, ...]) -> str:
        """A numerically valid persisted document with pending
        kinds exactly ``kinds`` (schema v4, UP, contiguous ids from
        2, anchored tail)."""

        def intent(index: int, kind: str) -> dict[str, object]:
            down = kind == DeadManNotificationKind.DOWN.value
            return {
                "notification_id": index + 2,
                "kind": kind,
                "transition": {
                    "from_state": "up" if down else "down",
                    "to_state": "down" if down else "up",
                    "at": "2026-09-24T12:00:00+00:00",
                },
            }

        document: dict[str, object] = {
            "schema_version": 4,
            "state": "up",
            "consecutive_failures": 0,
            "consecutive_successes": 0,
            "state_changed_at": "2026-09-24T12:01:00+00:00",
            "pending_notifications": [
                intent(index, kind)
                for index, kind in enumerate(kinds)
            ],
            "next_notification_id": len(kinds) + 2,
            "current_down": None,
        }
        return json.dumps(document)

    def test_every_short_kind_sequence_classified_correctly(self):
        # Self-audit: all 2^0 + ... + 2^4 = 31 sequences.
        for length in range(0, 5):
            for bits in range(2**length):
                kinds = tuple(
                    DeadManNotificationKind.RECOVERED.value
                    if bits >> position & 1
                    else DeadManNotificationKind.DOWN.value
                    for position in range(length)
                )
                # RECOVERED? DOWN*: valid iff no RECOVERED appears
                # after position 0.
                valid = all(
                    kind == DeadManNotificationKind.DOWN.value
                    for kind in kinds[1:]
                )
                with self.subTest(kinds=kinds):
                    if valid:
                        status = self._status_from_kinds(kinds)
                        self.assertEqual(
                            [
                                n.kind.value
                                for n in status.pending_notifications
                            ],
                            list(kinds),
                        )
                        self.assertEqual(
                            decode_deadman_status(
                                encode_deadman_status(status)
                            ),
                            status,
                        )
                    else:
                        with self.assertRaises(ValueError):
                            self._status_from_kinds(kinds)

    def test_qa_invalid_sequences_rejected_by_constructor(self):
        for kinds in (
            ("down", "recovered"),
            ("down", "down", "recovered"),
            ("recovered", "recovered"),
            ("recovered", "down", "recovered"),
        ):
            with self.subTest(kinds=kinds):
                with self.assertRaises(ValueError):
                    self._status_from_kinds(kinds)

    def test_qa_invalid_sequences_rejected_by_decoder(self):
        for kinds in (
            ("down", "recovered"),
            ("down", "down", "recovered"),
            ("recovered", "recovered"),
            ("recovered", "down", "recovered"),
        ):
            with self.subTest(kinds=kinds):
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(self._pending_document(kinds))

    def test_valid_sequences_decode_with_exact_kinds(self):
        for kinds in (
            (),
            ("down",),
            ("down", "down"),
            ("down", "down", "down"),
            ("recovered",),
            ("recovered", "down"),
            ("recovered", "down", "down"),
        ):
            with self.subTest(kinds=kinds):
                decoded = decode_deadman_status(
                    self._pending_document(kinds)
                )
                self.assertEqual(
                    [n.kind.value for n in decoded.pending_notifications],
                    list(kinds),
                )

    def test_machine_produced_queues_match_grammar(self):
        # Real machine histories for the canonical valid shapes.
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        self.assertEqual(
            [n.kind for n in down_a.pending_notifications],
            [DeadManNotificationKind.DOWN],
        )
        unacked_up = _recovery(down_a, _T0 + 3 * _MINUTE)
        down_b = _outage(unacked_up, _T0 + 5 * _MINUTE)
        self.assertEqual(
            [n.kind for n in down_b.pending_notifications],
            [DeadManNotificationKind.DOWN] * 2,
        )
        acked = _ack(down_a, 1)
        recovered_up = _recovery(acked, _T0 + 3 * _MINUTE)
        self.assertEqual(
            [n.kind for n in recovered_up.pending_notifications],
            [DeadManNotificationKind.RECOVERED],
        )
        down_c = _outage(recovered_up, _T0 + 5 * _MINUTE)
        self.assertEqual(
            [n.kind for n in down_c.pending_notifications],
            [DeadManNotificationKind.RECOVERED,
             DeadManNotificationKind.DOWN],
        )


class ModelBasedReachabilityTest(unittest.TestCase):
    """Bounded model regression over the real machine.

    From the canonical initial status, interleavings of
    successful/failed probe evaluations and valid oldest-first
    acknowledgements are explored to a bounded depth (traversal
    identity is ``(encoded state, depth)`` because the explicit
    ``now`` of an expansion depends on the depth). Every explored
    node must validate under the strict constructor invariants and
    survive encode -> decode with exact operational equality, and
    in-traversal probes verify stale/wrong acknowledgement
    rejection. This is a bounded model regression test, NOT a
    proof of all histories.
    """

    MAX_DEPTH = 10
    MAX_NODES = 20000

    _CATEGORIES = (
        "initial_unknown",
        "unknown_to_up",
        "unknown_to_down",
        "unacked_down",
        "acked_down",
        "recovery_without_recovered",
        "recovery_with_recovered",
        "second_outage",
        "multiple_pending",
        "pending_recovered",
        "recovered_then_down",
        "multiple_pending_down",
        "stale_wrong_ack_rejected",
    )

    @staticmethod
    def _mark_categories(
        status: DeadManStatus,
        categories: dict[str, bool],
    ) -> None:
        pending = status.pending_notifications
        if status.state is DeadManState.UNKNOWN:
            categories["initial_unknown"] = True
        if (
            status.state is DeadManState.UP
            and status.next_notification_id == 1
            and not pending
        ):
            # The canonical fresh-establishment snapshot.
            categories["unknown_to_up"] = True
        if status.state is DeadManState.DOWN:
            current = status.current_down
            assert current is not None
            if current.acknowledged:
                categories["acked_down"] = True
            else:
                categories["unacked_down"] = True
            if current.notification_id == 1:
                # The canonical first-outage snapshot (startup DOWN
                # and established-then-down intentionally coincide).
                categories["unknown_to_down"] = True
            if current.notification_id >= 2:
                categories["second_outage"] = True
        if (
            status.state is DeadManState.UP
            and pending
        ):
            if (
                pending[-1].kind
                is DeadManNotificationKind.RECOVERED
            ):
                categories["recovery_with_recovered"] = True
            else:
                categories["recovery_without_recovered"] = True
        if len(pending) >= 2:
            categories["multiple_pending"] = True
            if all(
                n.kind is DeadManNotificationKind.DOWN for n in pending
            ):
                categories["multiple_pending_down"] = True
        if (
            pending
            and pending[0].kind is DeadManNotificationKind.RECOVERED
        ):
            categories["pending_recovered"] = True
            if len(pending) >= 2:
                categories["recovered_then_down"] = True

    def test_reachable_states_roundtrip_exactly(self) -> None:
        visited: set[tuple[str, int]] = set()
        distinct_states: set[str] = set()
        categories = {name: False for name in self._CATEGORIES}
        frontier: list[tuple[DeadManStatus, int]] = [
            (INITIAL_DEADMAN_STATUS, 0)
        ]
        while frontier:
            status, depth = frontier.pop()
            encoded = encode_deadman_status(status)
            node = (encoded, depth)
            if node in visited:
                continue
            visited.add(node)
            distinct_states.add(encoded)
            self.assertEqual(decode_deadman_status(encoded), status)
            self._mark_categories(status, categories)
            pending = status.pending_notifications
            if pending:
                head_id = pending[0].notification_id
                # Wrong/non-head id: never removes another intent.
                with self.assertRaises(ValueError):
                    _ack(status, head_id + 1)
                if head_id > 1:
                    # Stale id: allocated once and already acked
                    # (everything below the contiguous head was).
                    with self.assertRaises(ValueError):
                        _ack(status, head_id - 1)
                categories["stale_wrong_ack_rejected"] = True
            if depth >= self.MAX_DEPTH:
                continue
            moment = _T0 + (depth + 1) * _MINUTE
            successors = [
                _advance(status, DeadManProbeOutcome.HEALTHY, moment),
                _advance(status, DeadManProbeOutcome.FAILED, moment),
            ]
            if pending:
                # The only acknowledgement ever attempted is the
                # exact id of the OLDEST pending intent.
                successors.append(_ack(status, head_id))
            for successor in successors:
                frontier.append((successor, depth + 1))
        for name, reached in categories.items():
            self.assertTrue(
                reached, f"traversal never reached category {name!r}"
            )
        # Diagnostics only — the coverage proof is the explicit
        # category assertions above, not these counts.
        self.assertGreater(len(distinct_states), 40)
        self.assertLess(len(visited), self.MAX_NODES)


class TimeSemanticsTest(unittest.TestCase):
    """Explicit time: awareness, monotonicity, offset instants."""

    def test_advance_is_keyword_only(self) -> None:
        params = inspect.signature(advance_deadman_status).parameters
        self.assertEqual(set(params), {"status", "outcome", "now"})
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in params.values()
            )
        )

    def test_positional_call_rejected(self) -> None:
        with self.assertRaises(TypeError):
            advance_deadman_status(  # type: ignore[misc]
                INITIAL_DEADMAN_STATUS, DeadManProbeOutcome.HEALTHY, _T0
            )

    def test_naive_now_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _advance(
                INITIAL_DEADMAN_STATUS,
                DeadManProbeOutcome.HEALTHY,
                datetime(2026, 9, 24, 12, 0, 0),
            )

    def test_effectively_naive_now_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _advance(
                INITIAL_DEADMAN_STATUS,
                DeadManProbeOutcome.HEALTHY,
                datetime(2026, 9, 24, 12, 0, 0, tzinfo=_NoneOffsetZone()),
            )

    def test_non_datetime_now_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _advance(
                INITIAL_DEADMAN_STATUS,
                DeadManProbeOutcome.HEALTHY,
                "2026-09-24T12:00:00+00:00",  # type: ignore[arg-type]
            )

    def test_now_before_anchor_rejected(self) -> None:
        up = _up_status(at=_T0)
        with self.assertRaises(ValueError):
            _advance(
                up,
                DeadManProbeOutcome.FAILED,
                _T0 - timedelta(microseconds=1),
            )

    def test_now_equal_to_anchor_allowed(self) -> None:
        up = _up_status(at=_T0)
        advanced = _advance(up, DeadManProbeOutcome.FAILED, _T0)
        self.assertEqual(advanced.consecutive_failures, 1)

    def test_different_offsets_compare_by_instant(self) -> None:
        # The anchor recorded at +03:00 representing the same
        # instant as 12:00Z; a UTC now of that instant is not
        # "before" it.
        at_plus3 = _T0.astimezone(_PLUS3)  # 15:00+03:00 == 12:00Z
        up = _up_status(at=at_plus3)
        advanced = _advance(up, DeadManProbeOutcome.FAILED, _T0)
        self.assertEqual(advanced.consecutive_failures, 1)
        # One microsecond earlier (in a different offset) still
        # fails closed — instants, not wall texts, decide.
        with self.assertRaises(ValueError):
            _advance(
                up,
                DeadManProbeOutcome.FAILED,
                _T0 - timedelta(microseconds=1),
            )

    def test_anchor_and_intent_stamped_at_explicit_now(self) -> None:
        moment = _T0 + timedelta(minutes=17, seconds=3)
        advanced = _advance(
            INITIAL_DEADMAN_STATUS, DeadManProbeOutcome.HEALTHY, moment
        )
        self.assertEqual(advanced.state_changed_at, moment)
        # The outage enters DOWN on its THIRD failure tick.
        down_at = moment + 3 * _MINUTE
        down = _outage(advanced, moment + _MINUTE)
        self.assertEqual(down.state_changed_at, down_at)
        self.assertEqual(down.pending_notifications[0].at, down_at)

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            advance_deadman_status(
                status="unknown",  # type: ignore[arg-type]
                outcome=DeadManProbeOutcome.HEALTHY,
                now=_T0,
            )
        with self.assertRaises(ValueError):
            advance_deadman_status(
                status=INITIAL_DEADMAN_STATUS,
                outcome="healthy",  # type: ignore[arg-type]
                now=_T0,
            )


class EncodeDecodeTest(unittest.TestCase):
    """Strict versioned persistence boundary (schema v4)."""

    def _scenario_b_status(self) -> DeadManStatus:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        up = _recovery(down_a, _T0 + 3 * _MINUTE)
        return _outage(up, _T0 + 5 * _MINUTE)

    def _scenario_c_status(self) -> DeadManStatus:
        down_a = _outage(INITIAL_DEADMAN_STATUS, _T0)
        acknowledged = _ack(down_a, 1)
        up = _recovery(acknowledged, _T0 + 3 * _MINUTE)
        return _outage(up, _T0 + 5 * _MINUTE)

    def test_roundtrip_representative_statuses(self) -> None:
        statuses = [
            INITIAL_DEADMAN_STATUS,
            DeadManStatus(
                state=DeadManState.UNKNOWN, consecutive_failures=2
            ),
            _up_status(),
            DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                consecutive_failures=2,
            ),
            _down_status(acked=False),
            _down_status(acked=True),
            _run(
                _down_status(acked=True),
                (DeadManProbeOutcome.HEALTHY,) * 1,
            )[-1],
            _run(
                _down_status(acked=False),
                (DeadManProbeOutcome.HEALTHY,) * 2,
            )[-1],
            _run(
                _down_status(acked=True),
                (DeadManProbeOutcome.HEALTHY,) * 2,
            )[-1],
        ]
        for status in statuses:
            with self.subTest(status=status):
                self.assertEqual(
                    decode_deadman_status(encode_deadman_status(status)),
                    status,
                )

    def test_roundtrip_multiple_pending_intents(self) -> None:
        for status in (self._scenario_b_status(),
                       self._scenario_c_status()):
            with self.subTest(queue_len=len(status.pending_notifications)):
                decoded = decode_deadman_status(
                    encode_deadman_status(status)
                )
                self.assertEqual(decoded, status)
                self.assertEqual(
                    [n.notification_id for n in decoded.pending_notifications],
                    [n.notification_id
                     for n in status.pending_notifications],
                )
                self.assertEqual(
                    decoded.current_down,
                    status.current_down,
                )

    def test_encoding_is_deterministic(self) -> None:
        status = self._scenario_c_status()
        self.assertEqual(
            encode_deadman_status(status), encode_deadman_status(status)
        )

    def test_encoding_is_compact_sorted_json(self) -> None:
        text = encode_deadman_status(INITIAL_DEADMAN_STATUS)
        document = json.loads(text)
        self.assertIsInstance(document, dict)
        self.assertEqual(
            list(document),
            sorted(
                [
                    "schema_version",
                    "state",
                    "consecutive_failures",
                    "consecutive_successes",
                    "state_changed_at",
                    "pending_notifications",
                    "next_notification_id",
                    "current_down",
                ]
            ),
        )
        self.assertEqual(document["schema_version"], 4)
        self.assertEqual(document["state"], "unknown")
        self.assertIsNone(document["state_changed_at"])
        self.assertEqual(document["pending_notifications"], [])
        self.assertEqual(document["next_notification_id"], 1)
        self.assertIsNone(document["current_down"])

    def test_encoding_normalizes_timestamps_to_utc(self) -> None:
        at_plus3 = _T0.astimezone(_PLUS3)  # same instant as 12:00Z
        status = _up_status(at=at_plus3)
        text = encode_deadman_status(status)
        self.assertNotIn("+03:00", text)
        self.assertIn("2026-09-24T12:00:00+00:00", text)
        decoded = decode_deadman_status(text)
        self.assertEqual(decoded.state_changed_at, _T0)
        self.assertEqual(decoded.state_changed_at.utcoffset(), timedelta(0))

    def test_encode_rejects_non_status_input(self) -> None:
        with self.assertRaises(ValueError):
            encode_deadman_status("unknown")  # type: ignore[arg-type]

    def test_decode_rejects_non_string_payload(self) -> None:
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(b'{"schema_version": 4}')  # type: ignore[arg-type]

    def test_decode_rejects_invalid_json(self) -> None:
        for text in ("", "{", "not json", "null"):
            with self.subTest(text=text):
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(text)

    def test_decode_rejects_non_object_root(self) -> None:
        for text in ("[]", "42", '"up"'):
            with self.subTest(text=text):
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(text)

    def test_decode_rejects_duplicate_keys(self) -> None:
        text = (
            '{"schema_version": 4, "schema_version": 4,'
            ' "state": "unknown", "consecutive_failures": 0,'
            ' "consecutive_successes": 0, "state_changed_at": null,'
            ' "pending_notifications": [], "next_notification_id": 1,'
            ' "current_down": null}'
        )
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(text)

    def test_decode_rejects_non_finite_constants(self) -> None:
        text = (
            '{"schema_version": 4, "state": "unknown",'
            ' "consecutive_failures": NaN, "consecutive_successes": 0,'
            ' "state_changed_at": null, "pending_notifications": [],'
            ' "next_notification_id": 1, "current_down": null}'
        )
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(text)

    def test_decode_rejects_missing_and_unknown_fields(self) -> None:
        base = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        for key in list(base):
            with self.subTest(missing=key):
                broken = dict(base)
                del broken[key]
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(broken))
        with self.subTest(unknown="extra"):
            broken = dict(base)
            broken["extra"] = 1
            with self.assertRaises(DeadManStateDecodeError):
                decode_deadman_status(json.dumps(broken))

    def test_decode_rejects_unknown_schema_version(self) -> None:
        # Versions 1-3 were superseded candidate shapes; only
        # exactly 4 decodes.
        for version in (0, 1, 2, 3, 5, -1, "4", 4.0, True, None):
            with self.subTest(version=version):
                document = json.loads(
                    encode_deadman_status(INITIAL_DEADMAN_STATUS)
                )
                document["schema_version"] = version
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))

    def test_decode_rejects_unknown_state_value(self) -> None:
        document = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        document["state"] = "degraded"
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_bad_counters(self) -> None:
        for value in (True, 1.0, "1", -1):
            with self.subTest(value=value):
                document = json.loads(
                    encode_deadman_status(INITIAL_DEADMAN_STATUS)
                )
                document["consecutive_failures"] = value
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))

    def test_decode_rejects_threshold_reaching_counters(self) -> None:
        document = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        document["consecutive_failures"] = DOWN_CONFIRMATIONS
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_naive_anchor(self) -> None:
        document = json.loads(encode_deadman_status(_up_status()))
        document["state_changed_at"] = "2026-09-24T12:00:00"
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_malformed_anchor(self) -> None:
        document = json.loads(encode_deadman_status(_up_status()))
        document["state_changed_at"] = "not-a-datetime"
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_up_without_anchor(self) -> None:
        document = json.loads(encode_deadman_status(_up_status()))
        document["state_changed_at"] = None
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_unknown_with_anchor(self) -> None:
        document = json.loads(encode_deadman_status(_up_status()))
        document["state"] = "unknown"
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_bad_next_id(self) -> None:
        for bad in (True, 0, -1, 1.0, "2", None):
            with self.subTest(value=bad):
                document = json.loads(
                    encode_deadman_status(INITIAL_DEADMAN_STATUS)
                )
                document["next_notification_id"] = bad
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))

    def test_decode_rejects_pending_not_an_array(self) -> None:
        for bad in (None, {}, True, "[]", 0):
            with self.subTest(value=bad):
                document = json.loads(
                    encode_deadman_status(INITIAL_DEADMAN_STATUS)
                )
                document["pending_notifications"] = bad
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))

    def test_decode_rejects_bad_notification_ids(self) -> None:
        cases = [
            ("bool id", lambda n: n.update({"notification_id": True})),
            ("zero id", lambda n: n.update({"notification_id": 0})),
            ("float id", lambda n: n.update({"notification_id": 1.0})),
            ("string id", lambda n: n.update({"notification_id": "1"})),
            ("missing id", lambda n: n.pop("notification_id")),
            ("unknown field", lambda n: n.update({"extra": 1})),
        ]
        for label, mutate in cases:
            with self.subTest(case=label):
                document = json.loads(
                    encode_deadman_status(self._scenario_b_status())
                )
                assert document["pending_notifications"]
                mutate(document["pending_notifications"][0])
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))

    def test_decode_rejects_recovered_id_one(self) -> None:
        # RECOVERED with id 1 is impossible: the first allocation
        # of any real timeline is necessarily a DOWN intent.
        document = json.loads(
            encode_deadman_status(self._scenario_c_status())
        )
        document["pending_notifications"][0].update(
            {
                "notification_id": 1,
                "kind": "recovered",
                "transition": {
                    "from_state": "down",
                    "to_state": "up",
                    "at": "2026-09-24T12:03:00+00:00",
                },
            }
        )
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_bad_notification_shapes(self) -> None:
        down = json.loads(encode_deadman_status(_down_status(acked=False)))
        assert down["pending_notifications"]
        for mutation in (
            {"kind": "recovered"},  # kind disagrees with transition
            {"kind": 1},  # non-string kind
            {"extra": 1},  # unknown field
        ):
            with self.subTest(mutation=mutation):
                document = json.loads(json.dumps(down))
                assert document["pending_notifications"]
                document["pending_notifications"][0].update(mutation)
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))
        with self.subTest(missing="kind"):
            document = json.loads(json.dumps(down))
            assert document["pending_notifications"]
            del document["pending_notifications"][0]["kind"]
            with self.assertRaises(DeadManStateDecodeError):
                decode_deadman_status(json.dumps(document))

    def test_decode_rejects_naive_transition_datetime(self) -> None:
        down = json.loads(encode_deadman_status(_down_status(acked=False)))
        down["pending_notifications"][0]["transition"]["at"] = (
            "2026-09-24T12:00:00"
        )
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(down))

    def test_decode_rejects_non_canonical_transition(self) -> None:
        down = json.loads(encode_deadman_status(_down_status(acked=False)))
        down["pending_notifications"][0]["transition"].update(
            {"from_state": "up", "to_state": "up"}
        )
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(down))

    def test_decode_rejects_nested_unknown_fields(self) -> None:
        document = json.loads(encode_deadman_status(_up_status()))
        document["state_changed_at"] = None
        document["state"] = "unknown"
        document["consecutive_failures"] = 2
        # (UNKNOWN with an extra nested field in a hypothetical
        # transition object; pending is empty, so use a DOWN state.)
        down = json.loads(encode_deadman_status(_down_status()))
        down["pending_notifications"][0]["transition"]["extra"] = 1
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(down))

    def test_decode_rejects_current_down_bad_shape(self) -> None:
        down = json.loads(encode_deadman_status(_down_status()))
        for mutation in (
            {"extra": 1},
            {"acknowledged": "false"},
        ):
            with self.subTest(mutation=mutation):
                document = json.loads(json.dumps(down))
                document["current_down"].update(mutation)
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(json.dumps(document))
        with self.subTest(missing="acknowledged"):
            document = json.loads(json.dumps(down))
            del document["current_down"]["acknowledged"]
            with self.assertRaises(DeadManStateDecodeError):
                decode_deadman_status(json.dumps(document))
        with self.subTest(not_an_object=True):
            document = json.loads(json.dumps(down))
            document["current_down"] = 1
            with self.assertRaises(DeadManStateDecodeError):
                decode_deadman_status(json.dumps(document))

    def test_decode_rejects_binding_not_latest_allocation(self) -> None:
        down = json.loads(encode_deadman_status(_down_status()))
        document = json.loads(json.dumps(down))
        document["current_down"]["notification_id"] = 2
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_unacked_binding_missing_from_tail(self):
        down = json.loads(encode_deadman_status(_down_status()))
        document = json.loads(json.dumps(down))
        document["pending_notifications"] = []
        document["current_down"]["acknowledged"] = False
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_acked_binding_with_pending(self) -> None:
        down = json.loads(encode_deadman_status(_down_status()))
        document = json.loads(json.dumps(down))
        document["current_down"]["acknowledged"] = True
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_decode_rejects_pending_dated_after_anchor(self) -> None:
        document = json.loads(encode_deadman_status(_up_status()))
        document["pending_notifications"] = [
            {
                "notification_id": 1,
                "kind": "down",
                "transition": {
                    "from_state": "up",
                    "to_state": "down",
                    "at": "2026-09-24T12:05:00+00:00",
                },
            }
        ]
        document["next_notification_id"] = 2
        with self.assertRaises(DeadManStateDecodeError):
            decode_deadman_status(json.dumps(document))

    def test_malformed_state_never_becomes_up(self) -> None:
        # Every malformed family raises instead of yielding a
        # healthy baseline.
        malformed = [
            "",
            "null",
            "[]",
            '{"schema_version": 5}',
            '{"schema_version": 4, "state": "up"}',
            '{"schema_version": 4, "state": "unknown",'
            ' "consecutive_failures": 0}',
        ]
        for text in malformed:
            with self.subTest(text=text):
                with self.assertRaises(DeadManStateDecodeError):
                    decode_deadman_status(text)

    def test_decode_error_is_bounded_type(self) -> None:
        self.assertTrue(issubclass(DeadManStateDecodeError, Exception))
        try:
            decode_deadman_status("{")
        except DeadManStateDecodeError as error:
            self.assertNotIn("secret", str(error).lower())


class PurityBoundaryTest(unittest.TestCase):
    """Purity, statelessness and the H1A architectural boundary."""

    def setUp(self) -> None:
        self.source = inspect.getsource(deadman)

    def test_same_inputs_produce_same_output(self) -> None:
        kwargs = dict(
            status=DeadManStatus(
                state=DeadManState.UP,
                state_changed_at=_T0,
                consecutive_failures=1,
            ),
            outcome=DeadManProbeOutcome.FAILED,
            now=_T0,
        )
        self.assertEqual(
            advance_deadman_status(**kwargs),
            advance_deadman_status(**kwargs),
        )

    def test_inputs_not_mutated(self) -> None:
        status = DeadManStatus(
            state=DeadManState.UP,
            state_changed_at=_T0,
            consecutive_failures=2,
        )
        snapshot = DeadManStatus(
            state=DeadManState.UP,
            state_changed_at=_T0,
            consecutive_failures=2,
        )
        _advance(status, DeadManProbeOutcome.FAILED)
        self.assertEqual(status, snapshot)

    def test_no_hidden_cross_call_memory(self) -> None:
        outcomes = (DeadManProbeOutcome.FAILED,) * 3
        first = _run(INITIAL_DEADMAN_STATUS, outcomes)
        second = _run(INITIAL_DEADMAN_STATUS, outcomes)
        self.assertEqual(first, second)

    def test_no_transport_dependency(self) -> None:
        for token in (
            "socket",
            "urllib",
            "urlopen",
            "requests",
            "httpx",
            "curl",
            "asyncio",
            "telegram",
        ):
            self.assertNotIn(token, self.source)

    def test_no_file_io_dependency(self) -> None:
        for token in (
            "open(",
            "Path(",
            "pathlib",
            "os.remove",
            "rename",
            "fsync",
        ):
            self.assertNotIn(token, self.source)

    def test_no_wall_clock_or_sleeping(self) -> None:
        for token in (
            "utcnow",
            "time.time",
            "time.monotonic",
            "monotonic_ns",
            "perf_counter",
            "sleep(",
            "date.today",
        ):
            self.assertNotIn(token, self.source)

    def test_no_environment_or_logging(self) -> None:
        for token in (
            "os.environ",
            "getenv",
            "import logging",
            "logger",
            "print(",
        ):
            self.assertNotIn(token, self.source)

    def test_no_persistence_dependency(self) -> None:
        for token in (
            "sqlite",
            "HeartbeatRepository",
            "hermes_sentinel.persistence",
        ):
            self.assertNotIn(token, self.source)

    def test_no_deployment_facts_in_module_source(self) -> None:
        # Public-distribution boundary: the core module carries no
        # URLs, IPv4 literals or hostnames of any kind — real probe
        # targets are operator deployment configuration, never
        # tracked repository facts.
        self.assertNotIn("http://", self.source)
        self.assertNotIn("https://", self.source)
        self.assertIsNone(
            re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", self.source)
        )

    def test_no_host_model_coupling(self) -> None:
        # The dead-man core is fully standalone: it imports NOTHING
        # from the rest of hermes_sentinel, so the host
        # HEALTHY/DEGRADED/DOWN model, its transitions and its
        # incidents are structurally unreachable from here.
        imported = {
            line.strip().split()[1]
            for line in self.source.splitlines()
            if line.strip().startswith("from hermes_sentinel")
        }
        self.assertEqual(imported, set())

    def test_no_mutable_module_state(self) -> None:
        allowed_public = {
            "DOWN_CONFIRMATIONS",
            "RECOVERY_CONFIRMATIONS",
            "INITIAL_DEADMAN_STATUS",
            "DeadManState",
            "DeadManProbeOutcome",
            "DeadManProbeResponse",
            "classify_deadman_probe",
            "DeadManTransition",
            "DeadManNotificationKind",
            "DeadManNotification",
            "DeadManCurrentDown",
            "DeadManStatus",
            "advance_deadman_status",
            "acknowledge_deadman_notification",
            "encode_deadman_status",
            "decode_deadman_status",
            "DeadManStateDecodeError",
            "annotations",  # from __future__ import annotations
            "json",  # stdlib encode/decode only, no I/O
            "dataclass",
            "replace",
            "datetime",
            "timezone",
            "Enum",
            "NoReturn",
        }
        for name, value in vars(deadman).items():
            # Private helpers and dunder module bookkeeping are
            # irrelevant here: only the public module surface may
            # exist, and none of it may be a mutable container.
            if name.startswith("_"):
                continue
            self.assertIn(name, allowed_public)
            if isinstance(value, type):
                continue
            self.assertNotIsInstance(value, (dict, list, set))

    def test_module_exports_match_all(self) -> None:
        self.assertEqual(
            set(deadman.__all__),
            {
                "DOWN_CONFIRMATIONS",
                "RECOVERY_CONFIRMATIONS",
                "INITIAL_DEADMAN_STATUS",
                "DeadManState",
                "DeadManProbeOutcome",
                "DeadManProbeResponse",
                "classify_deadman_probe",
                "DeadManTransition",
                "DeadManNotificationKind",
                "DeadManNotification",
                "DeadManCurrentDown",
                "DeadManStatus",
                "advance_deadman_status",
                "acknowledge_deadman_notification",
                "encode_deadman_status",
                "decode_deadman_status",
                "DeadManStateDecodeError",
            },
        )


if __name__ == "__main__":
    unittest.main()
