"""Minimal heartbeat repository on top of the SQLite schema.

Stage B1 persistence foundation: a typed, deterministic storage API
sufficient for heartbeat ingestion and later freshness checks
(Stage B/D). It is deliberately not a generic abstraction framework.

Persistence invariants (normative):

- node identity is an explicit ``node`` column; every query filters
  by it, so telemetry of different servers can never mix;
- node identity must be a non-empty string after ``strip()`` —
  whitespace-only identities are rejected at the persistence
  boundary. Names are stored verbatim: no normalization, no
  case folding ("Prod" and "prod" stay distinct);
- timezone-aware datetimes are canonically serialized as UTC ISO 8601
  text (``astimezone(UTC).isoformat()``), so the textual ordering in
  SQLite is chronological and unambiguous; on read they are restored
  as timezone-aware datetimes denoting the same instant. Awareness
  is checked by the full Python rule — ``tzinfo is not None`` AND
  ``utcoffset() is not None`` — because a non-None ``tzinfo`` whose
  ``utcoffset()`` returns None is effectively naive and would make
  ``astimezone`` reinterpret the instant as local time;
- all telemetry floats round-trip exactly through REAL columns
  (domain validation rejects NaN/inf, which have no portable
  SQLite representation);
- "latest heartbeat" is deterministic: rows are ordered by
  ``received_at DESC, id DESC``, so on a full tie the row inserted
  last wins;
- every query touching values is parameterized; SQL text is built
  only from module-level constants, never from user input.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_sentinel.domain import HostTelemetry, LoadAverage, ResourceUsage
from hermes_sentinel.persistence.schema import initialize_schema

__all__ = [
    "HeartbeatRecord",
    "HeartbeatRepository",
    "connect",
]


def _require_valid_node(name: str, value: str) -> None:
    """Node identity invariant: non-empty string after strip().

    Deliberately no normalization or case folding: significant node
    names are stored and compared verbatim.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{name} must be a non-empty string after strip(),"
            f" got {value!r}"
        )


def _require_aware(name: str, value: datetime) -> None:
    """True datetime awareness per authoritative Python semantics.

    A datetime is timezone-aware only if BOTH hold:
    ``value.tzinfo is not None`` AND ``value.utcoffset() is not None``.
    A non-None tzinfo whose ``utcoffset()`` returns None denotes an
    effectively naive datetime; ``astimezone`` would reinterpret it
    as local time and silently change the instant.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware (tzinfo is not None and"
            f" utcoffset() is not None), got {value!r}"
        )


def serialize_timestamp(value: datetime) -> str:
    """Serialize a truly timezone-aware datetime to canonical UTC ISO text.

    Fail-closed: an effectively naive datetime (tzinfo set but
    ``utcoffset()`` None) is rejected instead of being silently
    converted via a local-time assumption.
    """
    _require_aware("timestamp", value)
    return value.astimezone(UTC).isoformat()


def parse_timestamp(value: str) -> datetime:
    """Parse canonical UTC ISO text back into a datetime.

    Fail-closed: the restored value is confirmed to be truly
    timezone-aware by the same rule before being returned.
    """
    parsed = datetime.fromisoformat(value)
    try:
        _require_aware("stored timestamp", parsed)
    except ValueError as error:
        raise ValueError(
            f"stored timestamp must be timezone-aware: {value!r}"
        ) from error
    return parsed


@dataclass(frozen=True, slots=True)
class HeartbeatRecord:
    """One accepted heartbeat observation.

    ``telemetry`` is the host reporter payload (Stage A domain
    contract); ``received_at`` is the central Sentinel clock moment
    when the heartbeat was accepted. Node identity is explicit:
    ``node`` is the telemetry host name and is stored as its own
    column.
    """

    telemetry: HostTelemetry
    received_at: datetime

    def __post_init__(self) -> None:
        _require_valid_node("node (telemetry.host)", self.telemetry.host)
        _require_aware("received_at", self.received_at)
        # reported_at is guarded here too (defence in depth): an
        # upstream HostTelemetry built with an exotic tzinfo that
        # Stage A accepted must still fail at the persistence
        # boundary, before any write.
        _require_aware("reported_at (telemetry.timestamp)",
                       self.telemetry.timestamp)

    @property
    def node(self) -> str:
        """Explicit node identity of this observation."""
        return self.telemetry.host


_COLUMNS: tuple[str, ...] = (
    "node",
    "reported_at",
    "received_at",
    "uptime_seconds",
    "load_one",
    "load_five",
    "load_fifteen",
    "cpu_percent",
    "ram_used",
    "ram_total",
    "ram_percent",
    "swap_used",
    "swap_total",
    "swap_percent",
    "root_fs_used",
    "root_fs_total",
    "root_fs_percent",
    "root_inode_used",
    "root_inode_total",
    "root_inode_percent",
)

# SQL text is assembled only from module-level constants (never from
# user values); all values travel through ? placeholders.
_COLUMN_LIST = ", ".join(_COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)

_INSERT_SQL = (
    f"INSERT INTO heartbeat_observation ({_COLUMN_LIST})"
    f" VALUES ({_PLACEHOLDERS})"
)
_LATEST_SQL = (
    f"SELECT {_COLUMN_LIST} FROM heartbeat_observation"
    " WHERE node = ?"
    " ORDER BY received_at DESC, id DESC"
    " LIMIT 1"
)
_LATEST_RECEIVED_AT_SQL = (
    "SELECT received_at FROM heartbeat_observation"
    " WHERE node = ?"
    " ORDER BY received_at DESC, id DESC"
    " LIMIT 1"
)
_COUNT_SQL = "SELECT COUNT(*) FROM heartbeat_observation"


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (or create) the Sentinel database with the schema applied.

    Fail-closed: if the existing database is incompatible with the
    canonical B1 schema or has an unsupported (newer) schema version,
    :class:`~hermes_sentinel.persistence.SchemaCompatibilityError` /
    :class:`~hermes_sentinel.persistence.UnsupportedSchemaVersionError`
    is raised and the connection is closed. Safe on repeated
    application restarts.
    """
    connection = sqlite3.connect(str(path))
    try:
        initialize_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


class HeartbeatRepository:
    """Minimal storage API for heartbeat observations (Stage B1).

    Holds one ``sqlite3.Connection``; not thread-safe (a single
    service thread is the B-stage usage model).
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        # Idempotent: ensures the schema exists even if the caller
        # opened a raw connection without calling connect().
        initialize_schema(connection)
        self._connection = connection

    def insert_heartbeat(self, record: HeartbeatRecord) -> int:
        """Persist one heartbeat observation; return its row id."""
        telemetry = record.telemetry
        parameters: tuple[Any, ...] = (
            record.node,
            serialize_timestamp(telemetry.timestamp),
            serialize_timestamp(record.received_at),
            telemetry.uptime_seconds,
            telemetry.load.one,
            telemetry.load.five,
            telemetry.load.fifteen,
            telemetry.cpu_percent,
            telemetry.ram.used,
            telemetry.ram.total,
            telemetry.ram.percent,
            telemetry.swap.used,
            telemetry.swap.total,
            telemetry.swap.percent,
            telemetry.root_filesystem.used,
            telemetry.root_filesystem.total,
            telemetry.root_filesystem.percent,
            telemetry.root_inodes.used,
            telemetry.root_inodes.total,
            telemetry.root_inodes.percent,
        )
        with self._connection:
            cursor = self._connection.execute(_INSERT_SQL, parameters)
        rowid = cursor.lastrowid
        assert rowid is not None  # INSERT always produces a rowid
        return int(rowid)

    def latest_heartbeat(self, node: str) -> HeartbeatRecord | None:
        """Return the latest stored heartbeat for ``node``, if any.

        Deterministic: ordered by ``received_at DESC, id DESC`` — on a
        full timestamp tie the row inserted last wins.
        """
        _require_valid_node("node", node)
        row = self._connection.execute(_LATEST_SQL, (node,)).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def latest_received_at(self, node: str) -> datetime | None:
        """Return the central receive timestamp of the latest heartbeat.

        Sufficient for heartbeat freshness checks in later stages
        without loading the full telemetry payload.
        """
        _require_valid_node("node", node)
        row = self._connection.execute(
            _LATEST_RECEIVED_AT_SQL, (node,)
        ).fetchone()
        if row is None:
            return None
        return parse_timestamp(str(row[0]))

    def count_observations(self) -> int:
        """Total number of stored heartbeat observations (test helper)."""
        row = self._connection.execute(_COUNT_SQL).fetchone()
        return int(row[0])


def _row_to_record(row: tuple[Any, ...]) -> HeartbeatRecord:
    """Rebuild a HeartbeatRecord from a stored row without data loss."""
    (
        node,
        reported_at,
        received_at,
        uptime_seconds,
        load_one,
        load_five,
        load_fifteen,
        cpu_percent,
        ram_used,
        ram_total,
        ram_percent,
        swap_used,
        swap_total,
        swap_percent,
        root_fs_used,
        root_fs_total,
        root_fs_percent,
        root_inode_used,
        root_inode_total,
        root_inode_percent,
    ) = row
    telemetry = HostTelemetry(
        host=str(node),
        timestamp=parse_timestamp(str(reported_at)),
        uptime_seconds=float(uptime_seconds),
        load=LoadAverage(
            one=float(load_one),
            five=float(load_five),
            fifteen=float(load_fifteen),
        ),
        cpu_percent=float(cpu_percent),
        ram=ResourceUsage(
            used=float(ram_used),
            total=float(ram_total),
            percent=float(ram_percent),
        ),
        swap=ResourceUsage(
            used=float(swap_used),
            total=float(swap_total),
            percent=float(swap_percent),
        ),
        root_filesystem=ResourceUsage(
            used=float(root_fs_used),
            total=float(root_fs_total),
            percent=float(root_fs_percent),
        ),
        root_inodes=ResourceUsage(
            used=float(root_inode_used),
            total=float(root_inode_total),
            percent=float(root_inode_percent),
        ),
    )
    return HeartbeatRecord(
        telemetry=telemetry,
        received_at=parse_timestamp(str(received_at)),
    )
