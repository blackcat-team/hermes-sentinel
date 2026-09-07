"""Transport-independent heartbeat ingestion core (Stage B2).

Application layer between a (future) reporter transport and the Stage
B1 persistence foundation:

    reporter / future HTTP transport
                |
    HeartbeatIngestor (this module)
                |
    B1 HeartbeatRepository
                |
              SQLite

B2 is deliberately not an HTTP/API transport: it only defines what
happens once a typed ``HostTelemetry`` payload has reached the central
Sentinel process.

Normative ingestion semantics (see docs/ARCHITECTURE.md):

- the reported node must be a configured Sentinel node. Identity is
  looked up in the existing Stage A ``SentinelConfig`` contract —
  exact/verbatim, case-sensitive, no normalization or case folding
  ("Prod" and "prod" stay distinct). An unknown node fails closed:
  an explicit error is raised before any write, so an invalid node
  can never reach persistence;
- the central ``received_at`` timestamp is assigned here by the
  Sentinel ingestion layer via an injectable clock — the reporter
  never defines the authoritative receive moment. The production
  default clock is timezone-aware UTC;
- a clock result must be *truly* timezone-aware: ``tzinfo is not
  None`` AND ``utcoffset() is not None`` (the same rule the B1
  persistence boundary enforces). An invalid clock result is an
  explicit failure and no observation is written;
- the server-reported ``telemetry.timestamp`` is a separate time axis
  (``reported_at``); it is stored alongside ``received_at`` and is
  never replaced by the central clock;
- server-reported telemetry passes through ingestion unmodified —
  CPU/RAM/load/… reach persistence without mutation;
- every accepted heartbeat is one observation; B2 introduces no
  deduplication or idempotency protocol;
- persistence failures from the B1 repository propagate — ingestion
  never masks a repository exception as a successful receipt.

Out of scope for B2: HTTP transport, reporter authentication, clock
skew policy, heartbeat freshness, and any HEALTHY/DEGRADED/DOWN
computation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from hermes_sentinel.config import SentinelConfig
from hermes_sentinel.domain import HostTelemetry
from hermes_sentinel.persistence import HeartbeatRecord, HeartbeatRepository

__all__ = [
    "Clock",
    "HeartbeatIngestionError",
    "UnknownNodeError",
    "InvalidClockResultError",
    "HeartbeatReceipt",
    "HeartbeatIngestor",
    "utc_now",
]


#: A central clock: returns the authoritative receive moment.
#: Injectable for deterministic tests; the production default is
#: :func:`utc_now`.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Production default clock: the current timezone-aware UTC moment."""
    return datetime.now(UTC)


class HeartbeatIngestionError(Exception):
    """Base class for deterministic heartbeat ingestion failures."""


class UnknownNodeError(HeartbeatIngestionError):
    """The reported node is not a configured Sentinel node (fail closed).

    Raised before any write: no observation row is created.
    """


class InvalidClockResultError(HeartbeatIngestionError):
    """The central clock returned a value that is not a truly
    timezone-aware ``datetime``.

    A datetime is truly aware only if both ``tzinfo is not None`` and
    ``utcoffset() is not None`` — the same rule the B1 persistence
    boundary enforces. Raised before any write: no observation row is
    created.
    """


@dataclass(frozen=True, slots=True)
class HeartbeatReceipt:
    """Minimal typed acknowledgement of one accepted heartbeat.

    Deliberately free of speculative metadata: the accepted node
    identity, the persistence observation id and the central receive
    moment.
    """

    node: str
    observation_id: int
    received_at: datetime


class HeartbeatIngestor:
    """Transport-neutral heartbeat ingestion operation (Stage B2).

    Validates the reported node against the Stage A configuration,
    assigns the central ``received_at`` moment from the injected
    clock, persists the observation through the B1
    ``HeartbeatRepository`` and returns a minimal receipt.

    Node identity semantics are those of ``SentinelConfig.host()``:
    verbatim, case-sensitive, unknown nodes fail closed.
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

    def ingest(self, telemetry: HostTelemetry) -> HeartbeatReceipt:
        """Accept one heartbeat observation and return its receipt.

        Order of checks (all before any write):

        1. the reported node must be a configured Sentinel node —
           otherwise :class:`UnknownNodeError` and no row is created;
        2. the central clock result must be a truly timezone-aware
           datetime — otherwise :class:`InvalidClockResultError` and
           no row is created.

        The telemetry itself is stored unmodified through the B1
        repository; a repository failure propagates unchanged (never
        masked as a successful receipt).
        """
        node = telemetry.host
        if self._config.host(node) is None:
            raise UnknownNodeError(f"unknown Sentinel node: {node!r}")

        received_at = self._clock()
        if not isinstance(received_at, datetime) or (
            received_at.tzinfo is None or received_at.utcoffset() is None
        ):
            raise InvalidClockResultError(
                "central clock must return a timezone-aware datetime"
                " (tzinfo is not None and utcoffset() is not None),"
                f" got {received_at!r}"
            )

        # HeartbeatRecord re-validates the true awareness of both time
        # axes (defence in depth, B1 boundary contract) before the
        # repository performs any write.
        record = HeartbeatRecord(telemetry=telemetry, received_at=received_at)
        observation_id = self._repository.insert_heartbeat(record)

        return HeartbeatReceipt(
            node=node,
            observation_id=observation_id,
            received_at=received_at,
        )
