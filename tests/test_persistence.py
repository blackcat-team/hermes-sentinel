"""Deterministic tests for the Stage B1 SQLite persistence foundation.

Each test uses its own temporary SQLite database in a temporary
directory: no shared mutable test databases.
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

from hermes_sentinel.domain import (  # noqa: E402
    HostTelemetry,
    LoadAverage,
    ResourceUsage,
)
from hermes_sentinel.persistence import (  # noqa: E402
    SCHEMA_VERSION,
    HeartbeatRecord,
    HeartbeatRepository,
    SchemaCompatibilityError,
    UnsupportedSchemaVersionError,
    connect,
    initialize_schema,
    schema_version,
)
from hermes_sentinel.persistence.schema import heartbeat_table_exists  # noqa: E402

_MOMENT = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
_OFFSET_TZ = timezone(timedelta(hours=5))


class _NullOffsetTZ(tzinfo):
    """Exotic tzinfo: non-None, but utcoffset() returns None.

    A datetime built with it is effectively naive per authoritative
    Python semantics, even though ``tzinfo is not None``. Stage A
    domain validation (tzinfo check only) may let it through; the
    persistence boundary must not.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return "NullOffset"

    def __repr__(self) -> str:  # deterministic test output
        return "NullOffsetTZ()"


def _telemetry(**overrides: object) -> HostTelemetry:
    """Build a valid HostTelemetry snapshot with optional overrides."""
    defaults: dict[str, object] = {
        "host": "vds-01",
        "timestamp": _MOMENT,
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


def _record(**overrides: object) -> HeartbeatRecord:
    """Build a valid HeartbeatRecord with optional telemetry overrides."""
    telemetry_overrides: dict[str, object] = {
        k: v for k, v in overrides.items() if k != "received_at"
    }
    received_at = overrides.get("received_at", _MOMENT)
    assert isinstance(received_at, datetime)
    return HeartbeatRecord(
        telemetry=_telemetry(**telemetry_overrides),
        received_at=received_at,
    )


class PersistenceTestCase(unittest.TestCase):
    """Base: one temporary directory and database per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sentinel.sqlite3"
        self.connection = connect(self.db_path)
        self.addCleanup(self.connection.close)
        self.repository = HeartbeatRepository(self.connection)


class SchemaInitializationTest(PersistenceTestCase):
    def test_initialize_schema_creates_table_and_index(self) -> None:
        self.assertTrue(heartbeat_table_exists(self.connection))
        index_count = self.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master"
            " WHERE type = 'index' AND name = ?",
            ("idx_heartbeat_node_received",),
        ).fetchone()
        self.assertEqual(int(index_count[0]), 1)

    def test_initialize_schema_records_schema_version(self) -> None:
        self.assertEqual(schema_version(self.connection), SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1)

    def test_repeated_schema_initialization_is_idempotent(self) -> None:
        initialize_schema(self.connection)
        initialize_schema(self.connection)
        table_count = self.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master"
            " WHERE type = 'table' AND name = ?",
            ("heartbeat_observation",),
        ).fetchone()
        self.assertEqual(int(table_count[0]), 1)
        self.assertEqual(schema_version(self.connection), SCHEMA_VERSION)

    def test_repeated_connect_preserves_existing_data(self) -> None:
        """Application restart: reconnecting keeps stored heartbeats."""
        self.repository.insert_heartbeat(_record())
        self.connection.commit()
        self.connection.close()
        reopened = connect(self.db_path)
        self.addCleanup(reopened.close)
        repository = HeartbeatRepository(reopened)
        latest = repository.latest_heartbeat("vds-01")
        self.assertIsNotNone(latest)

    def test_repository_bootstraps_schema_on_raw_connection(self) -> None:
        raw = sqlite3.connect(self.db_path)
        self.addCleanup(raw.close)
        HeartbeatRepository(raw)
        self.assertTrue(heartbeat_table_exists(raw))


class InsertHeartbeatTest(PersistenceTestCase):
    def test_insert_returns_monotonic_row_ids(self) -> None:
        first = self.repository.insert_heartbeat(_record())
        second = self.repository.insert_heartbeat(_record())
        self.assertGreater(second, first)

    def test_insert_persists_exactly_one_row(self) -> None:
        self.repository.insert_heartbeat(_record())
        self.assertEqual(self.repository.count_observations(), 1)

    def test_insert_rejects_naive_received_at(self) -> None:
        with self.assertRaises(ValueError):
            _record(received_at=datetime(2026, 9, 7, 5, 0, 0))


class ReadLatestHeartbeatTest(PersistenceTestCase):
    def test_round_trip_single_heartbeat(self) -> None:
        record = _record()
        self.repository.insert_heartbeat(record)
        restored = self.repository.latest_heartbeat("vds-01")
        assert restored is not None
        self.assertEqual(restored.telemetry, record.telemetry)
        self.assertEqual(restored.received_at, record.received_at)
        self.assertEqual(restored.node, "vds-01")

    def test_latest_heartbeat_unknown_node_is_none(self) -> None:
        self.assertIsNone(self.repository.latest_heartbeat("unknown-node"))

    def test_latest_received_at_unknown_node_is_none(self) -> None:
        self.assertIsNone(
            self.repository.latest_received_at("unknown-node")
        )

    def test_latest_received_at_empty_database_is_none(self) -> None:
        self.assertIsNone(self.repository.latest_received_at("vds-01"))

    def test_latest_lookup_rejects_empty_node(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.latest_heartbeat("")
        with self.assertRaises(ValueError):
            self.repository.latest_received_at("")


class NodeIdentitySeparationTest(PersistenceTestCase):
    def test_nodes_never_mix_telemetry(self) -> None:
        first = _record(host="vds-01", cpu_percent=10.0)
        second = _record(host="vds-02", cpu_percent=90.0)
        self.repository.insert_heartbeat(first)
        self.repository.insert_heartbeat(second)

        latest_first = self.repository.latest_heartbeat("vds-01")
        latest_second = self.repository.latest_heartbeat("vds-02")
        assert latest_first is not None
        assert latest_second is not None
        self.assertEqual(latest_first.node, "vds-01")
        self.assertEqual(latest_second.node, "vds-02")
        self.assertEqual(latest_first.telemetry.cpu_percent, 10.0)
        self.assertEqual(latest_second.telemetry.cpu_percent, 90.0)

    def test_latest_received_at_is_per_node(self) -> None:
        early = datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC)
        late = datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
        self.repository.insert_heartbeat(
            _record(host="vds-01", received_at=early)
        )
        self.repository.insert_heartbeat(
            _record(host="vds-02", received_at=late)
        )
        self.assertEqual(
            self.repository.latest_received_at("vds-01"), early
        )
        self.assertEqual(
            self.repository.latest_received_at("vds-02"), late
        )


class TimestampRoundTripTest(PersistenceTestCase):
    def test_utc_timestamp_round_trip(self) -> None:
        moment = datetime(
            2026, 9, 7, 5, 4, 3, 123456, tzinfo=UTC
        )
        record = _record(
            timestamp=moment,
            received_at=moment + timedelta(seconds=30),
        )
        self.repository.insert_heartbeat(record)
        restored = self.repository.latest_heartbeat("vds-01")
        assert restored is not None
        # Aware datetime equality compares instants.
        self.assertEqual(restored.telemetry.timestamp, moment)
        self.assertEqual(
            restored.received_at, moment + timedelta(seconds=30)
        )
        self.assertIsNotNone(restored.telemetry.timestamp.tzinfo)
        self.assertIsNotNone(restored.received_at.tzinfo)

    def test_non_utc_offset_round_trip_preserves_instant(self) -> None:
        local_moment = datetime(
            2026, 9, 7, 10, 0, 0, tzinfo=_OFFSET_TZ
        )  # 05:00 UTC
        record = _record(
            timestamp=local_moment,
            received_at=local_moment,
        )
        self.repository.insert_heartbeat(record)
        restored = self.repository.latest_heartbeat("vds-01")
        assert restored is not None
        # Canonical UTC serialization preserves the instant and
        # timezone-awareness; offsets are normalized to UTC.
        self.assertEqual(
            restored.telemetry.timestamp, local_moment.astimezone(UTC)
        )
        self.assertEqual(restored.telemetry.timestamp.utcoffset(), UTC.utcoffset(None))
        self.assertIsNotNone(restored.received_at.tzinfo)

    def test_canonical_serialization_is_deterministic(self) -> None:
        from hermes_sentinel.persistence.repository import (
            serialize_timestamp,
        )

        value = datetime(2026, 9, 7, 10, 0, 0, tzinfo=_OFFSET_TZ)
        self.assertEqual(
            serialize_timestamp(value), "2026-09-07T05:00:00+00:00"
        )
        self.assertEqual(
            serialize_timestamp(value), serialize_timestamp(value)
        )

    def test_serialize_rejects_naive_datetime(self) -> None:
        from hermes_sentinel.persistence.repository import (
            serialize_timestamp,
        )

        with self.assertRaises(ValueError):
            serialize_timestamp(datetime(2026, 9, 7, 5, 0, 0))


class TelemetryRoundTripTest(PersistenceTestCase):
    def test_mandatory_telemetry_round_trips_without_loss(self) -> None:
        record = HeartbeatRecord(
            telemetry=HostTelemetry(
                host="vds-01",
                timestamp=datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC),
                uptime_seconds=98765.25,
                load=LoadAverage(one=1.25, five=2.5, fifteen=3.75),
                cpu_percent=42.5,
                ram=ResourceUsage(
                    used=3.0, total=4.0, percent=75.0
                ),
                swap=ResourceUsage(
                    used=1.5, total=3.0, percent=50.0
                ),
                root_filesystem=ResourceUsage(
                    used=30.0, total=100.0, percent=30.0
                ),
                root_inodes=ResourceUsage(
                    used=5000.0, total=50000.0, percent=10.0
                ),
            ),
            received_at=datetime(2026, 9, 7, 5, 0, 1, tzinfo=UTC),
        )
        self.repository.insert_heartbeat(record)
        restored = self.repository.latest_heartbeat("vds-01")
        assert restored is not None
        self.assertEqual(restored.telemetry.host, "vds-01")
        self.assertEqual(restored.telemetry.uptime_seconds, 98765.25)
        self.assertEqual(restored.telemetry.load.one, 1.25)
        self.assertEqual(restored.telemetry.load.five, 2.5)
        self.assertEqual(restored.telemetry.load.fifteen, 3.75)
        self.assertEqual(restored.telemetry.cpu_percent, 42.5)
        self.assertEqual(restored.telemetry.ram.used, 3.0)
        self.assertEqual(restored.telemetry.ram.total, 4.0)
        self.assertEqual(restored.telemetry.ram.percent, 75.0)
        self.assertEqual(restored.telemetry.swap.used, 1.5)
        self.assertEqual(restored.telemetry.swap.total, 3.0)
        self.assertEqual(restored.telemetry.swap.percent, 50.0)
        self.assertEqual(restored.telemetry.root_filesystem.used, 30.0)
        self.assertEqual(restored.telemetry.root_filesystem.total, 100.0)
        self.assertEqual(restored.telemetry.root_filesystem.percent, 30.0)
        self.assertEqual(restored.telemetry.root_inodes.used, 5000.0)
        self.assertEqual(restored.telemetry.root_inodes.total, 50000.0)
        self.assertEqual(restored.telemetry.root_inodes.percent, 10.0)

    def test_absent_swap_resource_round_trips(self) -> None:
        record = _record(
            swap=ResourceUsage(used=0.0, total=0.0, percent=0.0)
        )
        self.repository.insert_heartbeat(record)
        restored = self.repository.latest_heartbeat("vds-01")
        assert restored is not None
        self.assertEqual(restored.telemetry.swap.total, 0.0)


class DeterministicLatestSelectionTest(PersistenceTestCase):
    def test_latest_is_the_newest_received_heartbeat(self) -> None:
        moments = [
            datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC),
        ]
        for moment in moments:
            self.repository.insert_heartbeat(
                _record(timestamp=moment, received_at=moment)
            )
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(
            latest.received_at, datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
        )

    def test_tie_on_received_at_resolves_to_last_inserted_row(self) -> None:
        tie = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
        self.repository.insert_heartbeat(
            _record(cpu_percent=10.0, received_at=tie)
        )
        self.repository.insert_heartbeat(
            _record(cpu_percent=20.0, received_at=tie)
        )
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry.cpu_percent, 20.0)

    def test_latest_received_at_matches_latest_heartbeat(self) -> None:
        early = datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC)
        late = datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
        self.repository.insert_heartbeat(
            _record(timestamp=early, received_at=early)
        )
        self.repository.insert_heartbeat(
            _record(timestamp=late, received_at=late)
        )
        self.assertEqual(self.repository.latest_received_at("vds-01"), late)

    def test_out_of_order_insertion_is_ordered_by_timestamp(self) -> None:
        """Insert order must not matter: received_at defines latest."""
        late = datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
        early = datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC)
        self.repository.insert_heartbeat(
            _record(timestamp=late, received_at=late)
        )
        self.repository.insert_heartbeat(
            _record(timestamp=early, received_at=early)
        )
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.received_at, late)


class SchemaFailClosedTest(PersistenceTestCase):
    """Fail-closed bootstrap: incompatible or future-version databases
    are rejected explicitly at initialization time, never silently
    accepted and never modified."""

    def _path(self, name: str) -> Path:
        return Path(self._tmp.name) / name

    def _raw_connect(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path(name))
        self.addCleanup(connection.close)
        return connection

    def test_incompatible_table_with_version_1_is_rejected(self) -> None:
        """Defect 1 A: wrong heartbeat_observation + user_version=1."""
        raw = self._raw_connect("incompatible-v1.sqlite3")
        raw.execute(
            "CREATE TABLE heartbeat_observation ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " node TEXT NOT NULL)"
        )
        raw.execute("PRAGMA user_version = 1")
        raw.commit()

        path = self._path("incompatible-v1.sqlite3")
        with self.assertRaises(SchemaCompatibilityError):
            initialize_schema(raw)
        with self.assertRaises(SchemaCompatibilityError):
            connect(path)

        # Failure is not masked: version and table layout untouched,
        # no canonical schema was written.
        after = sqlite3.connect(path)
        self.addCleanup(after.close)
        self.assertEqual(schema_version(after), 1)
        columns = after.execute(
            "PRAGMA table_info(heartbeat_observation)"
        ).fetchall()
        self.assertEqual([row[1] for row in columns], ["id", "node"])

    def test_canonical_table_with_extra_column_is_rejected(self) -> None:
        """Extra column breaks the canonical contract: fail-closed."""
        path = self._path("extra-column.sqlite3")
        connection = connect(path)
        self.addCleanup(connection.close)
        connection.execute(
            "ALTER TABLE heartbeat_observation ADD COLUMN extra REAL"
        )
        connection.commit()
        connection.close()

        with self.assertRaises(SchemaCompatibilityError):
            connect(path)
        after = sqlite3.connect(path)
        self.addCleanup(after.close)
        self.assertEqual(schema_version(after), SCHEMA_VERSION)

    def test_future_version_is_rejected_without_downgrade(self) -> None:
        """Defect 1 B: user_version > 1 fails explicitly, no writes."""
        raw = self._raw_connect("future-version.sqlite3")
        raw.execute("PRAGMA user_version = 2")
        raw.commit()

        path = self._path("future-version.sqlite3")
        with self.assertRaises(UnsupportedSchemaVersionError):
            initialize_schema(raw)
        with self.assertRaises(UnsupportedSchemaVersionError):
            connect(path)

        after = sqlite3.connect(path)
        self.addCleanup(after.close)
        self.assertEqual(schema_version(after), 2)
        self.assertFalse(heartbeat_table_exists(after))

    def test_version_1_with_missing_table_is_rejected(self) -> None:
        raw = self._raw_connect("missing-table.sqlite3")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()

        with self.assertRaises(SchemaCompatibilityError):
            connect(self._path("missing-table.sqlite3"))

    def test_version_1_with_missing_index_is_rejected(self) -> None:
        """A v1 database without the canonical index is not ours."""
        path = self._path("missing-index.sqlite3")
        connection = connect(path)
        connection.execute(f"DROP INDEX idx_heartbeat_node_received")
        connection.commit()
        connection.close()

        with self.assertRaises(SchemaCompatibilityError):
            connect(path)

    def test_unversioned_incompatible_table_is_rejected(self) -> None:
        """Defect 1 D: pre-existing unversioned table needs proof."""
        raw = self._raw_connect("unversioned-bad.sqlite3")
        raw.execute(
            "CREATE TABLE heartbeat_observation (id INTEGER PRIMARY KEY)"
        )
        raw.commit()

        path = self._path("unversioned-bad.sqlite3")
        with self.assertRaises(SchemaCompatibilityError):
            connect(path)

        # Not masked: version stays 0, nothing accepted.
        after = sqlite3.connect(path)
        self.addCleanup(after.close)
        self.assertEqual(schema_version(after), 0)

    def test_unversioned_canonical_table_is_accepted_after_validation(
        self,
    ) -> None:
        """Defect 1 D: exact-compatible unversioned table is accepted,
        the missing index is created non-destructively, data is kept."""
        path = self._path("unversioned-good.sqlite3")
        connection = connect(path)
        repository = HeartbeatRepository(connection)
        repository.insert_heartbeat(_record())
        connection.commit()
        connection.execute("DROP INDEX idx_heartbeat_node_received")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()

        reopened = connect(path)  # must not raise
        self.addCleanup(reopened.close)
        self.assertEqual(schema_version(reopened), SCHEMA_VERSION)
        repository = HeartbeatRepository(reopened)
        self.assertIsNotNone(repository.latest_heartbeat("vds-01"))
        repository.insert_heartbeat(_record())
        self.assertEqual(repository.count_observations(), 2)

    def test_unversioned_with_canonical_index_is_accepted(self) -> None:
        path = self._path("unversioned-indexed.sqlite3")
        connection = connect(path)
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()

        reopened = connect(path)
        self.addCleanup(reopened.close)
        self.assertEqual(schema_version(reopened), SCHEMA_VERSION)

    def test_repeated_bootstrap_preserves_data_and_version(self) -> None:
        """Defect 1 C: repeated canonical bootstrap stays green."""
        self.repository.insert_heartbeat(_record())
        initialize_schema(self.connection)
        initialize_schema(self.connection)
        self.assertEqual(schema_version(self.connection), SCHEMA_VERSION)
        self.assertEqual(self.repository.count_observations(), 1)
        self.assertIsNotNone(self.repository.latest_heartbeat("vds-01"))


class NodeIdentityValidationTest(PersistenceTestCase):
    """Defect 2: whitespace-only node identities are rejected at the
    persistence boundary; names are never normalized or case-folded."""

    def test_whitespace_only_node_is_rejected_at_record_level(self) -> None:
        with self.assertRaises(ValueError):
            _record(host="   ")
        with self.assertRaises(ValueError):
            _record(host="\t\n ")

    def test_whitespace_only_node_is_rejected_on_lookup(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.latest_heartbeat("   ")
        with self.assertRaises(ValueError):
            self.repository.latest_received_at("\t")

    def test_normal_node_name_remains_valid_and_verbatim(self) -> None:
        record = _record(host="Prod-Node-01")
        self.repository.insert_heartbeat(record)
        latest = self.repository.latest_heartbeat("Prod-Node-01")
        assert latest is not None
        self.assertEqual(latest.node, "Prod-Node-01")

    def test_node_names_are_not_case_folded(self) -> None:
        """"Prod" and "prod" are distinct identities, never mixed."""
        self.repository.insert_heartbeat(
            _record(host="Prod", cpu_percent=10.0)
        )
        self.repository.insert_heartbeat(
            _record(host="prod", cpu_percent=90.0)
        )
        latest_prod = self.repository.latest_heartbeat("Prod")
        latest_lower = self.repository.latest_heartbeat("prod")
        assert latest_prod is not None
        assert latest_lower is not None
        self.assertEqual(latest_prod.telemetry.cpu_percent, 10.0)
        self.assertEqual(latest_lower.telemetry.cpu_percent, 90.0)
        self.assertIsNone(self.repository.latest_heartbeat("PROD"))


class DatetimeAwarenessValidationTest(PersistenceTestCase):
    """Defect: tzinfo is not None is NOT sufficient for awareness.

    A datetime with a non-None tzinfo whose utcoffset() returns None
    is effectively naive; astimezone would reinterpret it as local
    time and silently change the instant. The persistence boundary
    must reject it fail-closed.
    """

    def _null_offset_moment(self) -> datetime:
        return datetime(2026, 9, 7, 5, 0, 0, tzinfo=_NullOffsetTZ())

    def test_received_at_with_null_offset_tzinfo_is_rejected(self) -> None:
        """A: HeartbeatRecord must raise ValueError before any write."""
        moment = self._null_offset_moment()
        self.assertIsNotNone(moment.tzinfo)  # precondition of the defect
        self.assertIsNone(moment.utcoffset())
        with self.assertRaises(ValueError):
            HeartbeatRecord(telemetry=_telemetry(), received_at=moment)

    def test_serialize_timestamp_rejects_null_offset_tzinfo(self) -> None:
        """B: serialization path is fail-closed too."""
        from hermes_sentinel.persistence.repository import (
            serialize_timestamp,
        )

        with self.assertRaises(ValueError):
            serialize_timestamp(self._null_offset_moment())

    def test_reported_at_with_null_offset_tzinfo_is_rejected(
        self,
    ) -> None:
        """B: reported_at is guarded even when Stage A accepts it.

        HostTelemetry's own check only tests tzinfo is not None, so a
        payload with a null-offset timestamp constructs fine; the
        persistence boundary (HeartbeatRecord) must still fail-closed
        before anything reaches SQLite.
        """
        bad_telemetry = HostTelemetry(
            host="vds-01",
            timestamp=self._null_offset_moment(),
            uptime_seconds=3600.0,
            load=LoadAverage(one=0.1, five=0.2, fifteen=0.3),
            cpu_percent=12.5,
            ram=ResourceUsage(used=1.0, total=2.0, percent=50.0),
            swap=ResourceUsage(used=0.0, total=0.0, percent=0.0),
            root_filesystem=ResourceUsage(
                used=10.0, total=40.0, percent=25.0
            ),
            root_inodes=ResourceUsage(
                used=100.0, total=1000.0, percent=10.0
            ),
        )  # Stage A accepted it: no ValueError here
        self.assertIsNotNone(bad_telemetry.timestamp.tzinfo)
        with self.assertRaises(ValueError):
            HeartbeatRecord(
                telemetry=bad_telemetry,
                received_at=datetime(2026, 9, 7, 5, 0, 1, tzinfo=UTC),
            )
        # Nothing was written to the database.
        self.assertEqual(self.repository.count_observations(), 0)

    def test_parse_timestamp_still_restores_utc_and_offsets(self) -> None:
        """C: ordinary timestamp strings keep round-tripping."""
        from hermes_sentinel.persistence.repository import (
            parse_timestamp,
        )

        utc_value = parse_timestamp("2026-09-07T05:00:00+00:00")
        self.assertEqual(
            utc_value, datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
        )
        self.assertIsNotNone(utc_value.utcoffset())

        offset_value = parse_timestamp("2026-09-07T10:00:00+05:00")
        self.assertEqual(
            offset_value.utcoffset(), timedelta(hours=5)
        )
        self.assertEqual(
            offset_value, datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
        )

    def test_parse_timestamp_rejects_naive_string(self) -> None:
        from hermes_sentinel.persistence.repository import (
            parse_timestamp,
        )

        with self.assertRaises(ValueError):
            parse_timestamp("2026-09-07T05:00:00")

    def test_valid_aware_datetimes_are_not_broken(self) -> None:
        """D: timezone.utc and fixed non-zero offsets stay valid."""
        from hermes_sentinel.persistence.repository import (
            serialize_timestamp,
        )

        utc_moment = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
        self.assertEqual(
            serialize_timestamp(utc_moment), "2026-09-07T05:00:00+00:00"
        )
        offset_moment = datetime(2026, 9, 7, 10, 0, 0, tzinfo=_OFFSET_TZ)
        self.assertEqual(
            serialize_timestamp(offset_moment), "2026-09-07T05:00:00+00:00"
        )
        # And they still persist via the full record path.
        record = _record(
            timestamp=offset_moment, received_at=offset_moment
        )
        self.repository.insert_heartbeat(record)
        restored = self.repository.latest_heartbeat("vds-01")
        assert restored is not None
        self.assertEqual(
            restored.telemetry.timestamp, offset_moment.astimezone(UTC)
        )
        self.assertEqual(restored.received_at, offset_moment.astimezone(UTC))


if __name__ == "__main__":
    unittest.main()
