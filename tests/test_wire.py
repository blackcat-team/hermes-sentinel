"""Deterministic tests for the Stage B3 authenticated heartbeat wire.

Each test uses its own temporary SQLite database in a temporary
directory: no shared mutable test databases. The B2 central clock is
always injected (fixed moments), so no test depends on wall time.

All tokens are synthetic test values — never real secrets.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import traceback
import unittest
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    UnknownNodeError,
)
from hermes_sentinel.persistence import (  # noqa: E402
    HeartbeatRecord,
    HeartbeatRepository,
    connect,
)
from hermes_sentinel.wire import (  # noqa: E402
    AuthenticatedHeartbeatAdapter,
    DuplicateNodeTokenError,
    HeartbeatAuthenticationError,
    HeartbeatWireError,
    MalformedHeartbeatPayloadError,
    NodeCredentials,
    decode_heartbeat_payload,
)

# Two independent time axes: the server-reported moment (inside the
# wire payload) and the central receive moment (B2 injected clock).
_REPORTED_AT = datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC)
_REPORTED_AT_TEXT = "2026-09-07T04:00:00+00:00"
_RECEIVED_AT = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)

# Synthetic per-node tokens (test values only).
_TOKEN_ONE = "synthetic-token-vds-01"
_TOKEN_TWO = "synthetic-token-vds-02"
_TOKEN_PROD = "synthetic-token-prod"
_TOKEN_VPN1 = "synthetic-token-vpn1"
_UNKNOWN_TOKEN = "synthetic-token-unknown"


def _heartbeat() -> HeartbeatSettings:
    return HeartbeatSettings(
        expected_interval_seconds=30.0, stale_after_seconds=90.0
    )


def _external() -> ExternalCheckSettings:
    return ExternalCheckSettings(tcp_host="203.0.113.10", tcp_port=22)


def _host(name: str) -> HostConfig:
    return HostConfig(name=name, heartbeat=_heartbeat(), external=_external())


def _config() -> SentinelConfig:
    return SentinelConfig(hosts=(_host("vds-01"), _host("vds-02")))


def _credentials(
    tokens: dict[str, str] | None = None,
) -> NodeCredentials:
    if tokens is None:
        tokens = {"vds-01": _TOKEN_ONE, "vds-02": _TOKEN_TWO}
    return NodeCredentials(tokens)


def _payload(**overrides: object) -> dict[str, object]:
    """Build a valid external wire payload with optional overrides."""
    defaults: dict[str, object] = {
        "node": "vds-01",
        "reported_at": _REPORTED_AT_TEXT,
        "uptime_seconds": 3600.5,
        "load": {"one": 0.1, "five": 0.2, "fifteen": 0.3},
        "cpu_percent": 12.5,
        "ram": {"used": 1.0, "total": 2.0, "percent": 50.0},
        "swap": {"used": 0.0, "total": 0.0, "percent": 0.0},
        "root_fs": {"used": 10.0, "total": 40.0, "percent": 25.0},
        "root_inodes": {"used": 100.0, "total": 1000.0, "percent": 10.0},
    }
    defaults.update(overrides)
    return defaults


def _expected_telemetry(node: str = "vds-01") -> HostTelemetry:
    return HostTelemetry(
        host=node,
        timestamp=_REPORTED_AT,
        uptime_seconds=3600.5,
        load=LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        cpu_percent=12.5,
        ram=ResourceUsage(used=1.0, total=2.0, percent=50.0),
        swap=ResourceUsage(used=0.0, total=0.0, percent=0.0),
        root_filesystem=ResourceUsage(used=10.0, total=40.0, percent=25.0),
        root_inodes=ResourceUsage(used=100.0, total=1000.0, percent=10.0),
    )


class _ClockSequence:
    """Injectable central clock returning fixed moments in order."""

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


# --- hostile payload mappings (node/token TOCTOU regression) ------------


class _ShiftingNodeMapping(Mapping):
    """Hostile Mapping: the value of "node" changes after the first read.

    First read returns ``first_node``; every later read returns
    ``later_node``. Without a stable snapshot this can authenticate as
    one node and persist a heartbeat for another.
    """

    def __init__(
        self, base: dict[str, object], first_node: str, later_node: str
    ) -> None:
        self._base = dict(base)
        self._first_node = first_node
        self._later_node = later_node
        self.node_reads = 0

    def __getitem__(self, key: str) -> object:
        if key == "node":
            self.node_reads += 1
            if self.node_reads == 1:
                return self._first_node
            return self._later_node
        return self._base[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)


class _SelfMutatingMapping(Mapping):
    """Hostile Mapping: reading ``trigger_key`` rewrites the stored node."""

    def __init__(
        self, base: dict[str, object], trigger_key: str, new_node: str
    ) -> None:
        self._base = dict(base)
        self._trigger_key = trigger_key
        self._new_node = new_node
        self.mutations = 0

    def __getitem__(self, key: str) -> object:
        if key == self._trigger_key:
            self.mutations += 1
            self._base["node"] = self._new_node
        return self._base[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)


class _ExplodingItemsMapping(Mapping):
    """Hostile Mapping whose items() raises: the snapshot must fail
    closed (deterministic MalformedHeartbeatPayloadError, no clock,
    no write) with the hostile exception fully severed from the
    boundary error's object graph."""

    def __init__(
        self, base: dict[str, object], message: str = "hostile items()"
    ) -> None:
        self._base = dict(base)
        self._message = message

    def __getitem__(self, key: str) -> object:
        return self._base[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)

    def items(self) -> Any:
        raise RuntimeError(self._message)


class WireTestCase(unittest.TestCase):
    """Base: one temporary directory, database and adapter per test.

    Default configuration: nodes "vds-01" and "vds-02", each with its
    own synthetic token; the B2 clock is a fixed injected moment.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sentinel.sqlite3"
        self.connection = connect(self.db_path)
        self.addCleanup(self.connection.close)
        self.repository = HeartbeatRepository(self.connection)
        self.ingestor = HeartbeatIngestor(
            _config(), self.repository, clock=_ClockSequence(_RECEIVED_AT)
        )
        self.adapter = AuthenticatedHeartbeatAdapter(
            _config(), _credentials(), self.ingestor
        )

    def _row_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM heartbeat_observation"
        ).fetchone()
        return int(row[0])


# --------------------------------------------------------------------------
# SUCCESS
# --------------------------------------------------------------------------


class AdapterSuccessTest(WireTestCase):
    def test_valid_node_with_own_token_is_accepted_and_persisted(self) -> None:
        """(1) Configured node + correct own token => receipt + row."""
        receipt = self.adapter.handle(_payload(), _TOKEN_ONE)
        self.assertEqual(receipt.node, "vds-01")
        self.assertEqual(receipt.received_at, _RECEIVED_AT)
        self.assertEqual(self._row_count(), 1)
        self.assertIsNotNone(self.repository.latest_heartbeat("vds-01"))

    def test_full_mandatory_telemetry_survives_to_latest_heartbeat(
        self,
    ) -> None:
        """(2) Mapping -> decoder -> HostTelemetry -> B2 -> B1 -> latest."""
        self.adapter.handle(_payload(), _TOKEN_ONE)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry, _expected_telemetry())
        self.assertEqual(
            latest.telemetry.root_inodes,
            ResourceUsage(used=100.0, total=1000.0, percent=10.0),
        )
        self.assertEqual(
            latest.telemetry.root_filesystem,
            ResourceUsage(used=10.0, total=40.0, percent=25.0),
        )

    def test_reported_at_preserved_and_received_at_from_central_clock(
        self,
    ) -> None:
        """(3) reported_at survives; received_at is the B2 clock moment."""
        self.adapter.handle(_payload(), _TOKEN_ONE)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry.timestamp, _REPORTED_AT)
        self.assertEqual(latest.received_at, _RECEIVED_AT)
        self.assertNotEqual(latest.received_at, latest.telemetry.timestamp)

    def test_two_nodes_with_distinct_tokens_work_independently(self) -> None:
        """(4) Each node authenticates with its own token only."""
        first = self.adapter.handle(_payload(), _TOKEN_ONE)
        second = self.adapter.handle(_payload(node="vds-02"), _TOKEN_TWO)
        self.assertEqual(first.node, "vds-01")
        self.assertEqual(second.node, "vds-02")
        self.assertEqual(self._row_count(), 2)
        latest_one = self.repository.latest_heartbeat("vds-01")
        latest_two = self.repository.latest_heartbeat("vds-02")
        assert latest_one is not None and latest_two is not None
        self.assertEqual(latest_one.telemetry.host, "vds-01")
        self.assertEqual(latest_two.telemetry.host, "vds-02")

    def test_integer_scalars_are_valid_numbers(self) -> None:
        """JSON integers are valid numbers (ints, not bools)."""
        receipt = self.adapter.handle(
            _payload(uptime_seconds=7200, cpu_percent=50), _TOKEN_ONE
        )
        self.assertEqual(receipt.node, "vds-01")
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry.uptime_seconds, 7200.0)
        self.assertEqual(latest.telemetry.cpu_percent, 50.0)

    def test_offset_reported_at_is_accepted_and_preserved(self) -> None:
        """A fixed-offset aware reported_at keeps its instant."""
        self.adapter.handle(
            _payload(reported_at="2026-09-07T09:00:00+05:00"), _TOKEN_ONE
        )
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry.timestamp, _REPORTED_AT)
        self.assertEqual(
            latest.telemetry.timestamp.utcoffset(),
            _REPORTED_AT.utcoffset(),
        )

    def test_verbatim_token_with_significant_whitespace(self) -> None:
        """An exact secret with surrounding spaces is compared verbatim.

        The token is never stripped/normalized: " abc " is a distinct
        secret from "abc".
        """
        adapter = AuthenticatedHeartbeatAdapter(
            _config(),
            _credentials({"vds-01": " synthetic ", "vds-02": _TOKEN_TWO}),
            self.ingestor,
        )
        receipt = adapter.handle(_payload(), " synthetic ")
        self.assertEqual(receipt.node, "vds-01")
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(), "synthetic")
        self.assertEqual(self._row_count(), 1)


# --------------------------------------------------------------------------
# AUTH FAIL-CLOSED
# --------------------------------------------------------------------------


class AuthFailClosedTest(WireTestCase):
    def _assert_generic_auth_failure(
        self, payload: dict[str, object], token: object
    ) -> HeartbeatAuthenticationError:
        with self.assertRaises(HeartbeatAuthenticationError) as caught:
            self.adapter.handle(payload, token)
        # The external auth error is generic and constant.
        self.assertEqual(
            str(caught.exception), "heartbeat authentication failed"
        )
        self.assertNotIsInstance(caught.exception, UnknownNodeError)
        return caught.exception

    def test_wrong_token_fails_closed_with_zero_rows(self) -> None:
        """(5) Wrong token => generic auth failure, no row."""
        self._assert_generic_auth_failure(_payload(), _UNKNOWN_TOKEN)
        self.assertEqual(self._row_count(), 0)
        self.assertIsNone(self.repository.latest_heartbeat("vds-01"))

    def test_node_a_payload_with_node_b_token_fails(self) -> None:
        """(6) Node A payload + Node B token => auth failure, no row."""
        self._assert_generic_auth_failure(_payload(), _TOKEN_TWO)
        self.assertEqual(self._row_count(), 0)
        # The reverse direction fails identically.
        self._assert_generic_auth_failure(_payload(node="vds-02"), _TOKEN_ONE)
        self.assertEqual(self._row_count(), 0)

    def test_unknown_node_is_generic_auth_failure(self) -> None:
        """(7) Unknown node => the same generic external auth failure."""
        self._assert_generic_auth_failure(
            _payload(node="stranger"), _UNKNOWN_TOKEN
        )
        self.assertEqual(self._row_count(), 0)
        self.assertIsNone(self.repository.latest_heartbeat("stranger"))

    def test_configured_node_without_credential_fails_closed(self) -> None:
        """(8) Configured node missing a credential => generic failure."""
        adapter = AuthenticatedHeartbeatAdapter(
            _config(),
            _credentials({"vds-01": _TOKEN_ONE}),  # vds-02: no credential
            self.ingestor,
        )
        with self.assertRaises(HeartbeatAuthenticationError) as caught:
            adapter.handle(_payload(node="vds-02"), _TOKEN_TWO)
        self.assertEqual(
            str(caught.exception), "heartbeat authentication failed"
        )
        self.assertEqual(self._row_count(), 0)

    def test_empty_presented_token_rejected(self) -> None:
        """(9) Empty presented token => auth failure, no row."""
        self._assert_generic_auth_failure(_payload(), "")
        self.assertEqual(self._row_count(), 0)

    def test_whitespace_only_presented_token_rejected(self) -> None:
        """(10) Whitespace-only presented token => auth failure."""
        self._assert_generic_auth_failure(_payload(), "   \t ")
        self.assertEqual(self._row_count(), 0)

    def test_non_string_presented_token_rejected(self) -> None:
        """A non-string presented token fails closed."""
        self._assert_generic_auth_failure(_payload(), 12345)
        self._assert_generic_auth_failure(_payload(), None)
        self.assertEqual(self._row_count(), 0)

    def test_identity_is_case_sensitive(self) -> None:
        """Configured "vds-01" does not authenticate "VDS-01" — verbatim,
        case-sensitive identity, no normalization."""
        self._assert_generic_auth_failure(
            _payload(node="VDS-01"), _TOKEN_ONE
        )
        self.assertEqual(self._row_count(), 0)

    def test_same_length_wrong_token_rejected(self) -> None:
        """(13) Constant-time comparison behaviour: a wrong token of the
        same length as the expected one is still rejected (verified by
        behaviour, not by monkeypatching the comparison primitive)."""
        same_length_wrong = "x" * len(_TOKEN_ONE)
        self.assertNotEqual(same_length_wrong, _TOKEN_ONE)
        self._assert_generic_auth_failure(_payload(), same_length_wrong)
        self.assertEqual(self._row_count(), 0)
        # The exact own token still succeeds afterwards.
        receipt = self.adapter.handle(_payload(), _TOKEN_ONE)
        self.assertEqual(receipt.node, "vds-01")


class NodeCredentialsTest(WireTestCase):
    def test_duplicate_token_assignment_rejected_at_construction(self) -> None:
        """(11) The same token for two nodes is rejected when the
        credential set is built — never silently accepted."""
        with self.assertRaises(DuplicateNodeTokenError) as caught:
            _credentials(
                {
                    "vds-01": "synthetic-shared-secret",
                    "vds-02": "synthetic-shared-secret",
                }
            )
        # The error names the conflicting nodes but never the token.
        message = str(caught.exception)
        self.assertIn("vds-01", message)
        self.assertIn("vds-02", message)
        self.assertNotIn("synthetic-shared-secret", message)

    def test_invalid_credential_values_rejected(self) -> None:
        """Credential construction is fail-closed on invalid shapes."""
        with self.assertRaises(ValueError):
            _credentials({"vds-01": ""})
        with self.assertRaises(ValueError):
            _credentials({"vds-01": "   "})
        with self.assertRaises(ValueError):
            _credentials({"vds-01": 123})  # type: ignore[dict-item]
        with self.assertRaises(ValueError):
            _credentials({"   ": _TOKEN_ONE})
        with self.assertRaises(ValueError):
            _credentials({"": _TOKEN_ONE})
        with self.assertRaises(TypeError):
            _credentials(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_repr_and_messages_never_expose_token_values(self) -> None:
        """(12) repr()/error texts expose node names only, never tokens."""
        credentials = _credentials()
        self.assertNotIn(_TOKEN_ONE, repr(credentials))
        self.assertNotIn(_TOKEN_TWO, repr(credentials))
        self.assertIn("vds-01", repr(credentials))

    def test_token_for_returns_expected_token_per_node(self) -> None:
        credentials = _credentials()
        self.assertEqual(credentials.token_for("vds-01"), _TOKEN_ONE)
        self.assertEqual(credentials.token_for("vds-02"), _TOKEN_TWO)
        self.assertIsNone(credentials.token_for("stranger"))
        # Case-sensitive verbatim identity.
        self.assertIsNone(credentials.token_for("VDS-01"))

    def test_credential_for_unconfigured_node_cannot_authenticate(self) -> None:
        """A credential for an unconfigured node never becomes an
        accepted identity: the adapter checks the SentinelConfig
        configuration first."""
        adapter = AuthenticatedHeartbeatAdapter(
            _config(),
            _credentials(
                {"vds-01": _TOKEN_ONE, "stranger": "synthetic-stranger"}
            ),
            self.ingestor,
        )
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(node="stranger"), "synthetic-stranger")
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# WIRE FAIL-CLOSED (strict decoding)
# --------------------------------------------------------------------------


class WireDecodeFailClosedTest(WireTestCase):
    """Wire decode failures after a successful authentication."""

    def _assert_malformed(self, payload: object) -> None:
        with self.assertRaises(MalformedHeartbeatPayloadError):
            self.adapter.handle(payload, _TOKEN_ONE)  # type: ignore[arg-type]
        self.assertEqual(self._row_count(), 0)

    def test_missing_mandatory_top_level_field(self) -> None:
        """(14) Missing top-level field => malformed, no row."""
        for field in (
            "node",
            "reported_at",
            "uptime_seconds",
            "load",
            "cpu_percent",
            "ram",
            "swap",
            "root_fs",
            "root_inodes",
        ):
            payload = _payload()
            del payload[field]
            self._assert_malformed(payload)

    def test_missing_mandatory_nested_resource_field(self) -> None:
        """(15) Missing nested resource field => malformed, no row."""
        for resource in ("ram", "swap", "root_fs", "root_inodes"):
            payload = _payload()
            nested = dict(payload[resource])  # type: ignore[arg-type]
            del nested["percent"]
            payload[resource] = nested
            self._assert_malformed(payload)
        payload = _payload()
        load = dict(payload["load"])  # type: ignore[arg-type]
        del load["fifteen"]
        payload["load"] = load
        self._assert_malformed(payload)

    def test_extra_top_level_field_rejected(self) -> None:
        """(16) Extra top-level field => malformed (fail-closed policy)."""
        self._assert_malformed(_payload(services=["nginx"]))
        self._assert_malformed(_payload(received_at="2026-09-07T05:00:00Z"))
        self._assert_malformed(_payload(health="healthy"))

    def test_extra_nested_field_rejected(self) -> None:
        """(17) Extra nested telemetry field => malformed."""
        self._assert_malformed(
            _payload(ram={"used": 1.0, "total": 2.0, "percent": 50.0,
                          "free": 1.0})
        )
        self._assert_malformed(
            _payload(load={"one": 0.1, "five": 0.2, "fifteen": 0.3,
                           "extra": 1.0})
        )

    def test_malformed_reported_at_rejected(self) -> None:
        """(18) Malformed reported_at => malformed, no row."""
        self._assert_malformed(_payload(reported_at="not-a-datetime"))
        self._assert_malformed(_payload(reported_at="2026-13-45T99:99:99Z"))
        self._assert_malformed(_payload(reported_at=1725672000))

    def test_naive_reported_at_rejected(self) -> None:
        """(19) Naive reported_at (no offset) => malformed."""
        self._assert_malformed(_payload(reported_at="2026-09-07T04:00:00"))
        self._assert_malformed(_payload(reported_at="2026-09-07"))

    def test_wrong_scalar_type_rejected(self) -> None:
        """(20) Wrong scalar types => malformed."""
        self._assert_malformed(_payload(uptime_seconds="3600.5"))
        self._assert_malformed(_payload(cpu_percent=None))
        self._assert_malformed(_payload(node=42))
        self._assert_malformed(_payload(load=[0.1, 0.2, 0.3]))
        self._assert_malformed(_payload(ram="50%"))
        self._assert_malformed(
            _payload(root_inodes={"used": "100", "total": 1000,
                                  "percent": 10.0})
        )

    def test_numeric_string_rejected(self) -> None:
        """(21) Numeric strings are not silently coerced to numbers."""
        self._assert_malformed(_payload(cpu_percent="12.5"))
        self._assert_malformed(_payload(uptime_seconds="3600"))
        self._assert_malformed(
            _payload(swap={"used": "0", "total": "0", "percent": "0"})
        )

    def test_bool_as_number_rejected(self) -> None:
        """(22) bool is not accepted as a JSON number, even though
        bool is a subclass of int in Python."""
        self._assert_malformed(_payload(cpu_percent=True))
        self._assert_malformed(_payload(uptime_seconds=False))
        self._assert_malformed(
            _payload(ram={"used": True, "total": 2.0, "percent": 50.0})
        )
        self._assert_malformed(_payload(load={"one": True, "five": 0.2,
                                               "fifteen": 0.3}))

    def test_whitespace_only_node_rejected(self) -> None:
        """(23) Whitespace-only node => malformed, no row."""
        self._assert_malformed(_payload(node="   "))
        self._assert_malformed(_payload(node="\t\n"))

    def test_invalid_stage_a_resource_invariant_rejected(self) -> None:
        """(24) Stage A domain invariants are enforced via the typed
        models: used>total, percent out of range, non-zero used with
        total 0 are all malformed."""
        self._assert_malformed(
            _payload(ram={"used": 5.0, "total": 2.0, "percent": 50.0})
        )
        self._assert_malformed(
            _payload(root_fs={"used": 1.0, "total": 40.0, "percent": 150.0})
        )
        self._assert_malformed(
            _payload(swap={"used": 1.0, "total": 0.0, "percent": 0.0})
        )
        self._assert_malformed(_payload(cpu_percent=-1.0))
        self._assert_malformed(_payload(uptime_seconds=-5.0))
        self._assert_malformed(_payload(load={"one": -0.1, "five": 0.2,
                                               "fifteen": 0.3}))

    def test_non_mapping_payload_rejected(self) -> None:
        """A payload that is not a JSON object is malformed (rejected
        before any authentication lookup)."""
        for bad in ([1, 2, 3], "heartbeat", 42, None, 1.5):
            with self.assertRaises(MalformedHeartbeatPayloadError):
                self.adapter.handle(bad, _TOKEN_ONE)  # type: ignore[arg-type]
        self.assertEqual(self._row_count(), 0)

    def test_decode_heartbeat_payload_standalone(self) -> None:
        """The standalone decoder produces the typed HostTelemetry."""
        telemetry = decode_heartbeat_payload(_payload())
        self.assertEqual(telemetry, _expected_telemetry())
        with self.assertRaises(MalformedHeartbeatPayloadError):
            decode_heartbeat_payload(_payload(node="   "))


# --------------------------------------------------------------------------
# ORDER / SIDE EFFECTS
# --------------------------------------------------------------------------


class OrderAndSideEffectsTest(WireTestCase):
    def test_authentication_failure_never_calls_b2_ingestion_clock(
        self,
    ) -> None:
        """(25) Auth failure happens before the B2 central clock."""
        ingestor = HeartbeatIngestor(
            _config(), self.repository, clock=_ExplodingClock()
        )
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), _credentials(), ingestor
        )
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(), _UNKNOWN_TOKEN)
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(node="stranger"), _TOKEN_ONE)
        self.assertEqual(self._row_count(), 0)

    def test_authentication_failure_writes_nothing_to_sqlite(self) -> None:
        """(26) Auth failure: zero rows, no latest heartbeat."""
        for payload, token in (
            (_payload(), _UNKNOWN_TOKEN),
            (_payload(), ""),
            (_payload(), "   "),
            (_payload(node="vds-02"), _TOKEN_ONE),
            (_payload(node="stranger"), _TOKEN_ONE),
        ):
            with self.assertRaises(HeartbeatAuthenticationError):
                self.adapter.handle(payload, token)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repository.count_observations(), 0)
        self.assertIsNone(self.repository.latest_heartbeat("vds-01"))
        self.assertIsNone(self.repository.latest_heartbeat("vds-02"))

    def test_wire_decode_failure_writes_nothing_to_sqlite(self) -> None:
        """(27) Decode failure after successful auth: zero rows."""
        with self.assertRaises(MalformedHeartbeatPayloadError):
            self.adapter.handle(_payload(cpu_percent="12.5"), _TOKEN_ONE)
        with self.assertRaises(MalformedHeartbeatPayloadError):
            self.adapter.handle(
                _payload(ram={"used": 1.0, "total": 2.0}), _TOKEN_ONE
            )
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repository.count_observations(), 0)

    def test_repository_failure_after_successful_auth_is_not_masked(
        self,
    ) -> None:
        """(28) A B1/B2 failure after successful auth propagates; it is
        never turned into a success receipt."""
        failing = _FailingRepository(self.connection)
        ingestor = HeartbeatIngestor(
            _config(), failing, clock=_ClockSequence(_RECEIVED_AT)
        )
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), _credentials(), ingestor
        )
        with self.assertRaises(sqlite3.OperationalError):
            adapter.handle(_payload(), _TOKEN_ONE)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# HOSTILE PAYLOAD MAPPINGS — NODE/TOKEN TOCTOU (Defect 1 regression)
# --------------------------------------------------------------------------


class PayloadSnapshotAdversarialTest(WireTestCase):
    """A hostile mutable mapping must not be able to break the frozen
    one-node-one-own-token binding between authentication and decode."""

    def _prod_vpn_adapter(
        self, clock: object
    ) -> AuthenticatedHeartbeatAdapter:
        config = SentinelConfig(hosts=(_host("Prod"), _host("VPN-1")))
        credentials = _credentials(
            {"Prod": _TOKEN_PROD, "VPN-1": _TOKEN_VPN1}
        )
        ingestor = HeartbeatIngestor(
            config, self.repository, clock=clock  # type: ignore[arg-type]
        )
        return AuthenticatedHeartbeatAdapter(config, credentials, ingestor)

    def test_shifting_node_mapping_cannot_persist_other_node(self) -> None:
        """(A) A mapping whose "node" changes after the first read:
        token-prod must NEVER persist a VPN-1 heartbeat. Acceptable
        outcomes are (1) the snapshot fixes "Prod" and only Prod is
        stored, or (2) malformed input fails closed — zero rows."""
        clock = _ClockSequence(_RECEIVED_AT)
        adapter = self._prod_vpn_adapter(clock)
        hostile = _ShiftingNodeMapping(
            _payload(node="Prod"), first_node="Prod", later_node="VPN-1"
        )
        try:
            receipt = adapter.handle(hostile, _TOKEN_PROD)
        except (HeartbeatAuthenticationError, MalformedHeartbeatPayloadError):
            receipt = None
        if receipt is not None:
            self.assertEqual(receipt.node, "Prod")
            self.assertEqual(self._row_count(), 1)
            self.assertIsNotNone(self.repository.latest_heartbeat("Prod"))
        else:
            self.assertEqual(self._row_count(), 0)
        # FORBIDDEN in every outcome: a persisted VPN-1 row.
        self.assertIsNone(self.repository.latest_heartbeat("VPN-1"))
        stored_nodes = [
            row[0]
            for row in self.connection.execute(
                "SELECT node FROM heartbeat_observation"
            )
        ]
        self.assertNotIn("VPN-1", stored_nodes)
        # The stable snapshot reads the external mapping exactly once.
        self.assertEqual(hostile.node_reads, 1)

    def test_self_mutating_mapping_cannot_change_authenticated_identity(
        self,
    ) -> None:
        """A mapping that rewrites its own "node" while being read:
        the identity captured in the single snapshot pass wins."""
        clock = _ClockSequence(_RECEIVED_AT)
        adapter = self._prod_vpn_adapter(clock)
        hostile = _SelfMutatingMapping(
            _payload(node="Prod"), trigger_key="load", new_node="VPN-1"
        )
        receipt = adapter.handle(hostile, _TOKEN_PROD)
        self.assertEqual(receipt.node, "Prod")
        self.assertEqual(self._row_count(), 1)
        latest = self.repository.latest_heartbeat("Prod")
        assert latest is not None
        self.assertEqual(latest.telemetry.host, "Prod")
        self.assertIsNone(self.repository.latest_heartbeat("VPN-1"))
        # The mutation really fired during the read — after the node
        # had already been snapshotted.
        self.assertGreaterEqual(hostile.mutations, 1)

    def test_caller_dict_mutation_after_handle_cannot_change_identity(
        self,
    ) -> None:
        """(B) Mutating the caller-owned plain dict after handle()
        cannot change the authenticated/persisted identity: the
        adapter retains no reference to the caller's mapping."""
        payload = _payload()  # ordinary caller-owned mutable dict
        receipt = self.adapter.handle(payload, _TOKEN_ONE)
        self.assertEqual(receipt.node, "vds-01")
        payload["node"] = "vds-02"
        payload["cpu_percent"] = 99.9
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry.host, "vds-01")
        self.assertEqual(latest.telemetry.cpu_percent, 12.5)
        self.assertIsNone(self.repository.latest_heartbeat("vds-02"))
        self.assertEqual(self._row_count(), 1)

    def test_auth_failure_with_hostile_mapping_skips_clock_and_sqlite(
        self,
    ) -> None:
        """(C) Auth failure on a hostile mapping: the B2 clock is
        never called and SQLite stays empty."""
        adapter = self._prod_vpn_adapter(_ExplodingClock())
        hostile = _ShiftingNodeMapping(
            _payload(node="Prod"), first_node="Prod", later_node="VPN-1"
        )
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(hostile, _UNKNOWN_TOKEN)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repository.count_observations(), 0)

    def test_auth_failure_when_identity_shifts_to_unknown_node(self) -> None:
        """A shift to an unknown node is still a generic auth failure
        with zero rows and no clock call."""
        clock = _ClockSequence(_RECEIVED_AT)
        adapter = self._prod_vpn_adapter(clock)
        hostile = _ShiftingNodeMapping(
            _payload(node="Prod"), first_node="stranger", later_node="Prod"
        )
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(hostile, _TOKEN_PROD)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self._row_count(), 0)

    def test_snapshot_failure_leaves_persistence_untouched(self) -> None:
        """(D) A mapping that cannot be snapshotted deterministically
        is malformed: no clock call, no write, no exception leak."""
        clock = _ClockSequence(_RECEIVED_AT)
        adapter = self._prod_vpn_adapter(clock)
        hostile = _ExplodingItemsMapping(_payload(node="Prod"))
        with self.assertRaises(MalformedHeartbeatPayloadError) as caught:
            adapter.handle(hostile, _TOKEN_PROD)
        # The hostile exception is suppressed, not chained.
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self._row_count(), 0)

    def test_nested_snapshot_failure_leaves_persistence_untouched(
        self,
    ) -> None:
        """A hostile NESTED mapping fails the snapshot fail-closed
        too: deterministic malformed error, no clock, no write."""
        clock = _ClockSequence(_RECEIVED_AT)
        adapter = self._prod_vpn_adapter(clock)
        payload: dict[str, object] = _payload(node="Prod")
        payload["ram"] = _ExplodingItemsMapping(
            {"used": 1.0, "total": 2.0, "percent": 50.0}
        )
        with self.assertRaises(MalformedHeartbeatPayloadError):
            adapter.handle(payload, _TOKEN_PROD)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# TOTAL TOKEN COMPARISON (Defect 2 regression)
# --------------------------------------------------------------------------


class TokenComparisonRobustnessTest(WireTestCase):
    """Arbitrary str tokens compare exact/verbatim without encoding
    exceptions; auth failures stay generic and secret-safe."""

    def test_lone_surrogate_presented_token_is_generic_auth_failure(
        self,
    ) -> None:
        """(E) A presented token with a lone surrogate never raises
        UnicodeEncodeError: generic auth failure, exact generic
        message, zero rows, B2 clock never called."""
        clock = _ClockSequence(_RECEIVED_AT)
        ingestor = HeartbeatIngestor(_config(), self.repository, clock=clock)
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), _credentials(), ingestor
        )
        presented = "\ud800SurrogateSecret"
        with self.assertRaises(HeartbeatAuthenticationError) as caught:
            adapter.handle(_payload(), presented)
        self.assertEqual(
            str(caught.exception), "heartbeat authentication failed"
        )
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self._row_count(), 0)

    def test_lone_surrogate_auth_failure_is_secret_safe(self) -> None:
        """(F) str/repr/args/__cause__/__context__ of the auth error
        expose neither the presented secret nor a UnicodeEncodeError
        carrying a secret fragment."""
        presented = "\ud800SurrogateSecret"
        with self.assertRaises(HeartbeatAuthenticationError) as caught:
            self.adapter.handle(_payload(), presented)
        exc = caught.exception
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        renderings = (
            str(exc),
            repr(exc),
            str(exc.args),
            repr(exc.args),
            repr(exc.__cause__),
            repr(exc.__context__),
        )
        for rendering in renderings:
            self.assertNotIn(presented, rendering)
            self.assertNotIn("SurrogateSecret", rendering)
            self.assertNotIn("UnicodeEncodeError", rendering)

    def test_surrogate_token_exact_match_succeeds(self) -> None:
        """(G) The same arbitrary str secret (with a surrogate code
        point) on both sides compares exact/verbatim — no encoding
        exception, successful receipt; a codepoint-different
        surrogate secret fails closed."""
        secret = "synthetic-\ud800-secret"
        credentials = _credentials({"vds-01": secret, "vds-02": _TOKEN_TWO})
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), credentials, self.ingestor
        )
        receipt = adapter.handle(_payload(), secret)
        self.assertEqual(receipt.node, "vds-01")
        self.assertEqual(self._row_count(), 1)
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(), "synthetic-\ud801-secret")
        self.assertEqual(self._row_count(), 1)

    def test_significant_whitespace_secret_exact_semantics(self) -> None:
        """(H) Expected " abc ": presented " abc " succeeds; presented
        "abc" is a generic auth failure (no stripping/normalization)."""
        credentials = _credentials({"vds-01": " abc ", "vds-02": _TOKEN_TWO})
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), credentials, self.ingestor
        )
        receipt = adapter.handle(_payload(), " abc ")
        self.assertEqual(receipt.node, "vds-01")
        self.assertEqual(self._row_count(), 1)
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(), "abc")
        self.assertEqual(self._row_count(), 1)

    def test_unicode_normalization_is_not_applied(self) -> None:
        """(I) Visually similar but codepoint-different tokens stay
        different secrets (no NFC/NFD normalization)."""
        composed = "caf\u00e9-secret"
        decomposed = "cafe\u0301-secret"
        self.assertNotEqual(composed, decomposed)
        credentials = _credentials(
            {"vds-01": composed, "vds-02": _TOKEN_TWO}
        )
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), credentials, self.ingestor
        )
        receipt = adapter.handle(_payload(), composed)
        self.assertEqual(receipt.node, "vds-01")
        with self.assertRaises(HeartbeatAuthenticationError):
            adapter.handle(_payload(), decomposed)
        self.assertEqual(self._row_count(), 1)


# --------------------------------------------------------------------------
# SNAPSHOT EXCEPTION CHAIN SANITIZATION (Defect 3 regression)
# --------------------------------------------------------------------------


class SnapshotExceptionChainTest(WireTestCase):
    """A hostile exception raised during the defensive snapshot must be
    fully severed from the sanitized boundary error: __cause__ and
    __context__ are both None, and no hostile type/message text leaks
    into str/repr/args or into traceback formatting.

    ``raise ... from None`` inside an active handler is NOT sufficient
    (it only sets __suppress_context__; the hostile exception object
    stays attached as __context__). The boundary error must be raised
    outside the handler — proven here at runtime.
    """

    _GENERIC_MESSAGE = (
        "payload must be a deterministically readable JSON object"
    )

    def _capture_boundary_error(
        self, payload: object
    ) -> MalformedHeartbeatPayloadError:
        """Run handle() on a hostile payload and return the boundary
        error, asserting zero side effects (clock, SQLite)."""
        clock = _ClockSequence(_RECEIVED_AT)
        ingestor = HeartbeatIngestor(_config(), self.repository, clock=clock)
        adapter = AuthenticatedHeartbeatAdapter(
            _config(), _credentials(), ingestor
        )
        with self.assertRaises(MalformedHeartbeatPayloadError) as caught:
            adapter.handle(payload, _TOKEN_ONE)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repository.count_observations(), 0)
        return caught.exception

    def _assert_sanitized(
        self,
        exc: MalformedHeartbeatPayloadError,
        marker: str,
    ) -> None:
        """The boundary error carries neither the hostile exception
        object nor its type/message text."""
        # The hostile exception object is not chained.
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        # No hostile type/message text in any rendering of the error.
        for rendering in (
            str(exc),
            repr(exc),
            str(exc.args),
            repr(exc.args),
            str(exc.args[0]) if exc.args else "",
        ):
            self.assertNotIn(marker, rendering)
            self.assertNotIn("RuntimeError", rendering)
        # The message is the deterministic generic boundary text.
        self.assertEqual(str(exc), self._GENERIC_MESSAGE)

    def test_top_level_snapshot_exception_is_fully_sanitized(self) -> None:
        """(A) items() raises RuntimeError('TOP_LEVEL_SECRET_MARKER'):
        sanitized MalformedHeartbeatPayloadError, no marker/type text,
        __cause__/__context__ None, no clock call, zero rows."""
        payload = _ExplodingItemsMapping(
            _payload(), message="TOP_LEVEL_SECRET_MARKER"
        )
        exc = self._capture_boundary_error(payload)
        self._assert_sanitized(exc, "TOP_LEVEL_SECRET_MARKER")

    def test_nested_snapshot_exception_is_fully_sanitized(self) -> None:
        """(B) Nested ram/items() raises
        RuntimeError('NESTED_SECRET_MARKER'): the same sanitized
        boundary error, __cause__/__context__ None, no clock, zero
        rows."""
        payload: dict[str, object] = _payload()
        payload["ram"] = _ExplodingItemsMapping(
            {"used": 1.0, "total": 2.0, "percent": 50.0},
            message="NESTED_SECRET_MARKER",
        )
        exc = self._capture_boundary_error(payload)
        self._assert_sanitized(exc, "NESTED_SECRET_MARKER")

    def test_traceback_formatting_contains_no_hostile_markers(self) -> None:
        """(C) stdlib traceback formatting of the boundary error does
        not contain the hostile secret markers — the hostile exception
        never enters the traceback chain."""
        top_level: object = _ExplodingItemsMapping(
            _payload(), message="TOP_LEVEL_SECRET_MARKER"
        )
        nested: dict[str, object] = _payload()
        nested["ram"] = _ExplodingItemsMapping(
            {"used": 1.0, "total": 2.0, "percent": 50.0},
            message="NESTED_SECRET_MARKER",
        )
        for payload, marker in (
            (top_level, "TOP_LEVEL_SECRET_MARKER"),
            (nested, "NESTED_SECRET_MARKER"),
        ):
            exc = self._capture_boundary_error(payload)
            formatted = "".join(traceback.format_exception(exc))
            self.assertNotIn(marker, formatted)
            self.assertNotIn("RuntimeError", formatted)


# --------------------------------------------------------------------------
# SECRET SAFETY
# --------------------------------------------------------------------------


class SecretSafetyTest(WireTestCase):
    def test_auth_error_message_and_repr_expose_no_secrets(self) -> None:
        """(12) The external auth error never discloses expected or
        presented token material."""
        presented = "synthetic-wrong-presented-token"
        with self.assertRaises(HeartbeatAuthenticationError) as caught:
            self.adapter.handle(_payload(), presented)
        error = caught.exception
        for secret in (_TOKEN_ONE, _TOKEN_TWO, presented):
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, repr(error))
            self.assertNotIn(secret, error.args[0])

    def test_wire_errors_form_a_coherent_hierarchy(self) -> None:
        """Malformed payload and authentication failure are the two
        distinguishable application-level wire failures."""
        self.assertTrue(
            issubclass(MalformedHeartbeatPayloadError, HeartbeatWireError)
        )
        self.assertTrue(
            issubclass(HeartbeatAuthenticationError, HeartbeatWireError)
        )

    def test_no_token_reaches_persistence_or_receipt(self) -> None:
        """Tokens are never persisted: the stored row carries telemetry
        and timestamps only (the wire payload contains no token field,
        and the token never enters HostTelemetry/receipt)."""
        receipt = self.adapter.handle(_payload(), _TOKEN_ONE)
        for container in (
            repr(receipt),
            str(receipt.received_at),
        ):
            self.assertNotIn(_TOKEN_ONE, container)
        cursor = self.connection.execute(
            "SELECT node, reported_at, received_at FROM heartbeat_observation"
        )
        for row in cursor.fetchall():
            for value in row:
                self.assertNotIn(_TOKEN_ONE, str(value))
        telemetry = self.repository.latest_heartbeat("vds-01")
        assert telemetry is not None
        self.assertNotIn(_TOKEN_ONE, repr(telemetry))


if __name__ == "__main__":
    unittest.main()
