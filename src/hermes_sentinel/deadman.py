"""External dead-man core (Stage H1A).

Deterministic, transport-independent core for Stage H external
dead-man monitoring of the central Sentinel itself. The Sentinel
failure domain gets its OWN liveness vocabulary — ``UNKNOWN`` /
``UP`` / ``DOWN`` (:class:`DeadManState`) — which is deliberately
NOT the host ``HEALTHY`` / ``DEGRADED`` / ``DOWN`` model of Stage D:
a monitored host is an external object, while this state describes
the Sentinel heartbeat endpoint that ordinary alerting depends on.

H1A is CORE ONLY and completely deployment-agnostic: the frozen H0
design decisions (an unauthenticated liveness GET against the public
heartbeat endpoint, a ~60 s cadence, an independent direct Telegram
sender with its own Stage-H credential) are context for later
stages — concrete probe targets are operator deployment
configuration, never tracked repository facts. This module performs
no HTTP request, no Telegram transport, no environment loading, no
CLI/process entrypoint, no systemd packaging, no filesystem I/O
and no scheduling.

Contract points (see docs/ARCHITECTURE.md section 31):

- probe classification is a pure boundary over an ALREADY-OBSERVED
  HTTP result (:class:`DeadManProbeResponse`). The healthy signature
  is exactly the canonical dead-man response: status ``405``, an
  ``Allow`` header whose method list contains ``POST`` (methods are
  case-sensitive) and an empty body. Anything else — any 2xx/3xx,
  404, 502, a wrong or missing ``Allow``, a non-empty body — is a
  probe failure. Network/DNS/TLS exceptions belong to the H1B
  transport and are later mapped to failed outcomes before this
  boundary;
- debounce (frozen H0): 3 consecutive failed probes confirm DOWN,
  2 consecutive successful probes confirm recovery;
- ``UNKNOWN`` establishes ``UP`` silently on the first success (no
  startup notification); failures accumulate in ``UNKNOWN`` exactly
  like in ``UP`` and confirm DOWN at the third;
- notification INTENT is modelled, never delivered. The status
  carries an ORDERED immutable tuple of pending intents — never a
  replaceable single slot: every intent the machine legitimately
  creates stays durable until explicitly acknowledged, later
  transitions only APPEND (never replace, drop or reorder an older
  unacknowledged intent), and repeated ticks for the same
  transition never duplicate an intent. Entering DOWN appends
  exactly one DOWN intent and binds the CURRENT outage to that
  exact id explicitly (:class:`DeadManCurrentDown`), a binding that
  survives encode/decode restarts: acknowledging that exact id
  marks the outage acknowledged (a fact queue removal cannot
  lose), and a RECOVERED intent is appended on ``DOWN -> UP`` only
  when the bound outage is marked acknowledged — never inferred
  from timestamps or from queue presence alone — so a failed future
  Telegram delivery followed by recovery never emits a misleading
  RECOVERED for an outage the operator was never told about; the
  unacknowledged DOWN intent simply stays pending across its own
  recovery and all later outages;
- notification identity is deterministic and persisted: every
  intent carries a monotonically increasing integer
  ``notification_id`` allocated exactly once at creation and never
  reused (the next id lives in the persisted
  ``next_notification_id``). Ids are unique within the persisted
  dead-man state history and independent of timestamps — no
  random UUIDs, no wall-clock-derived values;
- acknowledgement is BY ID and strictly oldest-first:
  :func:`acknowledge_deadman_notification` succeeds only for the
  exact id of the OLDEST pending intent and never removes a
  different intent; stale (already acknowledged), unknown and
  non-head ids fail closed leaving the queue unchanged. The future
  runtime can safely do transition detected -> persist state ->
  attempt oldest-first delivery -> acknowledge only on success;
- time is explicit: no wall-clock reads, every evaluation carries
  its own timezone-aware ``now``, a ``now`` before the current
  state's ``state_changed_at`` anchor fails closed, and encoding
  canonicalizes timestamps to UTC ISO 8601 text;
- persistence is a CANONICAL OPERATIONAL SNAPSHOT: the status
  stores exactly the facts future dead-man behavior needs (the
  debounce state, the state-change time anchor, the durable pending
  queue, the id allocator and the current-outage binding) and
  deliberately NOT the historical provenance of how the current
  state was reached — two histories that produce identical future
  behavior serialize to the identical canonical state. Strict
  fail-closed decoding (:func:`encode_deadman_status` /
  :func:`decode_deadman_status`) proves the exact schema, types,
  enums, timestamps, counters, ids, queue ordering and grammar,
  notification kind/transition pairing, the current-DOWN binding
  and internal future-behavior safety — it never attempts to
  reconstruct unstored history. No secrets exist in this state.
  File I/O and atomic writes belong to the H1B runtime.

The intended future oneshot flow this state serves:
load state file -> decode -> probe -> classify -> advance -> encode
+ persist -> (while intents are pending) attempt oldest-first
delivery -> acknowledge the delivered id -> persist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import NoReturn

__all__ = [
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
]


#: Frozen H0 debounce: consecutive failed probes that confirm DOWN.
DOWN_CONFIRMATIONS = 3

#: Frozen H0 debounce: consecutive successful probes that end DOWN.
RECOVERY_CONFIRMATIONS = 2

#: Canonical healthy dead-man probe signature: the exact HTTP status
#: the heartbeat endpoint answers a wrong-method request with.
_HEALTHY_STATUS = 405

#: The one method token an ``Allow`` header must list for the probe
#: response to be healthy (HTTP methods are case-sensitive).
_REQUIRED_ALLOWED_METHOD = "POST"

#: Persisted state schema version understood by this module. Version
#: 4 is the canonical operational snapshot (``state_changed_at`` +
#: the ``current_down`` binding object; no historical transition
#: provenance). Earlier unreleased candidate shapes are rejected as
#: unsupported.
_SCHEMA_VERSION = 4

#: Exactly the top-level fields of the persisted dead-man state.
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "state",
        "consecutive_failures",
        "consecutive_successes",
        "state_changed_at",
        "pending_notifications",
        "next_notification_id",
        "current_down",
    }
)

#: Exactly the fields of a serialized dead-man transition.
_TRANSITION_FIELDS = frozenset({"from_state", "to_state", "at"})

#: Exactly the fields of a serialized dead-man notification intent.
_NOTIFICATION_FIELDS = frozenset(
    {"notification_id", "kind", "transition"}
)

#: Exactly the fields of a serialized current-outage binding.
_CURRENT_DOWN_FIELDS = frozenset({"notification_id", "acknowledged"})


def _require_aware(name: str, value: datetime) -> None:
    """True datetime awareness per authoritative Python semantics.

    A datetime is timezone-aware only if BOTH hold: ``tzinfo is not
    None`` AND ``utcoffset() is not None`` (the same rule as the
    Stage B/D boundaries).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware (tzinfo is not None and"
            f" utcoffset() is not None), got {value!r}"
        )


def _require_counter(name: str, value: int) -> None:
    """A debounce counter is a true integer (never bool) >= 0."""
    # bool is an int subclass but is never an acceptable counter
    # value: a counter is an integer count, not a boolean flag.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an integer, got {type(value).__name__}"
        )
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


def _require_notification_id(name: str, value: int) -> None:
    """A notification id is a true integer (never bool) >= 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an integer, got {type(value).__name__}"
        )
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value!r}")


class DeadManState(Enum):
    """Liveness state of the Sentinel failure domain itself.

    Deliberately not the Stage D host state: there is no DEGRADED —
    a dead-man endpoint is either observably answering its canonical
    healthy signature or it is not.
    """

    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class DeadManProbeOutcome(Enum):
    """The classified outcome of one dead-man probe observation."""

    HEALTHY = "healthy"
    FAILED = "failed"


class DeadManNotificationKind(Enum):
    """The two dead-man notification intent kinds."""

    DOWN = "down"
    RECOVERED = "recovered"


#: The transition families a notification intent can carry: the two
#: DOWN-entry families and the recovery family. (The status itself
#: no longer stores transitions — only pending intents do.)
_CANONICAL_TRANSITIONS = frozenset(
    {
        (DeadManState.UNKNOWN, DeadManState.DOWN),
        (DeadManState.UP, DeadManState.DOWN),
        (DeadManState.DOWN, DeadManState.UP),
    }
)


@dataclass(frozen=True, slots=True)
class DeadManProbeResponse:
    """An already-observed HTTP result of one dead-man probe.

    Transport-independent input to classification: the H1B transport
    (future) performs the actual request and hands the observed
    status, every ``Allow`` header value verbatim in arrival order
    (multiplicity preserved), and the raw body bytes here. This
    module never performs a request itself.

    Invariants (structural fail-closed):

    - ``status`` is a true integer (never bool) in the HTTP status
      range [100, 599];
    - ``allow_headers`` is a tuple of ``str`` values, kept verbatim
      (never normalized);
    - ``body`` is ``bytes``.

    ``repr`` deliberately never reflects header values or body
    content (both are network-controlled): only the status, the
      Allow-value count and the body length appear — the Stage B4
    ``HttpRequest`` repr precedent.
    """

    status: int
    allow_headers: tuple[str, ...]
    body: bytes

    def __post_init__(self) -> None:
        if (
            isinstance(self.status, bool)
            or not isinstance(self.status, int)
        ):
            raise ValueError(
                "status must be an integer, got"
                f" {type(self.status).__name__}"
            )
        if not 100 <= self.status <= 599:
            raise ValueError(
                f"status must be within [100, 599], got {self.status!r}"
            )
        if not isinstance(self.allow_headers, tuple):
            raise ValueError(
                "allow_headers must be a tuple of strings, got"
                f" {type(self.allow_headers).__name__}"
            )
        for value in self.allow_headers:
            if not isinstance(value, str):
                raise ValueError(
                    "each allow header value must be a string, got"
                    f" {type(value).__name__}"
                )
        if not isinstance(self.body, bytes):
            raise ValueError(
                "body must be bytes, got" f" {type(self.body).__name__}"
            )

    def __repr__(self) -> str:
        # Secret/reflection safety: network-controlled Allow values
        # and body content are never reflected — counts and lengths
        # only (the B4 HttpRequest repr precedent).
        return (
            f"{type(self).__name__}(status={self.status!r},"
            f" allow_headers={len(self.allow_headers)} values,"
            f" body_length={len(self.body)})"
        )


def _post_is_allowed(allow_headers: tuple[str, ...]) -> bool:
    """Does the combined ``Allow`` method list contain ``POST``?

    Deterministic RFC-style list semantics: every Allow value is
    split on commas, tokens are stripped of surrounding whitespace
    and empty tokens are ignored, and membership is compared
    case-sensitively (HTTP methods are case-sensitive, as in the
    Stage B4 contract). Missing or empty Allow values simply
    contribute no methods.
    """
    methods: set[str] = set()
    for value in allow_headers:
        for token in value.split(","):
            method = token.strip(" \t")
            if method:
                methods.add(method)
    return _REQUIRED_ALLOWED_METHOD in methods


def classify_deadman_probe(
    response: DeadManProbeResponse,
) -> DeadManProbeOutcome:
    """Classify an already-observed dead-man probe response.

    Healthy ONLY when the canonical dead-man signature matches
    exactly:

    - HTTP status is exactly ``405``;
    - an ``Allow`` header lists ``POST`` among its methods;
    - the response body is empty (``b""`` — whitespace-only is NOT
      empty and fails).

    Everything else is a probe failure: any 2xx/3xx, 404, 502, any
    other generic 4xx/5xx, a missing/wrong ``Allow``, a lowercase
    ``post``, or a non-empty body — an answering HTTP server is
    never healthy evidence by itself. The function is pure and
    total over valid :class:`DeadManProbeResponse` values; it
    performs no I/O and no request.
    """
    if not isinstance(response, DeadManProbeResponse):
        raise ValueError(
            "response must be a DeadManProbeResponse, got"
            f" {type(response).__name__}"
        )
    if (
        response.status == _HEALTHY_STATUS
        and _post_is_allowed(response.allow_headers)
        and response.body == b""
    ):
        return DeadManProbeOutcome.HEALTHY
    return DeadManProbeOutcome.FAILED


@dataclass(frozen=True, slots=True)
class DeadManTransition:
    """A dead-man state change carried by a notification intent.

    ``at`` is the moment the transition was confirmed by the
    evaluation that produced it (the explicit ``now`` of that call),
    not the moment it was first suspected. Only the three
    intent-observable families exist: ``UNKNOWN -> DOWN``,
    ``UP -> DOWN`` and ``DOWN -> UP`` — the status itself persists
    no transition provenance.
    """

    from_state: DeadManState
    to_state: DeadManState
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.from_state, DeadManState):
            raise ValueError(
                "from_state must be a DeadManState, got"
                f" {type(self.from_state).__name__}"
            )
        if not isinstance(self.to_state, DeadManState):
            raise ValueError(
                "to_state must be a DeadManState, got"
                f" {type(self.to_state).__name__}"
            )
        if (self.from_state, self.to_state) not in _CANONICAL_TRANSITIONS:
            raise ValueError(
                "not a canonical dead-man transition:"
                f" {self.from_state.value} -> {self.to_state.value}"
            )
        _require_aware("at", self.at)

    @property
    def is_down_event(self) -> bool:
        """True when the dead-man target transitioned into DOWN."""
        return self.to_state is DeadManState.DOWN

    @property
    def is_recovery_event(self) -> bool:
        """True when the dead-man target left DOWN (DOWN -> UP)."""
        return (
            self.from_state is DeadManState.DOWN
            and self.to_state is DeadManState.UP
        )


@dataclass(frozen=True, slots=True)
class DeadManNotification:
    """A notification INTENT (never a delivery) with durable identity.

    ``notification_id`` is the deterministic persisted identity of
    the intent: a monotonically increasing integer allocated exactly
    once by the state machine when the intent is created, unique
    within the persisted dead-man state history and independent of
    timestamps (acknowledgement targets this id — value equality of
    the payload alone is NOT identity, because two distinct
    transitions may legally carry the same explicit datetime).

    The wrapped :class:`DeadManTransition` stays the canonical
    source of ``at`` / ``from_state`` / ``to_state`` (the future
    delivery renders it). A RECOVERED intent can never carry id 1:
    the first allocation of any real timeline is necessarily the
    first outage's DOWN intent.
    """

    notification_id: int
    kind: DeadManNotificationKind
    transition: DeadManTransition

    def __post_init__(self) -> None:
        _require_notification_id(
            "notification_id", self.notification_id
        )
        if not isinstance(self.kind, DeadManNotificationKind):
            raise ValueError(
                "kind must be a DeadManNotificationKind, got"
                f" {type(self.kind).__name__}"
            )
        if not isinstance(self.transition, DeadManTransition):
            raise ValueError(
                "transition must be a DeadManTransition, got"
                f" {type(self.transition).__name__}"
            )
        if self.kind is DeadManNotificationKind.DOWN:
            if not self.transition.is_down_event:
                raise ValueError(
                    "DeadManNotificationKind.DOWN requires a transition"
                    " into DOWN"
                )
        elif not self.transition.is_recovery_event:
            raise ValueError(
                "DeadManNotificationKind.RECOVERED requires a transition"
                " out of DOWN"
            )
        if (
            self.kind is DeadManNotificationKind.RECOVERED
            and self.notification_id == 1
        ):
            raise ValueError(
                "a RECOVERED notification can never have id 1: the"
                " first allocation of any real timeline is necessarily"
                " a DOWN intent"
            )

    @property
    def at(self) -> datetime:
        """The confirmation moment (projected from the transition)."""
        return self.transition.at

    @property
    def from_state(self) -> DeadManState:
        """The state left (projected from the transition)."""
        return self.transition.from_state

    @property
    def to_state(self) -> DeadManState:
        """The state entered (projected from the transition)."""
        return self.transition.to_state


@dataclass(frozen=True, slots=True)
class DeadManCurrentDown:
    """The current outage's explicit binding to its DOWN intent.

    The durable relationship current DOWN outage -> its exact
    notification identity -> whether THAT notification was
    acknowledged. The binding's transition moment is not duplicated
    here: it IS the status ``state_changed_at`` (the outage began
    when the state changed to DOWN). Exists only while the state is
    DOWN; recovery always clears it.
    """

    notification_id: int
    acknowledged: bool

    def __post_init__(self) -> None:
        _require_notification_id(
            "notification_id", self.notification_id
        )
        if not isinstance(self.acknowledged, bool):
            raise ValueError(
                "acknowledged must be a bool, got"
                f" {type(self.acknowledged).__name__}"
            )


@dataclass(frozen=True, slots=True)
class DeadManStatus:
    """The canonical operational snapshot of the dead-man core.

    This is BOTH the current liveness classification AND the
    complete future-relevant memory a oneshot process persists
    between ticks: the debounce streaks, the current state's time
    anchor, the ordered durable pending queue, the notification-id
    allocator and — while DOWN — the current outage's binding to
    its exact DOWN intent. Historical provenance of how the current
    state was reached is deliberately NOT stored: two histories
    with identical future behavior serialize identically.

    Invariants (fail-closed on direct construction and decoding —
    only internally safe, behavior-relevant facts are proven, never
    unstored history):

    - ``consecutive_failures`` / ``consecutive_successes`` are true
      integers (never bool) and are ``>= 0``;
    - debounce canonicalization: in DOWN the failure streak is 0 and
      the pending success streak stays below
      ``RECOVERY_CONFIRMATIONS``; outside DOWN the success streak is
      0 and the pending failure streak stays below
      ``DOWN_CONFIRMATIONS`` (a streak at/above its threshold is
      never a valid resting state — it must have transitioned);
    - ``state_changed_at`` (the current state's time anchor) is
      ``None`` exactly in the canonical initial UNKNOWN, and a truly
      timezone-aware datetime otherwise;
    - UNKNOWN is only the canonical initial operational state: no
      time anchor, no pending notification, no current-outage
      binding and the untouched initial notification sequence
      (``next_notification_id == 1``);
    - UP carries no current-outage binding; durable pending
      notifications from older unacknowledged outages may exist;
    - ``pending_notifications`` is a tuple (immutable, ordered
      oldest-first) of valid intents whose ids are strictly
      increasing; when non-empty it is a CONTIGUOUS suffix of the
      allocated ids anchored at ``next_notification_id - 1`` (ids
      are allocated 1, 2, 3, ... without gaps and acknowledgement
      is strictly oldest-first), its kinds follow the grammar
      ``RECOVERED? DOWN*`` (a RECOVERED is allocated only from a
      fully drained queue, so at most one can be pending and it is
      always the oldest item), and no pending transition is dated
      after ``state_changed_at`` (ledger time sanity);
    - ``next_notification_id`` is a true integer ``>= 1`` — the id
      the NEXT created intent will receive (ids are allocated once
      and never reused, so it only grows);
    - DOWN carries exactly one ``current_down`` binding whose id is
      exactly ``next_notification_id - 1`` (the id this outage's
      entry allocated — nothing allocates while DOWN): while
      unacknowledged, that exact DOWN intent is still pending as
      the TAIL with its transition dated exactly at
      ``state_changed_at``; once acknowledged, nothing may remain
      pending (oldest-first acknowledgement drained everything
      first). The acknowledgement fact survives the queue removal
      and encode/decode, so recovery decides correctly after a
      process restart.
    """

    state: DeadManState
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    state_changed_at: datetime | None = None
    pending_notifications: tuple[DeadManNotification, ...] = ()
    next_notification_id: int = 1
    current_down: DeadManCurrentDown | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, DeadManState):
            raise ValueError(
                "state must be a DeadManState, got"
                f" {type(self.state).__name__}"
            )
        _require_counter("consecutive_failures", self.consecutive_failures)
        _require_counter(
            "consecutive_successes", self.consecutive_successes
        )
        if self.state is DeadManState.DOWN:
            if self.consecutive_failures != 0:
                raise ValueError(
                    "consecutive_failures must be 0 when state is DOWN,"
                    f" got {self.consecutive_failures!r}"
                )
            if self.consecutive_successes >= RECOVERY_CONFIRMATIONS:
                raise ValueError(
                    "consecutive_successes must be below"
                    f" {RECOVERY_CONFIRMATIONS} when state is DOWN, got"
                    f" {self.consecutive_successes!r}"
                )
        else:
            if self.consecutive_successes != 0:
                raise ValueError(
                    "consecutive_successes must be 0 when state is not"
                    f" DOWN, got {self.consecutive_successes!r}"
                )
            if self.consecutive_failures >= DOWN_CONFIRMATIONS:
                raise ValueError(
                    "consecutive_failures must be below"
                    f" {DOWN_CONFIRMATIONS} when state is not DOWN, got"
                    f" {self.consecutive_failures!r}"
                )
        if not isinstance(self.pending_notifications, tuple):
            raise ValueError(
                "pending_notifications must be a tuple (immutable),"
                " got"
                f" {type(self.pending_notifications).__name__}"
            )
        previous_id: int | None = None
        for notification in self.pending_notifications:
            if not isinstance(notification, DeadManNotification):
                raise ValueError(
                    "each pending notification must be a"
                    " DeadManNotification, got"
                    f" {type(notification).__name__}"
                )
            if previous_id is not None:
                if notification.notification_id <= previous_id:
                    raise ValueError(
                        "pending notification ids must be strictly"
                        " increasing (oldest-first, no duplicates),"
                        f" got {notification.notification_id!r} after"
                        f" {previous_id!r}"
                    )
            previous_id = notification.notification_id
        _require_notification_id(
            "next_notification_id", self.next_notification_id
        )
        if self.pending_notifications:
            # Notification-ledger continuity: ids are allocated
            # 1, 2, 3, ... without gaps (next_notification_id is the
            # next never-allocated id) and acknowledgement is
            # strictly oldest-first, so a NON-EMPTY pending queue is
            # always a CONTIGUOUS suffix of the allocated ids,
            # anchored at the most recent allocation
            # (next_notification_id - 1). Gaps such as [3, 5] or
            # [2, 4, 5], and tails short of the anchor such as
            # [3, 4] with next id 6, are unreachable.
            tail_id = self.pending_notifications[-1].notification_id
            if tail_id != self.next_notification_id - 1:
                raise ValueError(
                    "the newest pending notification id"
                    f" {tail_id!r} must equal the most recent"
                    " allocation (next_notification_id - 1 =="
                    f" {self.next_notification_id - 1!r}): a"
                    " non-empty pending queue is a contiguous suffix"
                    " of the allocated ids"
                )
            expected_head = tail_id - len(self.pending_notifications) + 1
            head_id = self.pending_notifications[0].notification_id
            if head_id != expected_head:
                raise ValueError(
                    f"pending notification ids must be contiguous"
                    f" (no holes in the allocated-id suffix): got"
                    f" head {head_id!r} where {expected_head!r} is"
                    " required for tail"
                    f" {tail_id!r} and length"
                    f" {len(self.pending_notifications)}"
                )
        # Pending-ledger KIND grammar: a RECOVERED intent is
        # allocated only when its outage's DOWN intent was already
        # acknowledged — and because that DOWN was the NEWEST
        # notification of its outage entry, oldest-first
        # acknowledgement can reach it only after the ENTIRE queue
        # was drained. So immediately before any RECOVERED is
        # allocated the queue is empty, at most ONE RECOVERED can
        # ever be pending (a second would require acknowledging a
        # newer DOWN while the first is still pending), and a
        # pending RECOVERED is always the OLDEST item. The reachable
        # pending kinds are exactly ``RECOVERED? DOWN*``.
        for index, notification in enumerate(self.pending_notifications):
            if (
                index > 0
                and notification.kind
                is DeadManNotificationKind.RECOVERED
            ):
                raise ValueError(
                    "a RECOVERED notification can only be the oldest"
                    " pending intent (the reachable pending kinds are"
                    " RECOVERED? DOWN*): a RECOVERED at position"
                    f" {index!r} requires its outage's DOWN to have"
                    " been acknowledged while an older intent is"
                    " still pending — impossible under oldest-first"
                    " acknowledgement"
                )
        anchor = self.state_changed_at
        if anchor is not None:
            if not isinstance(anchor, datetime):
                raise ValueError(
                    "state_changed_at must be a datetime or None, got"
                    f" {type(anchor).__name__}"
                )
            _require_aware("state_changed_at", anchor)
            # Ledger time sanity: every pending intent was created at
            # or before the current state began; a future-dated
            # pending intent would corrupt the monotone id/time
            # ordering of later allocations.
            for notification in self.pending_notifications:
                if notification.transition.at > anchor:
                    raise ValueError(
                        "pending notification id"
                        f" {notification.notification_id!r} is dated"
                        " after state_changed_at"
                        f" {anchor!r}: internally inconsistent"
                        " ledger time"
                    )
        elif self.state is not DeadManState.UNKNOWN:
            raise ValueError(
                f"state {self.state.value!r} requires its"
                " state_changed_at time anchor"
            )
        if self.state is DeadManState.UNKNOWN:
            if anchor is not None:
                raise ValueError(
                    "UNKNOWN is the canonical initial state and"
                    " carries no state_changed_at anchor"
                )
            if self.pending_notifications != ():
                raise ValueError(
                    "UNKNOWN can never carry a pending notification:"
                    " no intent exists before the first transition"
                )
            if self.next_notification_id != 1:
                raise ValueError(
                    "UNKNOWN is only the initial pre-baseline state:"
                    " next_notification_id must be exactly 1, got"
                    f" {self.next_notification_id!r}"
                )
        current = self.current_down
        if current is not None and not isinstance(
            current, DeadManCurrentDown
        ):
            raise ValueError(
                "current_down must be a DeadManCurrentDown, got"
                f" {type(current).__name__}"
            )
        if self.state is not DeadManState.DOWN:
            if current is not None:
                raise ValueError(
                    "current_down must be None when state is not DOWN"
                )
            return
        if current is None:
            raise ValueError(
                "DOWN requires the current outage's binding"
                " (current_down)"
            )
        if current.notification_id != self.next_notification_id - 1:
            raise ValueError(
                "the current outage's DOWN notification id"
                f" {current.notification_id!r} must be exactly the id"
                " the outage entry allocated (next_notification_id - 1"
                f" == {self.next_notification_id - 1!r})"
            )
        if current.acknowledged:
            if self.pending_notifications:
                # Oldest-first acknowledgement: acking the current
                # outage's DOWN intent required every older intent
                # to be acknowledged first, and nothing new is
                # allocated while DOWN — so an acknowledged binding
                # implies an empty pending queue.
                raise ValueError(
                    "an acknowledged current-outage DOWN notification"
                    " implies every older intent was acknowledged first"
                    " (oldest-first acknowledgement): no pending"
                    " notification may remain"
                )
            return
        if not self.pending_notifications:
            raise ValueError(
                "the unacknowledged current-outage DOWN notification"
                " must still be pending"
            )
        tail = self.pending_notifications[-1]
        if tail.notification_id != current.notification_id:
            raise ValueError(
                "the unacknowledged current-outage DOWN notification"
                " must be the pending tail"
            )
        if tail.kind is not DeadManNotificationKind.DOWN:
            raise ValueError(
                "the current-outage bound notification must be of"
                " kind DOWN"
            )
        if anchor is None or tail.transition.at != anchor:
            raise ValueError(
                "the current-outage bound DOWN notification must be"
                " dated exactly at state_changed_at (the outage entry"
                " moment)"
            )


#: The deterministic entry point of the state machine: never probed,
#: no state change, no intent ever created (next id 1), both
#: streaks 0, no time anchor.
INITIAL_DEADMAN_STATUS = DeadManStatus(state=DeadManState.UNKNOWN)


def _advance_success(
    status: DeadManStatus, now: datetime
) -> DeadManStatus:
    """Apply one healthy probe outcome (state machine internals)."""
    if status.state is DeadManState.UNKNOWN:
        # First evidence of liveness: establish the UP baseline
        # immediately and silently — no startup notification.
        return replace(
            status,
            state=DeadManState.UP,
            consecutive_failures=0,
            consecutive_successes=0,
            state_changed_at=now,
        )
    if status.state is DeadManState.UP:
        # Healthy and known healthy: stay UP, reset the failure
        # streak (the debounce is broken by any single success).
        return replace(
            status,
            consecutive_failures=0,
            consecutive_successes=0,
        )
    # DOWN: one more consecutive recovery success.
    successes = status.consecutive_successes + 1
    if successes < RECOVERY_CONFIRMATIONS:
        return replace(
            status,
            consecutive_failures=0,
            consecutive_successes=successes,
        )
    # Exact threshold reached: leave DOWN on THIS evaluation. The
    # recovery decision uses ONLY the explicit persisted
    # current-outage acknowledgement — never timestamp equality and
    # never queue presence/absence alone.
    current = status.current_down
    assert current is not None  # guaranteed by the DOWN invariants
    if not current.acknowledged:
        # The DOWN alert of this outage was never acknowledged. Keep
        # it pending (it is never silently discarded, replaced or
        # reordered) and emit NO RECOVERED intent — the operator was
        # never notified about this outage, so a RECOVERED for it
        # would be misleading. The outage binding is cleared as part
        # of the transition to UP.
        return DeadManStatus(
            state=DeadManState.UP,
            consecutive_failures=0,
            consecutive_successes=0,
            state_changed_at=now,
            pending_notifications=status.pending_notifications,
            next_notification_id=status.next_notification_id,
        )
    # The DOWN alert of this outage was acknowledged: append the
    # RECOVERED intent for it, oldest-first order preserved, and
    # clear the outage binding as part of the transition to UP.
    recovered = DeadManNotification(
        notification_id=status.next_notification_id,
        kind=DeadManNotificationKind.RECOVERED,
        transition=DeadManTransition(
            from_state=DeadManState.DOWN,
            to_state=DeadManState.UP,
            at=now,
        ),
    )
    return DeadManStatus(
        state=DeadManState.UP,
        consecutive_failures=0,
        consecutive_successes=0,
        state_changed_at=now,
        pending_notifications=status.pending_notifications + (recovered,),
        next_notification_id=status.next_notification_id + 1,
    )


def _advance_failure(
    status: DeadManStatus, now: datetime
) -> DeadManStatus:
    """Apply one failed probe outcome (state machine internals)."""
    if status.state is DeadManState.DOWN:
        # Confirmed DOWN holds: any failure resets the pending
        # recovery streak; the pending queue and the outage binding
        # stay exactly as they are — no duplicate intent is created
        # and the binding is never replaced.
        return replace(
            status,
            consecutive_failures=0,
            consecutive_successes=0,
        )
    failures = status.consecutive_failures + 1
    if failures < DOWN_CONFIRMATIONS:
        # Failures #1 and #2 (in UP or UNKNOWN): stay in the current
        # state carrying the incremented streak.
        return replace(
            status,
            consecutive_failures=failures,
            consecutive_successes=0,
        )
    # Exact threshold reached: confirm DOWN on THIS evaluation,
    # append exactly one DOWN intent with a freshly allocated id and
    # BIND that exact id to the current outage. Older
    # still-unacknowledged intents are preserved in order — nothing
    # is ever replaced, dropped or reordered; repeated ticks for
    # this same transition allocate nothing further and never touch
    # the binding.
    transition = DeadManTransition(
        from_state=status.state,
        to_state=DeadManState.DOWN,
        at=now,
    )
    intent = DeadManNotification(
        notification_id=status.next_notification_id,
        kind=DeadManNotificationKind.DOWN,
        transition=transition,
    )
    return DeadManStatus(
        state=DeadManState.DOWN,
        consecutive_failures=0,
        consecutive_successes=0,
        state_changed_at=now,
        pending_notifications=status.pending_notifications + (intent,),
        next_notification_id=status.next_notification_id + 1,
        current_down=DeadManCurrentDown(
            notification_id=status.next_notification_id,
            acknowledged=False,
        ),
    )


def advance_deadman_status(
    *,
    status: DeadManStatus,
    outcome: DeadManProbeOutcome,
    now: datetime,
) -> DeadManStatus:
    """Advance the dead-man state by one classified probe outcome.

    Pure and deterministic: reads no clock (``now`` is the explicit
    evaluation moment and must be truly timezone-aware), performs no
    I/O, mutates neither the arguments nor any module state, and
    returns a fresh immutable status. A ``now`` earlier than the
    current state's ``state_changed_at`` anchor fails closed — the
    evidence clock moved backwards and is never silently accepted.

    Debounce (frozen H0, see the module contract): 3 consecutive
    failures confirm DOWN, 2 consecutive successes confirm recovery;
    a single success resets the failure streak and a single failure
    resets the success streak.
    """
    if not isinstance(status, DeadManStatus):
        raise ValueError(
            "status must be a DeadManStatus, got"
            f" {type(status).__name__}"
        )
    if not isinstance(outcome, DeadManProbeOutcome):
        # A wrong outcome value must never be silently treated as a
        # failure (or a success): it is an explicit input error.
        raise ValueError(
            "outcome must be a DeadManProbeOutcome, got"
            f" {type(outcome).__name__}"
        )
    if not isinstance(now, datetime):
        raise ValueError(
            f"now must be a datetime, got {type(now).__name__}"
        )
    _require_aware("now", now)
    anchor = status.state_changed_at
    if anchor is not None and now < anchor:
        raise ValueError(
            f"now must not be before state_changed_at"
            f" ({now!r} < {anchor!r}):"
            " inconsistent clock evidence fails closed"
        )
    if outcome is DeadManProbeOutcome.HEALTHY:
        return _advance_success(status, now)
    return _advance_failure(status, now)


def acknowledge_deadman_notification(
    *,
    status: DeadManStatus,
    notification_id: int,
) -> DeadManStatus:
    """Acknowledge the OLDEST pending notification intent, by id.

    Strict ordered acknowledgement contract (delivery order is
    oldest-first and never silently reordered): the call succeeds
    only when ``notification_id`` is exactly the id of the FIRST
    (oldest) pending intent, and it removes exactly that one
    intent — never a different one. The future H1B runtime calls
    this only after a successful delivery of that oldest intent;
    everything else in the status (state, streaks, time anchor,
    remaining queue order, next id) is untouched, with one
    deliberate exception: when the acknowledged id is the current
    outage's bound DOWN id, the binding's ``acknowledged`` flag
    becomes True — the fact survives the queue removal and any
    encode/decode restart.

    Fail-closed with ``ValueError``, queue unchanged:

    - nothing pending at all;
    - a STALE id (already acknowledged earlier) — it is no longer
      the head, so it can never clear a newer intent;
    - a WRONG id (a newer, non-head pending intent, or an id that
      was never allocated / is out of range).
    """
    if not isinstance(status, DeadManStatus):
        raise ValueError(
            "status must be a DeadManStatus, got"
            f" {type(status).__name__}"
        )
    _require_notification_id("notification_id", notification_id)
    if not status.pending_notifications:
        raise ValueError(
            "no pending notification to acknowledge"
        )
    head = status.pending_notifications[0]
    if notification_id != head.notification_id:
        raise ValueError(
            f"notification_id {notification_id!r} does not identify the"
            " oldest pending notification"
            f" ({head.notification_id!r}): acknowledgement is"
            " oldest-first and never removes a different intent"
        )
    # One single consistent construction: when the acknowledged id
    # IS the current outage's bound DOWN intent, the explicit
    # acknowledgement is recorded in the same step as the queue
    # removal, so the fact survives the removal (and any
    # encode/decode restart).
    current = status.current_down
    marked: DeadManCurrentDown | None = current
    if current is not None and current.notification_id == notification_id:
        marked = replace(current, acknowledged=True)
    return replace(
        status,
        pending_notifications=status.pending_notifications[1:],
        current_down=marked,
    )


class DeadManStateDecodeError(Exception):
    """Malformed or unusable persisted dead-man state.

    Fail-closed decoding boundary for the canonical operational
    snapshot: a document that is not valid JSON, has duplicate
    keys, non-finite constants, a missing/unknown/extra field, an
    unsupported schema version, wrong types, invalid enums or
    counters, malformed or naive timestamps, malformed ids, a
    non-array pending sequence, an invalid queue ordering or kind
    grammar, an invalid notification kind/transition pairing, an
    invalid current-DOWN binding, or any internally unsafe
    combination is NEVER silently coerced to a healthy baseline.
    The boundary deliberately does NOT attempt to prove unstored
    historical provenance. Messages name fields and reasons; the
    state carries no secrets.
    """


def _encode_datetime(value: datetime) -> str:
    """Canonical UTC ISO 8601 text (the Stage B1 precedent).

    Offsets are normalized to UTC so equal instants encode to equal
    text; decoding restores the same instant.
    """
    return value.astimezone(timezone.utc).isoformat()


def _encode_transition(transition: DeadManTransition) -> dict[str, str]:
    return {
        "from_state": transition.from_state.value,
        "to_state": transition.to_state.value,
        "at": _encode_datetime(transition.at),
    }


def encode_deadman_status(status: DeadManStatus) -> str:
    """Encode the dead-man status as deterministic versioned JSON.

    The document is the canonical operational snapshot (schema
    version, state, both debounce streaks, the state-change time
    anchor, the ordered pending notification intents with their
    ids, the next notification id and the current-outage binding)
    with sorted keys and canonical UTC ISO 8601 timestamps, so
    statuses with identical future behavior always encode to
    identical text. The state contains no secret fields by design.
    Writing the text to a file (atomically) belongs to the H1B
    runtime, not here.
    """
    if not isinstance(status, DeadManStatus):
        raise ValueError(
            "status must be a DeadManStatus, got"
            f" {type(status).__name__}"
        )
    document: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "state": status.state.value,
        "consecutive_failures": status.consecutive_failures,
        "consecutive_successes": status.consecutive_successes,
        "state_changed_at": (
            None
            if status.state_changed_at is None
            else _encode_datetime(status.state_changed_at)
        ),
        "pending_notifications": [
            {
                "notification_id": notification.notification_id,
                "kind": notification.kind.value,
                "transition": _encode_transition(notification.transition),
            }
            for notification in status.pending_notifications
        ],
        "next_notification_id": status.next_notification_id,
        "current_down": (
            None
            if status.current_down is None
            else {
                "notification_id": status.current_down.notification_id,
                "acknowledged": status.current_down.acknowledged,
            }
        ),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _reject_pairs_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON object hook: duplicate keys are malformed, never last-wins."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> NoReturn:
    """JSON hook: NaN/Infinity constants are malformed state."""
    raise ValueError(f"non-finite JSON constant {token!r}")


def _require_exact_keys(
    name: str, mapping: dict[str, object], fields: frozenset[str]
) -> None:
    """Key set check: missing/unknown/extra fields fail."""
    if set(mapping) != set(fields):
        raise DeadManStateDecodeError(
            f"{name} must contain exactly the fields"
            f" {sorted(fields)}, got {sorted(mapping)}"
        )


def _decode_state_value(name: str, value: object) -> DeadManState:
    if not isinstance(value, str):
        raise DeadManStateDecodeError(
            f"{name} must be a string, got {type(value).__name__}"
        )
    try:
        return DeadManState(value)
    except ValueError:
        raise DeadManStateDecodeError(
            f"{name} is not a known dead-man state: {value!r}"
        ) from None


def _decode_notification_kind(
    name: str, value: object
) -> DeadManNotificationKind:
    if not isinstance(value, str):
        raise DeadManStateDecodeError(
            f"{name} must be a string, got {type(value).__name__}"
        )
    try:
        return DeadManNotificationKind(value)
    except ValueError:
        raise DeadManStateDecodeError(
            f"{name} is not a known notification kind: {value!r}"
        ) from None


def _decode_counter(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON integer, got {type(value).__name__}"
        )
    if value < 0:
        raise DeadManStateDecodeError(
            f"{name} must be >= 0, got {value!r}"
        )
    return value


def _decode_notification_id(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON integer, got {type(value).__name__}"
        )
    if value < 1:
        raise DeadManStateDecodeError(
            f"{name} must be >= 1, got {value!r}"
        )
    return value


def _decode_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON boolean, got"
            f" {type(value).__name__}"
        )
    return value


def _decode_datetime(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise DeadManStateDecodeError(
            f"{name} must be an ISO 8601 datetime string, got"
            f" {type(value).__name__}"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DeadManStateDecodeError(
            f"{name} must be a valid ISO 8601 datetime"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeadManStateDecodeError(f"{name} must be timezone-aware")
    return parsed


def _decode_optional_datetime(
    name: str, value: object
) -> datetime | None:
    if value is None:
        return None
    return _decode_datetime(name, value)


def _decode_transition(name: str, value: object) -> DeadManTransition:
    if not isinstance(value, dict):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON object, got {type(value).__name__}"
        )
    _require_exact_keys(name, value, _TRANSITION_FIELDS)
    try:
        return DeadManTransition(
            from_state=_decode_state_value(
                f"{name}.from_state", value["from_state"]
            ),
            to_state=_decode_state_value(
                f"{name}.to_state", value["to_state"]
            ),
            at=_decode_datetime(f"{name}.at", value["at"]),
        )
    except DeadManStateDecodeError:
        raise
    except ValueError as error:
        raise DeadManStateDecodeError(
            f"invalid persisted dead-man transition: {error}"
        ) from error


def _decode_notification(
    name: str, value: object
) -> DeadManNotification:
    if not isinstance(value, dict):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON object, got {type(value).__name__}"
        )
    _require_exact_keys(name, value, _NOTIFICATION_FIELDS)
    kind = _decode_notification_kind(f"{name}.kind", value["kind"])
    transition = _decode_transition(
        f"{name}.transition", value["transition"]
    )
    try:
        return DeadManNotification(
            notification_id=_decode_notification_id(
                f"{name}.notification_id", value["notification_id"]
            ),
            kind=kind,
            transition=transition,
        )
    except DeadManStateDecodeError:
        raise
    except ValueError as error:
        raise DeadManStateDecodeError(
            f"invalid persisted dead-man notification: {error}"
        ) from error


def _decode_notifications(
    name: str, value: object
) -> tuple[DeadManNotification, ...]:
    if not isinstance(value, list):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON array, got {type(value).__name__}"
        )
    return tuple(
        _decode_notification(f"{name}[{index}]", element)
        for index, element in enumerate(value)
    )


def _decode_current_down(
    name: str, value: object
) -> DeadManCurrentDown | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DeadManStateDecodeError(
            f"{name} must be a JSON object or null, got"
            f" {type(value).__name__}"
        )
    _require_exact_keys(name, value, _CURRENT_DOWN_FIELDS)
    try:
        return DeadManCurrentDown(
            notification_id=_decode_notification_id(
                f"{name}.notification_id", value["notification_id"]
            ),
            acknowledged=_decode_bool(
                f"{name}.acknowledged", value["acknowledged"]
            ),
        )
    except DeadManStateDecodeError:
        raise
    except ValueError as error:
        raise DeadManStateDecodeError(
            f"invalid persisted current-outage binding: {error}"
        ) from error


def decode_deadman_status(payload: str) -> DeadManStatus:
    """Strictly decode a persisted canonical dead-man snapshot.

    Fail-closed on every deviation: non-string input, invalid JSON,
    duplicate object keys, non-finite JSON constants, pathological
    nesting, a non-object root, missing/unknown/extra fields at
    every level, an unsupported ``schema_version``, unknown enum
    values, bool/float-as-counter, bool/float-as-id, negative
    values, malformed or naive timestamps, a non-array pending
    sequence, invalid queue ordering (non-increasing, duplicate,
    non-contiguous or unanchored ids), an invalid pending kind
    grammar (``RECOVERED? DOWN*``; a RECOVERED with id 1), invalid
    notification kind/transition pairing, a malformed current-outage
    binding, and any internally unsafe combination (UNKNOWN outside
    the canonical initial form, UP/UNKNOWN with a binding, DOWN
    without one, a binding id that is not ``next_notification_id -
    1``, an unacknowledged binding missing from the pending tail or
    dated away from ``state_changed_at``, an acknowledged binding
    with something still pending, ledger times after the anchor, ...).

    The boundary is deliberately bounded to the snapshot's own
    facts: it proves the exact schema, types, enums, timestamps,
    counters, ids, queue ordering/grammar and internal future-
    behavior safety — it never attempts to reconstruct or
    distinguish the intentionally unstored historical provenance of
    a canonical state. Malformed state NEVER silently becomes a
    healthy baseline — :class:`DeadManStateDecodeError` is the only
    outward failure. Reading the state file itself belongs to the
    H1B runtime, not here.
    """
    if not isinstance(payload, str):
        raise DeadManStateDecodeError(
            "persisted dead-man state must be a string, got"
            f" {type(payload).__name__}"
        )
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_reject_pairs_duplicate,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError) as error:
        raise DeadManStateDecodeError(
            f"malformed persisted dead-man state JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise DeadManStateDecodeError(
            "persisted dead-man state must be a JSON object, got"
            f" {type(document).__name__}"
        )
    _require_exact_keys("dead-man state", document, _STATE_FIELDS)
    version = document["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != _SCHEMA_VERSION
    ):
        raise DeadManStateDecodeError(
            "unsupported dead-man state schema_version"
            f" {version!r}: this module supports exactly"
            f" {_SCHEMA_VERSION}"
        )
    try:
        return DeadManStatus(
            state=_decode_state_value("state", document["state"]),
            consecutive_failures=_decode_counter(
                "consecutive_failures", document["consecutive_failures"]
            ),
            consecutive_successes=_decode_counter(
                "consecutive_successes",
                document["consecutive_successes"],
            ),
            state_changed_at=_decode_optional_datetime(
                "state_changed_at", document["state_changed_at"]
            ),
            pending_notifications=_decode_notifications(
                "pending_notifications", document["pending_notifications"]
            ),
            next_notification_id=_decode_notification_id(
                "next_notification_id",
                document["next_notification_id"],
            ),
            current_down=_decode_current_down(
                "current_down", document["current_down"]
            ),
        )
    except DeadManStateDecodeError:
        raise
    except ValueError as error:
        raise DeadManStateDecodeError(
            f"invalid persisted dead-man state: {error}"
        ) from error
