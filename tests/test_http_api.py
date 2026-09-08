"""Deterministic tests for the Stage B4 heartbeat HTTP request adapter.

Each test uses its own temporary SQLite database in a temporary
directory: no shared mutable test databases. The B2 central clock is
always injected (fixed moments), so no test depends on wall time.

All tokens are synthetic test values — never real secrets.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
from hermes_sentinel.http_api import (  # noqa: E402
    HEARTBEAT_METHOD,
    HEARTBEAT_PATH,
    MAX_HEARTBEAT_BODY_BYTES,
    HeartbeatHttpAdapter,
    HttpRequest,
    HttpResponse,
)
from hermes_sentinel.ingestion import (  # noqa: E402
    Clock,
    HeartbeatIngestor,
)
from hermes_sentinel.persistence import (  # noqa: E402
    HeartbeatRecord,
    HeartbeatRepository,
    connect,
)
from hermes_sentinel.wire import (  # noqa: E402
    AuthenticatedHeartbeatAdapter,
    NodeCredentials,
)

# Two independent time axes: the server-reported moment (inside the
# wire payload) and the central receive moment (B2 injected clock).
_REPORTED_AT = datetime(2026, 9, 7, 4, 0, 0, tzinfo=UTC)
_REPORTED_AT_TEXT = "2026-09-07T04:00:00+00:00"
_RECEIVED_AT = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)
_RECEIVED_AT_LATER = datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)

# Synthetic per-node tokens (test values only).
_TOKEN_ONE = "synthetic-token-vds-01"
_TOKEN_TWO = "synthetic-token-vds-02"
_UNKNOWN_TOKEN = "synthetic-token-unknown"

_JSON_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Type", "application/json"),
    ("X-Sentinel-Token", _TOKEN_ONE),
)


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


def _payload_bytes(**overrides: object) -> bytes:
    return json.dumps(_payload(**overrides)).encode("utf-8")


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


class HttpTestCase(unittest.TestCase):
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
        self.adapter = self._adapter_with(_ClockSequence(_RECEIVED_AT))

    def _adapter_with(
        self,
        clock: Clock,
        credentials: NodeCredentials | None = None,
        repository: HeartbeatRepository | None = None,
    ) -> HeartbeatHttpAdapter:
        ingestor = HeartbeatIngestor(
            _config(),
            repository if repository is not None else self.repository,
            clock=clock,
        )
        wire = AuthenticatedHeartbeatAdapter(
            _config(),
            credentials if credentials is not None else _credentials(),
            ingestor,
        )
        return HeartbeatHttpAdapter(wire)

    def _row_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM heartbeat_observation"
        ).fetchone()
        return int(row[0])

    def _request(
        self,
        method: str = "POST",
        path: str = HEARTBEAT_PATH,
        headers: tuple[tuple[str, str], ...] | None = None,
        body: bytes | None = None,
    ) -> HttpRequest:
        if headers is None:
            headers = _JSON_HEADERS
        if body is None:
            body = _payload_bytes()
        return HttpRequest(
            method=method, path=path, headers=headers, body=body
        )

    def _post(
        self,
        method: str = "POST",
        path: str = HEARTBEAT_PATH,
        headers: tuple[tuple[str, str], ...] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        return self.adapter.handle(
            self._request(
                method=method, path=path, headers=headers, body=body
            )
        )


# --------------------------------------------------------------------------
# SUCCESS
# --------------------------------------------------------------------------


class SuccessTest(HttpTestCase):
    def test_valid_post_returns_204_empty_body_and_persists(self) -> None:
        """(1) Valid POST => 204, empty body, one persistence row."""
        response = self._post()
        self.assertEqual(response.status, 204)
        self.assertEqual(response.body, b"")
        self.assertIn(("Content-Length", "0"), response.headers)
        self.assertEqual(self._row_count(), 1)

    def test_content_type_media_type_case_insensitive(self) -> None:
        """(2) "APPLICATION/JSON" is accepted."""
        for value in ("APPLICATION/JSON", "Application/Json"):
            with self.subTest(value=value):
                response = self._post(
                    headers=(
                        ("Content-Type", value),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                )
                self.assertEqual(response.status, 204)
        self.assertEqual(self._row_count(), 2)

    def test_content_type_charset_utf8_accepted(self) -> None:
        """(3) application/json; charset=utf-8 (any case) accepted."""
        for value in (
            "application/json; charset=utf-8",
            "application/json; charset=UTF-8",
            "APPLICATION/JSON; Charset=Utf-8",
        ):
            with self.subTest(value=value):
                response = self._post(
                    headers=(
                        ("Content-Type", value),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                )
                self.assertEqual(response.status, 204)
        self.assertEqual(self._row_count(), 3)

    def test_token_header_name_case_insensitive(self) -> None:
        """(4) x-sentinel-token in any case carries the token."""
        for name in (
            "x-sentinel-token",
            "X-SENTINEL-TOKEN",
            "X-Sentinel-Token",
        ):
            with self.subTest(name=name):
                response = self._post(
                    headers=(
                        ("Content-Type", "application/json"),
                        (name, _TOKEN_ONE),
                    )
                )
                self.assertEqual(response.status, 204)
        self.assertEqual(self._row_count(), 3)

    def test_full_mapping_survives_http_json_into_persistence(self) -> None:
        """(5) Full Mapping -> B4 JSON -> B3 -> B2 -> B1 unchanged."""
        self._post()
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.telemetry, _expected_telemetry())
        self.assertEqual(latest.received_at, _RECEIVED_AT)


# --------------------------------------------------------------------------
# ROUTE / METHOD
# --------------------------------------------------------------------------


class RouteMethodTest(HttpTestCase):
    def test_wrong_path_404(self) -> None:
        """(6) Any other path => 404, empty body, no row."""
        for path in ("/", "/v1/heart", "/v2/heartbeat", "/heartbeat"):
            with self.subTest(path=path):
                response = self._post(path=path)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_trailing_slash_404(self) -> None:
        """(7) "/v1/heartbeat/" => 404 (no trailing-slash tolerance)."""
        response = self._post(path="/v1/heartbeat/")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_query_string_target_404(self) -> None:
        """(8) Query/percent-encoded targets => 404 (no normalization)."""
        for path in ("/v1/heartbeat?x=1", "/v1%2Fheartbeat", "/v1/Heartbeat"):
            with self.subTest(path=path):
                response = self._post(path=path)
                self.assertEqual(response.status, 404)
        self.assertEqual(self._row_count(), 0)

    def test_wrong_method_405_allow_post(self) -> None:
        """(9) Wrong method (case-sensitive) => 405 + Allow: POST."""
        for method in ("GET", "PUT", "DELETE", "PATCH", "post"):
            with self.subTest(method=method):
                response = self._post(method=method)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.body, b"")
                self.assertIn(("Allow", HEARTBEAT_METHOD), response.headers)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# BODY LIMIT
# --------------------------------------------------------------------------


class BodyLimitTest(HttpTestCase):
    def test_empty_body_400(self) -> None:
        """(10) Empty body => 400 (not 413, not success)."""
        response = self._post(body=b"")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_body_exactly_limit_not_rejected_for_size(self) -> None:
        """(11) Exactly 16384 bytes never gets 413 for size alone.

        A valid JSON payload padded with spaces to exactly the limit
        succeeds; an all-spaces body of the same size fails later as
        malformed JSON (400), never as 413.
        """
        valid = _payload_bytes()
        padded = valid + b" " * (MAX_HEARTBEAT_BODY_BYTES - len(valid))
        self.assertEqual(len(padded), MAX_HEARTBEAT_BODY_BYTES)
        response = self._post(body=padded)
        self.assertEqual(response.status, 204)
        self.assertEqual(self._row_count(), 1)

        response = self._post(body=b" " * MAX_HEARTBEAT_BODY_BYTES)
        self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 1)

    def test_body_over_limit_413(self) -> None:
        """(12) 16385 bytes => 413, no row."""
        valid = _payload_bytes()
        oversized = valid + b" " * (
            MAX_HEARTBEAT_BODY_BYTES + 1 - len(valid)
        )
        self.assertEqual(len(oversized), MAX_HEARTBEAT_BODY_BYTES + 1)
        response = self._post(body=oversized)
        self.assertEqual(response.status, 413)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# CONTENT TYPE
# --------------------------------------------------------------------------


class ContentTypeTest(HttpTestCase):
    def test_missing_content_type_415(self) -> None:
        """(13) No Content-Type header => 415."""
        response = self._post(
            headers=(("X-Sentinel-Token", _TOKEN_ONE),)
        )
        self.assertEqual(response.status, 415)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_unsupported_content_type_415(self) -> None:
        """(14) Wrong media types / parameters => 415, never parsed."""
        for value in (
            "text/plain",
            "text/json",
            "application/xml",
            "application/vnd.api+json",
            "application/*+json",
            "application/json; charset=utf-16",
            'application/json; charset="utf-8"',
            "application/json; boundary=x",
        ):
            with self.subTest(value=value):
                response = self._post(
                    headers=(
                        ("Content-Type", value),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                )
                self.assertEqual(response.status, 415)
        self.assertEqual(self._row_count(), 0)

    def test_latin1_charset_415(self) -> None:
        """(15) charset=latin-1 => 415."""
        response = self._post(
            headers=(
                ("Content-Type", "application/json; charset=latin-1"),
                ("X-Sentinel-Token", _TOKEN_ONE),
            )
        )
        self.assertEqual(response.status, 415)
        self.assertEqual(self._row_count(), 0)

    def test_duplicate_content_type_400(self) -> None:
        """(16) Duplicate Content-Type (any case) => 400 fail closed."""
        for headers in (
            (
                ("Content-Type", "application/json"),
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", _TOKEN_ONE),
            ),
            (
                ("Content-Type", "application/json"),
                ("content-type", "application/json"),
                ("X-Sentinel-Token", _TOKEN_ONE),
            ),
        ):
            with self.subTest(headers=headers):
                response = self._post(headers=headers)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_malformed_content_type_400(self) -> None:
        """(17) Syntactically malformed Content-Type => 400."""
        for value in (
            "application/json;",
            "application/json; charset",
            "application/json; charset=",
            "application/json; =utf-8",
            ";charset=utf-8",
            "application / json",
            "applicationjson",
            "application/json; charset=utf-8; charset=utf-8",
        ):
            with self.subTest(value=value):
                response = self._post(
                    headers=(
                        ("Content-Type", value),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                )
                self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# TOKEN HEADER
# --------------------------------------------------------------------------


class TokenHeaderTest(HttpTestCase):
    def test_missing_token_401(self) -> None:
        """(18) No token header => 401, no row."""
        response = self._post(
            headers=(("Content-Type", "application/json"),)
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_duplicate_token_header_401(self) -> None:
        """(19) Duplicate token header (case-insensitive) => 401."""
        response = self._post(
            headers=(
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", _TOKEN_ONE),
                ("x-sentinel-token", _TOKEN_ONE),
            )
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_wrong_token_401(self) -> None:
        """(20) Wrong token => 401, no row."""
        response = self._post(
            headers=(
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", _UNKNOWN_TOKEN),
            )
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_cross_node_token_401(self) -> None:
        """(21) Node A payload + node B token => 401, no row."""
        for node, token in (
            ("vds-01", _TOKEN_TWO),
            ("vds-02", _TOKEN_ONE),
        ):
            with self.subTest(node=node):
                response = self._post(
                    headers=(
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token", token),
                    ),
                    body=_payload_bytes(node=node),
                )
                self.assertEqual(response.status, 401)
        self.assertEqual(self._row_count(), 0)

    def test_token_value_forwarded_verbatim(self) -> None:
        """(22) The token reaches B3 verbatim — no strip/normalization.

        A secret with significant surrounding whitespace authenticates
        only when presented byte-exact; the stripped variant fails.
        """
        adapter = self._adapter_with(
            _ClockSequence(_RECEIVED_AT),
            credentials=_credentials(
                {"vds-01": " synthetic ", "vds-02": _TOKEN_TWO}
            ),
        )
        verbatim = adapter.handle(
            self._request(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", " synthetic "),
                )
            )
        )
        self.assertEqual(verbatim.status, 204)
        self.assertEqual(self._row_count(), 1)

        stripped = adapter.handle(
            self._request(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", "synthetic"),
                )
            )
        )
        self.assertEqual(stripped.status, 401)
        self.assertEqual(self._row_count(), 1)


# --------------------------------------------------------------------------
# STRICT JSON
# --------------------------------------------------------------------------


class JsonStrictnessTest(HttpTestCase):
    def test_invalid_utf8_400(self) -> None:
        """(23) Invalid UTF-8 bytes => 400, no row."""
        response = self._post(body=b'{"node": "\xff\xfe"}')
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_malformed_json_400(self) -> None:
        """(24) Malformed JSON => 400."""
        for body in (b"{", b'{"node": ', b"}", b"", b"{'x': 1}"):
            with self.subTest(body=body):
                response = self._post(body=body)
                self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 0)

    def test_non_object_root_400(self) -> None:
        """(25) JSON array/scalar/null roots => 400."""
        for body in (
            b"[1, 2]",
            b'"text"',
            b"42",
            b"null",
            b"true",
            b"[]",
        ):
            with self.subTest(body=body):
                response = self._post(body=body)
                self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 0)

    def test_duplicate_top_level_key_400(self) -> None:
        """(26) Duplicate top-level JSON key => 400."""
        body = b'{"node": "vds-01", "node": "vds-01"}'
        response = self._post(body=body)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_duplicate_nested_key_400(self) -> None:
        """(27) Duplicate key in a nested object => 400."""
        body = (
            b'{"node": "vds-01", "load": {"one": 1, "one": 2,'
            b' "five": 3, "fifteen": 4}}'
        )
        response = self._post(body=body)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_json_nan_400(self) -> None:
        """(28) NaN constant => 400."""
        response = self._post(body=b'{"cpu_percent": NaN}')
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_json_infinity_400(self) -> None:
        """(29) Infinity constant => 400."""
        response = self._post(body=b'{"cpu_percent": Infinity}')
        self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 0)

    def test_json_negative_infinity_400(self) -> None:
        """(30) -Infinity constant => 400."""
        response = self._post(body=b'{"cpu_percent": -Infinity}')
        self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# B3 ERROR MAPPING
# --------------------------------------------------------------------------


class B3ErrorMappingTest(HttpTestCase):
    def test_valid_json_but_malformed_b3_payload_400(self) -> None:
        """(31) Syntactically valid JSON violating the B3 wire schema
        (extra/missing field, wrong scalar type) => 400, no row."""
        invalid_payloads = (
            _payload(extra_field="x"),
            {k: v for k, v in _payload().items() if k != "cpu_percent"},
            _payload(cpu_percent="12.5"),
            _payload(load={"one": 1, "five": 2}),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self._post(body=json.dumps(payload).encode())
                self.assertEqual(response.status, 400)
                self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_unknown_node_generic_401(self) -> None:
        """(32) Unknown node with any token => generic 401, no row."""
        response = self._post(body=_payload_bytes(node="stranger"))
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# SIDE EFFECTS
# --------------------------------------------------------------------------


class SideEffectsTest(HttpTestCase):
    def test_http_level_failures_never_touch_clock_or_persistence(
        self,
    ) -> None:
        """(33) Route/method/size/content-type/token-header failures:
        B2 clock never called, zero persistence rows."""
        adapter = self._adapter_with(_ExplodingClock())
        scenarios = [
            (self._request(path="/nope"), 404),
            (self._request(method="GET"), 405),
            (self._request(body=b"x" * (MAX_HEARTBEAT_BODY_BYTES + 1)), 413),
            (self._request(body=b""), 400),
            (
                self._request(
                    headers=(("X-Sentinel-Token", _TOKEN_ONE),)
                ),
                415,
            ),
            (
                self._request(
                    headers=(
                        ("Content-Type", "application/json"),
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                ),
                400,
            ),
            (
                self._request(
                    headers=(("Content-Type", "application/json"),)
                ),
                401,
            ),
        ]
        for request, expected_status in scenarios:
            with self.subTest(expected_status=expected_status):
                # The exploding clock raises AssertionError if the
                # B2 path is ever reached; here it must NOT raise.
                response = adapter.handle(request)
                self.assertEqual(response.status, expected_status)
        self.assertEqual(self._row_count(), 0)

    def test_json_failures_zero_persistence_rows(self) -> None:
        """(34) JSON/UTF-8 failures: clock never called, zero rows."""
        adapter = self._adapter_with(_ExplodingClock())
        for body in (
            b'{"node": "\xff"}',
            b"{",
            b"[1]",
            b'{"node": "a", "node": "a"}',
            b'{"x": NaN}',
        ):
            with self.subTest(body=body):
                response = adapter.handle(self._request(body=body))
                self.assertEqual(response.status, 400)
        self.assertEqual(self._row_count(), 0)

    def test_b3_auth_failure_zero_persistence_rows(self) -> None:
        """(35) B3 authentication failure: clock never called, no row."""
        adapter = self._adapter_with(_ExplodingClock())
        response = adapter.handle(
            self._request(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", _UNKNOWN_TOKEN),
                )
            )
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(self._row_count(), 0)

    def test_success_exactly_one_observation(self) -> None:
        """(36) One successful request => exactly one observation."""
        self._post()
        self.assertEqual(self._row_count(), 1)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.node, "vds-01")

    def test_repeated_success_separate_observations(self) -> None:
        """(37) Repeated success => separate observations."""
        adapter = self._adapter_with(
            _ClockSequence(_RECEIVED_AT, _RECEIVED_AT_LATER)
        )
        for _ in range(2):
            response = adapter.handle(self._request())
            self.assertEqual(response.status, 204)
        self.assertEqual(self._row_count(), 2)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.received_at, _RECEIVED_AT_LATER)


# --------------------------------------------------------------------------
# INTERNAL FAILURE
# --------------------------------------------------------------------------


class InternalFailureTest(HttpTestCase):
    def test_repository_failure_propagates_unmapped(self) -> None:
        """(38) A B1 unexpected exception after a valid request
        propagates; it is never masked as 204/400/401."""
        adapter = self._adapter_with(
            _ClockSequence(_RECEIVED_AT),
            repository=_FailingRepository(self.connection),
        )
        with self.assertRaises(sqlite3.OperationalError):
            adapter.handle(self._request())
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# STRUCTURAL MALFORMATION (request abstraction)
# --------------------------------------------------------------------------


class StructuralMalformedTest(HttpTestCase):
    def test_non_string_method_400(self) -> None:
        request = HttpRequest(
            method=cast(Any, 123),
            path=HEARTBEAT_PATH,
            headers=_JSON_HEADERS,
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_non_string_path_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=cast(Any, None),
            headers=_JSON_HEADERS,
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_non_bytes_body_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=_JSON_HEADERS,
            body=cast(Any, _payload_bytes().decode("utf-8")),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_headers_not_a_tuple_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=cast(Any, list(_JSON_HEADERS)),
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_header_pair_not_a_tuple_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=(
                ("Content-Type", "application/json"),
                cast(Any, ["a", "b"]),
            ),
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_header_pair_wrong_arity_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=(
                ("Content-Type", "application/json"),
                cast(Any, ("a", "b", "c")),
            ),
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_non_string_header_name_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=(("Content-Type", "application/json"), (cast(Any, 1), "x")),
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_non_string_header_value_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=(
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", cast(Any, 12345)),
            ),
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_empty_header_name_400(self) -> None:
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=(("Content-Type", "application/json"), ("", "x")),
            body=_payload_bytes(),
        )
        self.assertEqual(self.adapter.handle(request).status, 400)

    def test_structural_failures_create_no_rows(self) -> None:
        """Malformed structure never reaches persistence."""
        for request in (
            HttpRequest(
                method=cast(Any, 123),
                path=HEARTBEAT_PATH,
                headers=_JSON_HEADERS,
                body=_payload_bytes(),
            ),
            HttpRequest(
                method="POST",
                path=HEARTBEAT_PATH,
                headers=_JSON_HEADERS,
                body=cast(Any, "not-bytes"),
            ),
        ):
            self.adapter.handle(request)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# SECRET SAFETY
# --------------------------------------------------------------------------


class SecretSafetyTest(HttpTestCase):
    def _failure_scenarios(self) -> list[tuple[HttpRequest, int]]:
        return [
            (self._request(path="/nope"), 404),
            (self._request(method="GET"), 405),
            (self._request(body=b"x" * (MAX_HEARTBEAT_BODY_BYTES + 1)), 413),
            (self._request(body=b""), 400),
            (self._request(headers=(("X-Sentinel-Token", _TOKEN_ONE),)), 415),
            (
                self._request(
                    headers=(
                        ("Content-Type", "text/plain"),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                ),
                415,
            ),
            (
                self._request(
                    headers=(
                        ("Content-Type", "application/json"),
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token", _TOKEN_ONE),
                    )
                ),
                400,
            ),
            (
                self._request(
                    headers=(("Content-Type", "application/json"),)
                ),
                401,
            ),
            (
                self._request(
                    headers=(
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token", _UNKNOWN_TOKEN),
                    )
                ),
                401,
            ),
            (self._request(body=b'{"node": "\xff"}'), 400),
            (self._request(body=b"{"), 400),
            (self._request(body=b"[1]"), 400),
            (self._request(body=b'{"a": 1, "a": 2}'), 400),
            (self._request(body=b'{"x": NaN}'), 400),
            (self._request(body=_payload_bytes(extra="x")), 400),
            (self._request(body=_payload_bytes(node="stranger")), 401),
            (
                self._request(
                    headers=(
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token", "tok\r\nX-Injected: yes"),
                    )
                ),
                400,
            ),
            (
                self._request(
                    headers=(
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token\x00", _TOKEN_ONE),
                    )
                ),
                400,
            ),
        ]

    def test_all_client_failures_have_empty_bodies_and_length_zero(
        self,
    ) -> None:
        """(40) Every failure response: empty body + Content-Length: 0
        and the deterministic expected status."""
        for request, expected_status in self._failure_scenarios():
            with self.subTest(expected_status=expected_status):
                response = self.adapter.handle(request)
                self.assertEqual(response.status, expected_status)
                self.assertEqual(response.body, b"")
                self.assertIn(("Content-Length", "0"), response.headers)

    def test_response_headers_never_contain_token(self) -> None:
        """(41) No response header name/value carries token material."""
        for request, _ in self._failure_scenarios():
            response = self.adapter.handle(request)
            for name, value in response.headers:
                self.assertNotIn(_TOKEN_ONE, name)
                self.assertNotIn(_TOKEN_ONE, value)
        # The success response is equally clean.
        response = self._post()
        for name, value in response.headers:
            self.assertNotIn(_TOKEN_ONE, name)
            self.assertNotIn(_TOKEN_ONE, value)

    def test_b3_auth_error_text_not_reflected(self) -> None:
        """(42) The B3 auth exception text is never copied into the
        response body or headers."""
        response = self._post(
            headers=(
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", _UNKNOWN_TOKEN),
            )
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, b"")
        self.assertNotIn(b"authentication", response.body)
        for name, value in response.headers:
            self.assertNotIn("authentication", name)
            self.assertNotIn("authentication", value)

    def test_request_repr_never_contains_token_or_header_values(
        self,
    ) -> None:
        """(39) HttpRequest repr: no token, no header values, no body
        contents, total on hostile shapes."""
        request = self._request()
        representation = repr(request)
        self.assertIsInstance(representation, str)
        self.assertNotIn(_TOKEN_ONE, representation)
        self.assertNotIn("application/json", representation)

        # A hostile/invalid structure must not make repr() fail and
        # must not leak values either.
        hostile = HttpRequest(
            method=cast(Any, 123),
            path=cast(Any, None),
            headers=cast(
                Any,
                [("X-Sentinel-Token", _TOKEN_ONE), "not-a-pair"],
            ),
            body=cast(Any, "not-bytes"),
        )
        representation = repr(hostile)
        self.assertNotIn(_TOKEN_ONE, representation)

    def test_crlf_injected_header_fail_closed_without_reflection(
        self,
    ) -> None:
        """(43) CR/LF-injected header names/values => 400 with no
        secret reflection anywhere in the response."""
        injected = [
            (
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", f"{_TOKEN_ONE}\r\nX-Injected: 1"),
            ),
            (
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", f"{_TOKEN_ONE}\nEvil: 2"),
            ),
            (
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token\r\nSet-Cookie: x=1", _TOKEN_ONE),
            ),
            (
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", f"{_TOKEN_ONE}\x00"),
            ),
        ]
        for headers in injected:
            with self.subTest(headers=headers):
                response = self._post(headers=headers)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.body, b"")
                for name, value in response.headers:
                    self.assertNotIn(_TOKEN_ONE, name)
                    self.assertNotIn(_TOKEN_ONE, value)
# --------------------------------------------------------------------------
# DEEP JSON RECURSION (client 400, bounded parsing boundary)
# --------------------------------------------------------------------------

#: Deterministic, calculable nesting depth: well below the 16 KiB
#: body limit (2 bytes per level => 10000 bytes) and far beyond the
#: stdlib json parser recursion budget in canonical Python 3.11
#: (default recursion limit 1000; the C scanner recurses per level
#: and honours the limit). No massive loops, no resource abuse.
_DEEP_JSON_DEPTH = 5000


class DeepJsonRecursionTest(HttpTestCase):
    """Pathologically deep client JSON is a client 400, not an
    internal failure — and the remediation does not blur the
    internal-failure boundary."""

    def _deep_body(self) -> bytes:
        body = b"[" * _DEEP_JSON_DEPTH + b"]" * _DEEP_JSON_DEPTH
        self.assertLessEqual(len(body), MAX_HEARTBEAT_BODY_BYTES)
        return body

    def test_deep_body_fits_limit_and_reproducibly_recurses_out(
        self,
    ) -> None:
        """(A precondition) The bounded deep body really triggers
        RecursionError in the canonical stdlib json parser."""
        with self.assertRaises(RecursionError):
            json.loads(self._deep_body().decode("utf-8"))

    def test_deeply_nested_json_is_client_400_without_side_effects(
        self,
    ) -> None:
        """(A) Deep JSON within the body limit => 400, empty body,
        Content-Length: 0; the B2 clock is never called, no row is
        written and RecursionError never escapes the adapter."""
        adapter = self._adapter_with(_ExplodingClock())
        response = adapter.handle(self._request(body=self._deep_body()))
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertIn(("Content-Length", "0"), response.headers)
        self.assertEqual(self._row_count(), 0)

    def test_normal_malformed_json_still_400_after_recursion_fix(
        self,
    ) -> None:
        """(B) Ordinary malformed JSON remains a plain client 400."""
        response = self._post(body=b'{"node": ')
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(self._row_count(), 0)

    def test_repository_failure_after_valid_json_still_propagates(
        self,
    ) -> None:
        """(C) The RecursionError remediation does not blur the
        internal-failure boundary: a repository exception after a
        VALID request still propagates (never 204/400/401)."""
        adapter = self._adapter_with(
            _ClockSequence(_RECEIVED_AT),
            repository=_FailingRepository(self.connection),
        )
        with self.assertRaises(sqlite3.OperationalError):
            adapter.handle(self._request())
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# HOSTILE REPR SAFETY (constant, total, no caller-controlled repr)
# --------------------------------------------------------------------------


class _HostileReprObject:
    """Caller-controlled object whose __repr__ raises and records
    whether it was ever invoked."""

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.repr_called = False

    def __repr__(self) -> str:
        self.repr_called = True
        raise RuntimeError(self.marker)


class _HostileReprStr(str):
    """Hostile str subclass whose __repr__ raises and records the
    invocation (proves even str-subclass repr is never called)."""

    marker: str
    repr_called: bool

    def __new__(cls, marker: str) -> "_HostileReprStr":
        instance = super().__new__(cls, "hostile")
        instance.marker = marker
        instance.repr_called = False
        return instance

    def __repr__(self) -> str:
        self.repr_called = True
        raise RuntimeError(self.marker)


class HostileReprTest(HttpTestCase):
    """repr(HttpRequest) must be total and secret-safe: it never
    invokes caller-controlled __repr__ and never reflects markers,
    token or body contents. The assertions check runtime behaviour
    (no raise, no call, no leak), not an implementation constant."""

    def _assert_total_safe_repr(
        self, request: HttpRequest, *hostile: Any
    ) -> None:
        representation = repr(request)  # must not raise
        self.assertIsInstance(representation, str)
        for obj in hostile:
            self.assertFalse(
                obj.repr_called,
                "caller-controlled __repr__ must never be invoked",
            )
            self.assertNotIn(obj.marker, representation)

    def test_hostile_method_repr_never_invoked(self) -> None:
        """(D) Hostile method __repr__ -> RuntimeError(marker):
        repr(request) neither raises nor reflects the marker."""
        hostile = _HostileReprObject("METHOD_REPR_SECRET")
        request = HttpRequest(
            method=cast(Any, hostile),
            path=HEARTBEAT_PATH,
            headers=_JSON_HEADERS,
            body=_payload_bytes(),
        )
        self._assert_total_safe_repr(request, hostile)

    def test_hostile_path_repr_never_invoked(self) -> None:
        """(E) Hostile path __repr__: repr(request) is total, marker
        absent."""
        hostile = _HostileReprObject("PATH_REPR_SECRET")
        request = HttpRequest(
            method="POST",
            path=cast(Any, hostile),
            headers=_JSON_HEADERS,
            body=_payload_bytes(),
        )
        self._assert_total_safe_repr(request, hostile)

    def test_hostile_header_name_and_value_repr_never_invoked(
        self,
    ) -> None:
        """(F) Hostile header name/value __repr__ (including a str
        subclass): repr(request) is total; no marker, no header
        values (token included) are disclosed."""
        hostile_name = _HostileReprObject("HEADER_NAME_REPR_SECRET")
        hostile_value = _HostileReprStr("HEADER_VALUE_REPR_SECRET")
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=(
                ("Content-Type", "application/json"),
                (cast(Any, hostile_name), cast(Any, hostile_value)),
                ("X-Sentinel-Token", _TOKEN_ONE),
            ),
            body=_payload_bytes(),
        )
        representation = repr(request)  # must not raise
        self.assertIsInstance(representation, str)
        self.assertFalse(hostile_name.repr_called)
        self.assertFalse(hostile_value.repr_called)
        self.assertNotIn("HEADER_NAME_REPR_SECRET", representation)
        self.assertNotIn("HEADER_VALUE_REPR_SECRET", representation)
        self.assertNotIn(_TOKEN_ONE, representation)

    def test_hostile_body_repr_never_invoked(self) -> None:
        """(G) Hostile non-bytes body __repr__: repr(request) is
        total and the body marker is absent."""
        hostile = _HostileReprObject("BODY_REPR_SECRET")
        request = HttpRequest(
            method="POST",
            path=HEARTBEAT_PATH,
            headers=_JSON_HEADERS,
            body=cast(Any, hostile),
        )
        self._assert_total_safe_repr(request, hostile)

    def test_valid_request_repr_hides_token_and_body_contents(
        self,
    ) -> None:
        """(H) A normal valid request with a unique token marker and
        a body content marker: neither appears in repr(request)."""
        token_marker = "UNIQUE_TOKEN_MARKER_9f1c2e"
        body_marker = "BODY_CONTENT_MARKER_77aa31"
        request = self._request(
            headers=(
                ("Content-Type", "application/json"),
                ("X-Sentinel-Token", token_marker),
            ),
            body=b'{"body_marker": "' + body_marker.encode() + b'"}',
        )
        representation = repr(request)
        self.assertIsInstance(representation, str)
        self.assertNotIn(token_marker, representation)
        self.assertNotIn(body_marker, representation)


if __name__ == "__main__":
    unittest.main()
