"""Deterministic tests for the Stage B2 heartbeat ingestion core.

Each test uses its own temporary SQLite database in a temporary
directory: no shared mutable test databases. The central clock is
always injected (fixed moments), so no test depends on wall time.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing the
# package (stdlib unittest has no pythonpath support; pytest gets the
# same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.config import (  # noqa: E402
    ExternalCheckSettings,
    HeartbeatSettings,
    HostConfig,
    SentinelConfig,
)
from hermes_sentinel.domain import (  # noqa: E402
    HostTelemetry,
    LoadAverage,
    ResourceUsage,
)
from hermes_sentinel.ingestion import (  # noqa: E402
    HeartbeatIngestor,
    HeartbeatReceipt,
    InvalidClockResultError,
    UnknownNodeError,
    utc_now,
)
from hermes_sentinel.persistence import (  # noqa: E402
    HeartbeatRecord,
    HeartbeatRepository,
    connect,
)

# Two independent time axes used throughout: the server-reported
# moment and the central receive moment are always distinct instants.
_REPORTED_AT = datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC)
_RECEIVED_AT = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
_RECEIVED_AT_LATER = datetime(2026, 9, 7, 5, 1, 0, tzinfo=UTC)
_OFFSET_TZ = timezone(timedelta(hours=5))
# The same instant as _RECEIVED_AT, expressed with a fixed +05:00 offset.
_RECEIVED_AT_OFFSET = _RECEIVED_AT.astimezone(_OFFSET_TZ)


class _NullOffsetTZ(tzinfo):
    """Exotic tzinfo: non-None, but utcoffset() returns None.

    A datetime built with it is effectively naive per authoritative
    Python semantics, even though ``tzinfo is not None``.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return "NullOffset"

    def __repr__(self) -> str:  # deterministic test output
        return "NullOffsetTZ()"


def _heartbeat() -> HeartbeatSettings:
    return HeartbeatSettings(
        expected_interval_seconds=30.0, stale_after_seconds=90.0
    )


def _external() -> ExternalCheckSettings:
    return ExternalCheckSettings(tcp_host="203.0.113.10", tcp_port=22)


def _host(**overrides: object) -> HostConfig:
    defaults: dict[str, object] = {
        "name": "vds-01",
        "heartbeat": _heartbeat(),
        "external": _external(),
    }
    defaults.update(overrides)
    return HostConfig(**defaults)  # type: ignore[arg-type]


def _config(*hosts: HostConfig) -> SentinelConfig:
    return SentinelConfig(hosts=hosts)


def _telemetry(**overrides: object) -> HostTelemetry:
    """Build a valid HostTelemetry snapshot with optional overrides."""
    defaults: dict[str, object] = {
        "host": "vds-01",
        "timestamp": _REPORTED_AT,
        "uptime_seconds": 3600.5,
        "load": LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        "cpu_percent": 12.5,
        "ram": ResourceUsage(used=1.0, total=2.0, percent=50.0),
        "swap": ResourceUsage(used=0.0, total=0.0, percent=0.0),
        "root_filesystem": ResourceUsage(used=10.0, total=40.0, percent=25.0),
        "root_inodes": ResourceUsage(used=100.0, total=1000.0, percent=10.0),
    }
    defaults.update(overrides)
    return HostTelemetry(**defaults)  # type: ignore[arg-type]


class _ClockSequence:
    """Injectable central clock returning fixed moments in order.

    The final moment is returned for every subsequent call, so tests
    with one expected call work as well.
    """

    def __init__(self, *moments: datetime) -> None:
        self._moments = list(moments)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if len(self._moments) > 1:
            return self._moments.pop(0)
        return self._moments[0]


class _ExplodingClock:
    """Clock that must never be called; fails the test if it is."""

    def __call__(self) -> datetime:
        raise AssertionError("central clock must not be called")


class _FailingRepository(HeartbeatRepository):
    """Repository whose insert always fails (persistence failure)."""

    def insert_heartbeat(self, record: HeartbeatRecord) -> int:
        raise sqlite3.OperationalError("simulated persistence failure")


class IngestionTestCase(unittest.TestCase):
    """Base: one temporary directory, database and repository per test.

    Default configuration contains one node "vds-01" (no services) —
    the normal production configuration.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sentinel.sqlite3"
        self.connection = connect(self.db_path)
        self.addCleanup(self.connection.close)
        self.repository = HeartbeatRepository(self.connection)
        self.config = _config(_host())
        self.ingestor = HeartbeatIngestor(
            self.config, self.repository, clock=_ClockSequence(_RECEIVED_AT)
        )

    def _row_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM heartbeat_observation"
        ).fetchone()
        return int(row[0])

    def _stored_rows(self) -> list[tuple[object, ...]]:
        cursor = self.connection.execute(
            "SELECT id, node, reported_at, received_at"
            " FROM heartbeat_observation ORDER BY id"
        )
        return list(cursor.fetchall())


class AcceptIngestionTest(IngestionTestCase):
    def test_configured_node_heartbeat_is_accepted(self) -> None:
        """(1) A configured node's heartbeat is accepted with a receipt."""
        receipt = self.ingestor.ingest(_telemetry())
        self.assertIsInstance(receipt, HeartbeatReceipt)
        self.assertEqual(receipt.node, "vds-01")

    def test_observation_actually_reaches_b1_repository(self) -> None:
        """(2) The observation is really stored in the B1 repository."""
        self.ingestor.ingest(_telemetry())
        self.assertEqual(self.repository.count_observations(), 1)
        latest = self.repository.latest_heartbeat("vds-01")
        self.assertIsNotNone(latest)
        self.assertEqual(self.repository.latest_received_at("vds-01"),
                         _RECEIVED_AT)

    def test_receipt_contains_node_observation_id_and_received_at(self) -> None:
        """(3) The receipt carries node, persistence id, received_at."""
        receipt = self.ingestor.ingest(_telemetry())
        rows = self._stored_rows()
        self.assertEqual(len(rows), 1)
        row_id, row_node, _row_reported, row_received = rows[0]
        self.assertEqual(receipt.node, row_node)
        self.assertEqual(receipt.observation_id, int(row_id))
        self.assertEqual(receipt.received_at, _RECEIVED_AT)
        self.assertEqual(row_node, "vds-01")
        self.assertEqual(row_received, _RECEIVED_AT.astimezone(UTC).isoformat())


class TimeAxesTest(IngestionTestCase):
    def test_received_at_comes_from_injected_clock_not_telemetry(self) -> None:
        """(4) received_at is the central clock moment, not reported one."""
        receipt = self.ingestor.ingest(_telemetry())
        self.assertNotEqual(receipt.received_at, _REPORTED_AT)
        self.assertEqual(receipt.received_at, _RECEIVED_AT)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.received_at, _RECEIVED_AT)
        self.assertNotEqual(latest.received_at, _REPORTED_AT)

    def test_reported_at_is_preserved_not_replaced(self) -> None:
        """(5) The server-reported timestamp survives as its own axis."""
        self.ingestor.ingest(_telemetry())
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry.timestamp, _REPORTED_AT)
        self.assertNotEqual(latest.telemetry.timestamp, latest.received_at)

    def test_full_telemetry_round_trip_through_ingestion_and_persistence(
        self,
    ) -> None:
        """(6) Mandatory telemetry round-trip: ingestion -> persistence
        -> latest heartbeat, without field loss or mutation."""
        telemetry = _telemetry()
        self.ingestor.ingest(telemetry)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry, telemetry)
        self.assertEqual(latest.telemetry.host, "vds-01")
        self.assertEqual(latest.telemetry.uptime_seconds, 3600.5)
        self.assertEqual(
            latest.telemetry.load,
            LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        )
        self.assertEqual(latest.telemetry.cpu_percent, 12.5)
        self.assertEqual(
            latest.telemetry.ram,
            ResourceUsage(used=1.0, total=2.0, percent=50.0),
        )
        self.assertEqual(
            latest.telemetry.swap,
            ResourceUsage(used=0.0, total=0.0, percent=0.0),
        )
        self.assertEqual(
            latest.telemetry.root_filesystem,
            ResourceUsage(used=10.0, total=40.0, percent=25.0),
        )
        self.assertEqual(
            latest.telemetry.root_inodes,
            ResourceUsage(used=100.0, total=1000.0, percent=10.0),
        )
        # The caller's telemetry object itself is untouched.
        self.assertEqual(telemetry, _telemetry())


class UnknownNodeTest(IngestionTestCase):
    def test_unknown_node_fails_closed_with_explicit_error(self) -> None:
        """(7) Unknown node: explicit error, zero new rows."""
        with self.assertRaises(UnknownNodeError):
            self.ingestor.ingest(_telemetry(host="stranger"))
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repository.count_observations(), 0)
        self.assertIsNone(self.repository.latest_heartbeat("stranger"))

    def test_unknown_node_is_checked_before_the_clock_is_consulted(self) -> None:
        """Identity validation happens first: an unknown node fails
        even before the central clock is called."""
        ingestor = HeartbeatIngestor(
            self.config, self.repository, clock=_ExplodingClock()
        )
        with self.assertRaises(UnknownNodeError):
            ingestor.ingest(_telemetry(host="stranger"))
        self.assertEqual(self._row_count(), 0)

    def test_identity_is_case_sensitive(self) -> None:
        """(8) Configured "Prod" does not accept "prod" — verbatim,
        case-sensitive identity, no normalization."""
        config = _config(_host(name="Prod"))
        ingestor = HeartbeatIngestor(
            config, self.repository, clock=_ClockSequence(_RECEIVED_AT)
        )
        with self.assertRaises(UnknownNodeError):
            ingestor.ingest(_telemetry(host="prod"))
        with self.assertRaises(UnknownNodeError):
            ingestor.ingest(_telemetry(host="PROD"))
        self.assertEqual(self._row_count(), 0)
        # The exact configured name is accepted.
        receipt = ingestor.ingest(_telemetry(host="Prod"))
        self.assertEqual(receipt.node, "Prod")
        self.assertEqual(self._row_count(), 1)


class CentralClockTest(IngestionTestCase):
    def test_naive_clock_result_is_rejected_without_writes(self) -> None:
        """(9a) A naive datetime from the clock fails; nothing stored."""
        ingestor = HeartbeatIngestor(
            self.config,
            self.repository,
            clock=_ClockSequence(datetime(2026, 9, 7, 5, 0, 0)),
        )
        with self.assertRaises(InvalidClockResultError):
            ingestor.ingest(_telemetry())
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repository.count_observations(), 0)

    def test_tzinfo_without_utcoffset_is_rejected_without_writes(self) -> None:
        """(9b) tzinfo set but utcoffset() None is effectively naive:
        rejected, nothing stored."""
        ingestor = HeartbeatIngestor(
            self.config,
            self.repository,
            clock=_ClockSequence(
                datetime(2026, 9, 7, 5, 0, 0, tzinfo=_NullOffsetTZ())
            ),
        )
        with self.assertRaises(InvalidClockResultError):
            ingestor.ingest(_telemetry())
        self.assertEqual(self._row_count(), 0)

    def test_valid_utc_clock_is_accepted(self) -> None:
        """(10) A valid timezone-aware UTC clock result is accepted."""
        receipt = self.ingestor.ingest(_telemetry())
        self.assertEqual(receipt.received_at, _RECEIVED_AT)
        self.assertEqual(receipt.received_at.utcoffset(), timedelta(0))
        self.assertEqual(self._row_count(), 1)

    def test_valid_fixed_offset_clock_preserves_the_instant(self) -> None:
        """(11) A fixed non-zero-offset clock is accepted; B1 canonical
        UTC persistence keeps the same instant."""
        ingestor = HeartbeatIngestor(
            self.config,
            self.repository,
            clock=_ClockSequence(_RECEIVED_AT_OFFSET),
        )
        receipt = ingestor.ingest(_telemetry())
        # Same instant, expressed in the fixed +05:00 offset.
        self.assertEqual(receipt.received_at, _RECEIVED_AT_OFFSET)
        self.assertEqual(receipt.received_at, _RECEIVED_AT)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        # B1 restores the canonical UTC representation of the instant.
        self.assertEqual(latest.received_at, _RECEIVED_AT)
        self.assertEqual(latest.received_at.utcoffset(), timedelta(0))
        self.assertEqual(self.repository.latest_received_at("vds-01"),
                         _RECEIVED_AT)


class MultipleObservationsTest(IngestionTestCase):
    def test_repeated_heartbeats_are_separate_observations(self) -> None:
        """(12) No deduplication: each heartbeat is its own row."""
        ingestor = HeartbeatIngestor(
            self.config,
            self.repository,
            clock=_ClockSequence(_RECEIVED_AT, _RECEIVED_AT_LATER),
        )
        first = ingestor.ingest(_telemetry())
        second = ingestor.ingest(_telemetry())
        self.assertNotEqual(first.observation_id, second.observation_id)
        self.assertEqual(self._row_count(), 2)
        self.assertEqual(self.repository.count_observations(), 2)
        # Deterministic latest: the later central moment wins.
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.received_at, _RECEIVED_AT_LATER)
        self.assertEqual(latest.telemetry.timestamp, _REPORTED_AT)

    def test_different_configured_nodes_do_not_mix(self) -> None:
        """(13) Telemetry of different nodes stays separated."""
        config = _config(_host(name="vds-01"), _host(name="vds-02"))
        ingestor = HeartbeatIngestor(
            config, self.repository, clock=_ClockSequence(_RECEIVED_AT)
        )
        first = ingestor.ingest(_telemetry(host="vds-01"))
        second = ingestor.ingest(_telemetry(host="vds-02"))
        self.assertEqual(first.node, "vds-01")
        self.assertEqual(second.node, "vds-02")
        self.assertEqual(self._row_count(), 2)
        latest_one = self.repository.latest_heartbeat("vds-01")
        latest_two = self.repository.latest_heartbeat("vds-02")
        assert latest_one is not None and latest_two is not None
        self.assertEqual(latest_one.telemetry.host, "vds-01")
        self.assertEqual(latest_two.telemetry.host, "vds-02")
        self.assertEqual(
            self.repository.latest_received_at("vds-01"), _RECEIVED_AT
        )
        self.assertEqual(
            self.repository.latest_received_at("vds-02"), _RECEIVED_AT
        )

    def test_services_do_not_affect_ingestion(self) -> None:
        """(14) services=() / services=(...) is irrelevant to ingestion."""
        config = _config(
            _host(name="plain"),
            _host(name="with-services", services=("nginx", "postgres")),
        )
        ingestor = HeartbeatIngestor(
            config, self.repository, clock=_ClockSequence(_RECEIVED_AT)
        )
        plain = ingestor.ingest(_telemetry(host="plain"))
        serviced = ingestor.ingest(_telemetry(host="with-services"))
        self.assertEqual(plain.node, "plain")
        self.assertEqual(serviced.node, "with-services")
        self.assertEqual(self._row_count(), 2)
        # Identical telemetry apart from the node identity: the stored
        # observations differ only by node and id.
        latest_plain = self.repository.latest_heartbeat("plain")
        latest_serviced = self.repository.latest_heartbeat("with-services")
        assert latest_plain is not None and latest_serviced is not None
        self.assertEqual(latest_plain.received_at, latest_serviced.received_at)
        self.assertEqual(
            latest_plain.telemetry.cpu_percent,
            latest_serviced.telemetry.cpu_percent,
        )


class PersistenceFailureTest(IngestionTestCase):
    def test_repository_failure_is_not_masked_as_success(self) -> None:
        """A failing repository insert propagates; no false receipt."""
        failing = _FailingRepository(self.connection)
        ingestor = HeartbeatIngestor(
            self.config, failing, clock=_ClockSequence(_RECEIVED_AT)
        )
        with self.assertRaises(sqlite3.OperationalError):
            ingestor.ingest(_telemetry())
        self.assertEqual(self._row_count(), 0)

    def test_closed_connection_failure_propagates(self) -> None:
        """A real persistence failure (closed connection) propagates."""
        connection = connect(self.db_path)
        repository = HeartbeatRepository(connection)
        ingestor = HeartbeatIngestor(
            self.config, repository, clock=_ClockSequence(_RECEIVED_AT)
        )
        connection.close()
        with self.assertRaises(sqlite3.Error):
            ingestor.ingest(_telemetry())


class ProductionDefaultsTest(unittest.TestCase):
    def test_default_clock_returns_truly_aware_utc_datetime(self) -> None:
        """The production default clock is truly timezone-aware UTC."""
        moment = utc_now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertIsNotNone(moment.utcoffset())
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_ingestor_default_clock_is_utc_now(self) -> None:
        """The ingestor uses the UTC production clock by default."""
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        repository = HeartbeatRepository(connection)
        ingestor = HeartbeatIngestor(_config(_host()), repository)
        receipt = ingestor.ingest(_telemetry())
        self.assertIsNotNone(receipt.received_at.tzinfo)
        self.assertIsNotNone(receipt.received_at.utcoffset())


if __name__ == "__main__":
    unittest.main()
