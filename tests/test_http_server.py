"""Deterministic tests for the Stage B5 heartbeat HTTP server bridge.

Each test runs a real ``HTTPServer`` on an ephemeral loopback port
(127.0.0.1) and speaks raw HTTP/1.1 over a real TCP socket — the
stdlib parsing path is exercised end-to-end, no mocks on the wire.
Each test uses its own temporary SQLite database; the B2 central
clock is always injected (fixed moments), so no test depends on
wall time.

The SQLite connection is opened with ``check_same_thread=False``
because the server serves on its own thread; usage stays strictly
sequential — a response is only read after the persistence work
that produced it has finished. Client-side socket timeouts protect
the test runner only; the product server adds no timeout contract.

All tokens are synthetic test values — never real secrets.
"""

from __future__ import annotations

import io
import json
import sqlite3
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime
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
from hermes_sentinel.http_api import (  # noqa: E402
    HEARTBEAT_METHOD,
    HEARTBEAT_PATH,
    MAX_HEARTBEAT_BODY_BYTES,
    HeartbeatHttpAdapter,
)
from hermes_sentinel.http_server import (  # noqa: E402
    create_heartbeat_http_server,
)
from hermes_sentinel.ingestion import (  # noqa: E402
    Clock,
    HeartbeatIngestor,
)
from hermes_sentinel.persistence import (  # noqa: E402
    HeartbeatRecord,
    HeartbeatRepository,
    initialize_schema,
)
from hermes_sentinel.wire import (  # noqa: E402
    AuthenticatedHeartbeatAdapter,
    NodeCredentials,
)

# Two independent time axes: the server-reported moment (inside the
# wire payload) and the central receive moment (B2 injected clock).
_REPORTED_AT_TEXT = "2026-09-07T04:00:00+00:00"
_RECEIVED_AT = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)

# Synthetic per-node tokens (test values only).
_TOKEN_ONE = "synthetic-token-vds-01"
_TOKEN_TWO = "synthetic-token-vds-02"
_UNKNOWN_TOKEN = "synthetic-token-unknown"

_DEFAULT_HEADERS: tuple[tuple[str, str], ...] = (
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


def _credentials() -> NodeCredentials:
    return NodeCredentials({"vds-01": _TOKEN_ONE, "vds-02": _TOKEN_TWO})


def _payload_bytes(**overrides: object) -> bytes:
    """A valid external wire payload (JSON bytes) with overrides."""
    payload: dict[str, object] = {
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
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class _ClockSequence:
    """Injectable central clock returning fixed moments in order."""

    def __init__(self, *moments: datetime) -> None:
        self._moments = list(moments)

    def __call__(self) -> datetime:
        if len(self._moments) > 1:
            return self._moments.pop(0)
        return self._moments[0]


class _FailingRepository(HeartbeatRepository):
    """Repository whose insert always fails (persistence failure)."""

    def insert_heartbeat(self, record: HeartbeatRecord) -> int:
        raise sqlite3.OperationalError("simulated persistence failure")


class _MarkerFailingRepository(HeartbeatRepository):
    """Repository failing with a unique secret marker (log-leak
    proof): the marker must never reach stderr/logging."""

    def __init__(self, connection: sqlite3.Connection, marker: str) -> None:
        super().__init__(connection)
        self._marker = marker

    def insert_heartbeat(self, record: HeartbeatRecord) -> int:
        raise sqlite3.OperationalError(
            f"simulated failure {self._marker}"
        )


def _read_all(client: socket.socket) -> bytes:
    """Read from ``client`` until EOF; return everything received.

    A platform RST (server closed with unread client bytes left in
    flight) is tolerated: whatever arrived first is the response.
    """
    chunks: list[bytes] = []
    while True:
        try:
            data = client.recv(65536)
        except ConnectionResetError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _parse_response(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    """Parse one full response (read to EOF) from raw bytes.

    Pure test-side byte parsing (no http.client, no second
    connection): the raw bytes were already received from the
    server; this only splits status line / headers / body.
    """
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise AssertionError(f"malformed response head: {raw[:200]!r}")
    lines = head.decode("ascii").split("\r\n")
    status_line = lines[0]
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise AssertionError(f"unexpected status line: {status_line!r}")
    status = int(parts[1])
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon:
            raise AssertionError(f"malformed header line: {line!r}")
        headers.append((name, value.strip()))
    return status, headers, body


class HttpServerTestCase(unittest.TestCase):
    """Base: one temporary database, one real loopback server per test.

    Default configuration: nodes "vds-01" and "vds-02", each with
    its own synthetic token; the B2 clock is a fixed injected
    moment. The server serves on a background thread on an
    ephemeral port; cleanup performs shutdown() + server_close() +
    thread join — no leaked listener/thread/socket.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sentinel.sqlite3"
        # check_same_thread=False: the server serves on its own
        # thread, but usage is strictly sequential (a response is
        # only read after the persistence work has finished).
        self.connection = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        try:
            initialize_schema(self.connection)
        except BaseException:
            self.connection.close()
            raise
        self.addCleanup(self.connection.close)
        self.repository = HeartbeatRepository(self.connection)
        self.server = self._start_server()

    def _start_server(
        self,
        *,
        clock: Clock | None = None,
        repository: HeartbeatRepository | None = None,
    ) -> object:
        """Start the heartbeat server (ephemeral port) + cleanup."""
        ingestor = HeartbeatIngestor(
            _config(),
            repository if repository is not None else self.repository,
            clock=clock if clock is not None
            else _ClockSequence(_RECEIVED_AT),
        )
        wire = AuthenticatedHeartbeatAdapter(
            _config(), _credentials(), ingestor
        )
        server = create_heartbeat_http_server(
            HeartbeatHttpAdapter(wire), "127.0.0.1", 0
        )
        self.server_address = server.server_address
        serve_thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        serve_thread.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            serve_thread.join(timeout=10.0)

        self.addCleanup(_cleanup)
        return server

    def _row_count(self) -> int:
        return self.repository.count_observations()

    def _request_bytes(
        self,
        *,
        method: str = HEARTBEAT_METHOD,
        target: str = HEARTBEAT_PATH,
        headers: tuple[tuple[str, str], ...] | None = None,
        body: bytes | None = None,
        content_length: int | str | None = None,
        include_content_length: bool = True,
        extra_lines: tuple[bytes, ...] = (),
    ) -> bytes:
        """Build one raw HTTP/1.1 request (head + body).

        ``content_length`` overrides the emitted Content-Length
        header value (defaults to the real body length); with
        ``include_content_length=False`` the header is omitted.
        ``extra_lines`` appends raw header lines verbatim.
        """
        if body is None:
            body = _payload_bytes()
        lines = [f"{method} {target} HTTP/1.1"]
        if include_content_length:
            value: int | str = (
                len(body) if content_length is None else content_length
            )
            lines.append(f"Content-Length: {value}")
        for name, value_header in (
            headers if headers is not None else _DEFAULT_HEADERS
        ):
            lines.append(f"{name}: {value_header}")
        head = ("\r\n".join(lines) + "\r\n").encode("utf-8")
        if extra_lines:
            head += b"".join(line + b"\r\n" for line in extra_lines)
        return head + b"\r\n" + body

    def _exchange(self, raw: bytes, *, timeout: float = 10.0) -> bytes:
        """Send raw bytes, read the response until EOF, return it.

        Reading to EOF is deterministic because the server always
        closes the connection after exactly one response.
        """
        with socket.create_connection(
            self.server_address, timeout=timeout
        ) as client:
            client.sendall(raw)
            return _read_all(client)

    def _exchange_parsed(
        self, raw: bytes, *, timeout: float = 10.0
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        return _parse_response(self._exchange(raw, timeout=timeout))

    def _assert_empty_response(
        self,
        parsed: tuple[int, list[tuple[str, str]], bytes],
        expected_status: int,
    ) -> None:
        """Every response: exact status, empty body, CL: 0, close."""
        status, headers, body = parsed
        self.assertEqual(status, expected_status)
        self.assertEqual(body, b"")
        self.assertIn(("Content-Length", "0"), headers)
        self.assertIn(("Connection", "close"), headers)


# --------------------------------------------------------------------------
# SUCCESS
# --------------------------------------------------------------------------


class SuccessTest(HttpServerTestCase):
    def test_valid_post_204_and_persisted(self) -> None:
        """(1) Valid POST -> 204, Content-Length 0, SQLite row 1."""
        status, headers, body = self._exchange_parsed(
            self._request_bytes()
        )
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertIn(("Content-Length", "0"), headers)
        self.assertEqual(self._row_count(), 1)
        latest = self.repository.latest_heartbeat("vds-01")
        assert latest is not None
        self.assertEqual(latest.node, "vds-01")
        self.assertEqual(latest.received_at, _RECEIVED_AT)

    def test_repeated_post_row_2(self) -> None:
        """(2) Repeated valid POST -> SQLite row 2."""
        for _ in range(2):
            status, _, _ = self._exchange_parsed(self._request_bytes())
            self.assertEqual(status, 204)
        self.assertEqual(self._row_count(), 2)


# --------------------------------------------------------------------------
# ROUTING (B4 owns 404/405; body never read)
# --------------------------------------------------------------------------


class RoutingTest(HttpServerTestCase):
    def test_wrong_path_404(self) -> None:
        """(3) Wrong paths -> 404 (B4 semantics, verbatim target)."""
        for target in ("/", "/v1/heartbeat/", "/v1/heartbeat?x=1"):
            with self.subTest(target=target):
                parsed = self._exchange_parsed(
                    self._request_bytes(target=target, body=b"")
                )
                self._assert_empty_response(parsed, 404)
        self.assertEqual(self._row_count(), 0)

    def test_get_exact_405_allow_post(self) -> None:
        """(4) GET exact path -> 405 + Allow: POST."""
        status, headers, body = self._exchange_parsed(
            self._request_bytes(method="GET", body=b"")
        )
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")
        self.assertIn(("Allow", HEARTBEAT_METHOD), headers)
        self.assertEqual(self._row_count(), 0)

    def test_brew_exact_405_not_501(self) -> None:
        """(5) Arbitrary method BREW -> B4 405 + Allow: POST, NOT
        the stdlib default 501."""
        status, headers, body = self._exchange_parsed(
            self._request_bytes(method="BREW", body=b"")
        )
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")
        self.assertIn(("Allow", HEARTBEAT_METHOD), headers)
        self.assertEqual(self._row_count(), 0)

    def test_wrong_path_huge_cl_no_body_prompt_404(self) -> None:
        """(6) Wrong path + Content-Length: 999999 + NO body ->
        prompt 404 (body never read)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    target="/nope", body=b"", content_length=999999
                )
            )
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 404)
        self.assertEqual(self._row_count(), 0)

    def test_wrong_method_huge_cl_no_body_prompt_405(self) -> None:
        """(7) Wrong method + Content-Length: 999999 + NO body ->
        prompt 405 (body never read)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    method="PUT", body=b"", content_length=999999
                )
            )
            raw = _read_all(client)
        parsed = _parse_response(raw)
        status, headers, body = parsed
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")
        self.assertIn(("Allow", HEARTBEAT_METHOD), headers)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# FRAMING (B5-owned: 411/400/413)
# --------------------------------------------------------------------------


class FramingTest(HttpServerTestCase):
    def test_missing_content_length_411(self) -> None:
        """(8) No Content-Length on the exact POST -> 411."""
        parsed = self._exchange_parsed(
            self._request_bytes(include_content_length=False, body=b"")
        )
        self._assert_empty_response(parsed, 411)
        self.assertEqual(self._row_count(), 0)

    def test_duplicate_content_length_400(self) -> None:
        """(9) Duplicate Content-Length -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                body=b"",
                extra_lines=(b"Content-Length: 2",),
            )
        )
        self._assert_empty_response(parsed, 400)
        self.assertEqual(self._row_count(), 0)

    def test_case_insensitive_duplicate_content_length_400(self) -> None:
        """(10) Case-insensitive duplicate Content-Length -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                body=b"",
                extra_lines=(b"content-length: 2",),
            )
        )
        self._assert_empty_response(parsed, 400)

    def test_negative_content_length_400(self) -> None:
        """(11) Negative Content-Length -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(body=b"", content_length="-1")
        )
        self._assert_empty_response(parsed, 400)

    def test_plus_prefixed_content_length_400(self) -> None:
        """(12) '+10' Content-Length -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(body=b"", content_length="+10")
        )
        self._assert_empty_response(parsed, 400)

    def test_comma_list_content_length_400(self) -> None:
        """(13) Comma-separated Content-Length -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(body=b"", content_length="5, 5")
        )
        self._assert_empty_response(parsed, 400)

    def test_decimal_and_hex_content_length_400(self) -> None:
        """(14) Decimal / hex / internal whitespace / Unicode
        digits -> 400."""
        for value in ("1.5", "0x10", "1 2", "١٢"):
            with self.subTest(value=value):
                parsed = self._exchange_parsed(
                    self._request_bytes(body=b"", content_length=value)
                )
                self._assert_empty_response(parsed, 400)
        self.assertEqual(self._row_count(), 0)

    def test_empty_content_length_400(self) -> None:
        """(15) Empty Content-Length value -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(body=b"", content_length="")
        )
        self._assert_empty_response(parsed, 400)

    def test_transfer_encoding_400(self) -> None:
        """(16) Transfer-Encoding on exact POST -> 400 (not 501),
        body never read."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    extra_lines=(b"Transfer-Encoding: chunked",),
                    include_content_length=False,
                )
            )
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 400)
        self.assertEqual(self._row_count(), 0)

    def test_transfer_encoding_with_content_length_400(self) -> None:
        """(17) Transfer-Encoding + Content-Length -> 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                body=b"",
                extra_lines=(b"Transfer-Encoding: chunked",),
            )
        )
        self._assert_empty_response(parsed, 400)
        self.assertEqual(self._row_count(), 0)

    def test_cl_16385_headers_only_prompt_413(self) -> None:
        """(18) REAL LOOPBACK PROOF: client sends ONLY headers with
        Content-Length: 16385 and the server already answers 413
        (pre-read enforcement — no body byte is read)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"", content_length=MAX_HEARTBEAT_BODY_BYTES + 1
                )
            )
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 413)
        self.assertEqual(self._row_count(), 0)

    def test_cl_16384_not_size_rejected(self) -> None:
        """(19) Content-Length: 16384 is not rejected for size: a
        valid JSON padded to exactly 16384 bytes succeeds."""
        valid = _payload_bytes()
        padded = valid + b" " * (MAX_HEARTBEAT_BODY_BYTES - len(valid))
        self.assertEqual(len(padded), MAX_HEARTBEAT_BODY_BYTES)
        parsed = self._exchange_parsed(self._request_bytes(body=padded))
        self._assert_empty_response(parsed, 204)
        self.assertEqual(self._row_count(), 1)

    def test_cl_0_b4_400(self) -> None:
        """(20) Content-Length: 0 -> body=b"" reaches B4 -> B4 400."""
        parsed = self._exchange_parsed(
            self._request_bytes(body=b"", content_length=0)
        )
        self._assert_empty_response(parsed, 400)
        self.assertEqual(self._row_count(), 0)

    def test_short_body_half_close_400(self) -> None:
        """(21) REAL LOOPBACK PROOF: declared 100 bytes, only 10
        sent, then client half-close (shutdown WR) -> 400; B4 never
        sees a partial body."""
        body = _payload_bytes()
        head = (
            self._request_bytes(
                body=b"", content_length=100
            ).replace(b"\r\n\r\n", b"\r\n")
            + b"\r\n\r\n"
        )
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(head + body[:10])
            client.shutdown(socket.SHUT_WR)
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 400)
        self.assertEqual(self._row_count(), 0)

    def test_huge_decimal_cl_5000_nines_413(self) -> None:
        """(D) QA Defect 2: Content-Length = 5000 ASCII '9'
        digits, NO BODY -> prompt 413, no traceback, server stays
        usable (Python 3.11 int-str digit limit must never fire)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length="9" * 5000,
                )
            )
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 413)
        self.assertEqual(self._row_count(), 0)

    def test_leading_zero_16385_413(self) -> None:
        """(E) 5000 leading zeroes + '16385' (numeric 16385) ->
        413 without body read."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length="0" * 5000 + "16385",
                )
            )
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 413)
        self.assertEqual(self._row_count(), 0)

    def test_leading_zero_numeric_one_framing(self) -> None:
        """(F) 5000 leading zeroes + '1' (numeric 1) with exactly
        one body byte sent: NOT 413, NOT a crash — framing treats
        the declared numeric length as 1 and B4 sees the one-byte
        body (400: not valid heartbeat JSON)."""
        raw = self._exchange(
            self._request_bytes(
                body=b"X",
                content_length="0" * 5000 + "1",
            )
        )
        self._assert_empty_response(_parse_response(raw), 400)
        self.assertEqual(self._row_count(), 0)

    def test_leading_zero_numeric_one_short_body_400(self) -> None:
        """(F-short) Numeric-1 declaration with ZERO body bytes
        sent then half-close: short body -> 400 (framing honored
        numerically, not textually)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length="0" * 5000 + "1",
                )
            )
            client.shutdown(socket.SHUT_WR)
            raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 400)
        self.assertEqual(self._row_count(), 0)

    def test_huge_all_zero_cl_b4_400(self) -> None:
        """(G) Content-Length = 5000 ASCII '0' digits (numeric 0)
        -> B4 empty-body semantics 400, no uncaught exception."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                body=b"",
                content_length="0" * 5000,
            )
        )
        self._assert_empty_response(parsed, 400)
        self.assertEqual(self._row_count(), 0)

    def test_huge_cl_no_traceback_in_stderr(self) -> None:
        """(J) The huge Content-Length path emits no ValueError
        text or traceback through socketserver stderr."""
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            with socket.create_connection(
                self.server_address, timeout=10.0
            ) as client:
                client.sendall(
                    self._request_bytes(
                        body=b"",
                        content_length="9" * 5000,
                    )
                )
                raw = _read_all(client)
        self._assert_empty_response(_parse_response(raw), 413)
        captured = stderr_capture.getvalue()
        self.assertNotIn("Traceback", captured)
        self.assertNotIn("ValueError", captured)


# --------------------------------------------------------------------------
# EXPECT: 100-CONTINUE
# --------------------------------------------------------------------------


class ExpectTest(HttpServerTestCase):
    def test_expect_100_continue_417_no_interim(self) -> None:
        """(22) Expect: 100-continue on exact POST -> 417, no
        interim 100 response, no body required (headers only)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    extra_lines=(b"Expect: 100-continue",),
                )
            )
            raw = _read_all(client)
        # Exactly one final response: no interim 100 anywhere.
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_expect_wrong_route_does_not_wait_for_body(self) -> None:
        """(23) Expect on a wrong route: no 100, no body wait —
        the authoritative B4 404 arrives promptly."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    target="/nope",
                    body=b"",
                    content_length=999999,
                    extra_lines=(b"Expect: 100-continue",),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 404)

    def test_expect_wrong_method_does_not_wait_for_body(self) -> None:
        """(24) Expect on a wrong method: no 100, no body wait —
        the authoritative B4 405 arrives promptly."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    method="GET",
                    body=b"",
                    content_length=999999,
                    extra_lines=(b"Expect: 100-continue",),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        parsed = _parse_response(raw)
        status, headers, body = parsed
        self.assertEqual(status, 405)
        self.assertIn(("Allow", HEARTBEAT_METHOD), headers)

    def test_arbitrary_expectation_417_no_body(self) -> None:
        """(A) QA Defect 1: ANY Expect value (not just
        100-continue) on the exact POST -> final 417, empty body,
        no interim 100, body never sent/read, zero rows."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length=999999,
                    extra_lines=(b"Expect: arbitrary-expectation",),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self.assertNotIn(b"arbitrary-expectation", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_mixed_case_expect_header_417(self) -> None:
        """(B) Mixed-case Expect header name -> same 417."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length=999999,
                    extra_lines=(b"eXpEcT: anything",),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_multiple_expect_headers_417(self) -> None:
        """(C) Multiple Expect headers -> 417 (presence alone)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length=999999,
                    extra_lines=(
                        b"Expect: 100-continue",
                        b"Expect: something-else",
                    ),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_expect_plus_transfer_encoding_417(self) -> None:
        """(A) QA Defect: arbitrary Expect + Transfer-Encoding on
        the exact POST -> 417 (Expect precedence), NOT 400; body
        never sent/read; zero rows; prompt response."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    include_content_length=False,
                    extra_lines=(
                        b"Expect: arbitrary",
                        b"Transfer-Encoding: chunked",
                    ),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self.assertNotIn(b"arbitrary", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_expect_plus_te_plus_huge_cl_417(self) -> None:
        """(B) arbitrary Expect + Transfer-Encoding +
        Content-Length: 999999, NO BODY -> 417 (not 400, not 413,
        no body wait/read), zero rows."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length=999999,
                    extra_lines=(
                        b"Expect: arbitrary",
                        b"Transfer-Encoding: chunked",
                    ),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_expect_100_continue_plus_te_417_no_interim(self) -> None:
        """(C-100) Expect: 100-continue + Transfer-Encoding +
        nonzero Content-Length, NO BODY -> first/final response
        417, no interim 100."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    body=b"",
                    content_length=999999,
                    extra_lines=(
                        b"Expect: 100-continue",
                        b"Transfer-Encoding: chunked",
                    ),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 417)
        self.assertEqual(self._row_count(), 0)

    def test_wrong_path_expect_te_huge_cl_404(self) -> None:
        """(E) Routing boundary with the full hostile framing
        combination: wrong path + Expect + TE + huge CL, NO BODY ->
        still the authoritative B4 404 (no body wait/read)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    target="/nope",
                    body=b"",
                    content_length=999999,
                    extra_lines=(
                        b"Expect: arbitrary",
                        b"Transfer-Encoding: chunked",
                    ),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        self._assert_empty_response(_parse_response(raw), 404)
        self.assertEqual(self._row_count(), 0)

    def test_wrong_method_expect_te_huge_cl_405(self) -> None:
        """(F) Wrong method + Expect + TE + huge CL, NO BODY ->
        still the authoritative B4 405 + Allow: POST."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(
                self._request_bytes(
                    method="PUT",
                    body=b"",
                    content_length=999999,
                    extra_lines=(
                        b"Expect: arbitrary",
                        b"Transfer-Encoding: chunked",
                    ),
                )
            )
            raw = _read_all(client)
        self.assertNotIn(b"100 Continue", raw)
        parsed = _parse_response(raw)
        status, headers, body = parsed
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")
        self.assertIn(("Allow", HEARTBEAT_METHOD), headers)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# B4 HANDOFF (header multiplicity and token semantics)
# --------------------------------------------------------------------------


class B4HandoffTest(HttpServerTestCase):
    def test_duplicate_content_type_b4_400(self) -> None:
        """(25) Duplicate Content-Type -> B4 400 (multiplicity
        preserved through the stdlib header representation)."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                body=b"",
                headers=(
                    ("Content-Type", "application/json"),
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", _TOKEN_ONE),
                ),
            )
        )
        self._assert_empty_response(parsed, 400)
        self.assertEqual(self._row_count(), 0)

    def test_mixed_case_duplicate_token_b4_401(self) -> None:
        """(26) Mixed-case duplicate token header -> B4 401
        (case-insensitive duplicate detection stays in B4; a
        valid body is used so B4's empty-body 400 is not hit
        first)."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", _TOKEN_ONE),
                    ("x-sentinel-token", _TOKEN_ONE),
                ),
            )
        )
        self._assert_empty_response(parsed, 401)
        self.assertEqual(self._row_count(), 0)

    def test_lowercase_token_header_works(self) -> None:
        """(27) Lowercase token header name authenticates fine."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                headers=(
                    ("Content-Type", "application/json"),
                    ("x-sentinel-token", _TOKEN_ONE),
                ),
            )
        )
        self._assert_empty_response(parsed, 204)
        self.assertEqual(self._row_count(), 1)

    def test_token_semantics_preserved(self) -> None:
        """(28) Verbatim token semantics through stdlib/B5: a
        secret with INTERNAL whitespace (never OWS-strippable by
        the stdlib header parser) authenticates byte-exact only;
        the collapsed variant fails."""
        ingestor = HeartbeatIngestor(
            _config(), self.repository,
            clock=_ClockSequence(_RECEIVED_AT),
        )
        wire = AuthenticatedHeartbeatAdapter(
            _config(),
            NodeCredentials(
                {"vds-01": "syn thetic secret", "vds-02": _TOKEN_TWO}
            ),
            ingestor,
        )
        self.server.adapter = HeartbeatHttpAdapter(wire)  # type: ignore[attr-defined]
        parsed = self._exchange_parsed(
            self._request_bytes(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", "syn thetic secret"),
                ),
            )
        )
        self._assert_empty_response(parsed, 204)
        self.assertEqual(self._row_count(), 1)
        parsed = self._exchange_parsed(
            self._request_bytes(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", "synthetic secret"),
                ),
            )
        )
        self._assert_empty_response(parsed, 401)
        self.assertEqual(self._row_count(), 1)

    def test_wrong_token_b4_401(self) -> None:
        """(29) Wrong token -> B4 401, no row."""
        parsed = self._exchange_parsed(
            self._request_bytes(
                headers=(
                    ("Content-Type", "application/json"),
                    ("X-Sentinel-Token", _UNKNOWN_TOKEN),
                ),
            )
        )
        self._assert_empty_response(parsed, 401)
        self.assertEqual(self._row_count(), 0)


# --------------------------------------------------------------------------
# INTERNAL 500 BOUNDARY
# --------------------------------------------------------------------------


class InternalFailureTest(HttpServerTestCase):
    def test_repository_exception_500(self) -> None:
        """(30) sqlite3.OperationalError after a valid request ->
        500, empty body, no exception text."""
        self._start_server(repository=_FailingRepository(self.connection))
        status, headers, body = self._exchange_parsed(
            self._request_bytes()
        )
        self.assertEqual(status, 500)
        self.assertEqual(body, b"")
        self.assertIn(("Content-Length", "0"), headers)
        self.assertNotIn(b"simulated persistence failure", body)
        self.assertEqual(self._row_count(), 0)

    def test_internal_exception_log_silence(self) -> None:
        """(H) QA Defect 3: an internal exception carrying a unique
        marker produces a 500 whose captured stderr/logging output
        contains neither the marker, nor 'Traceback', nor the
        exception class/message — the caught exception is never
        logged or exposed at all."""
        self._start_server(
            repository=_MarkerFailingRepository(
                self.connection,
                "INTERNAL_EXCEPTION_SECRET_4f0b91c2",
            )
        )
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            status, headers, body = self._exchange_parsed(
                self._request_bytes()
            )
        self.assertEqual(status, 500)
        self.assertEqual(body, b"")
        self.assertIn(("Content-Length", "0"), headers)
        captured = stderr_capture.getvalue()
        self.assertNotIn("INTERNAL_EXCEPTION_SECRET_4f0b91c2", captured)
        self.assertNotIn("Traceback", captured)
        self.assertNotIn("OperationalError", captured)
        self.assertNotIn("simulated", captured)

    def test_server_still_serves_after_internal_failure(self) -> None:
        """(31)(I) After an internal failure the (fresh) server
        still serves successfully — the 500 path is not a crash."""
        self._start_server(repository=_FailingRepository(self.connection))
        status, _, _ = self._exchange_parsed(self._request_bytes())
        self.assertEqual(status, 500)
        self._start_server()
        status, _, _ = self._exchange_parsed(self._request_bytes())
        self.assertEqual(status, 204)
        self.assertEqual(self._row_count(), 1)


# --------------------------------------------------------------------------
# PARSER SAFETY (sanitized stdlib parser errors)
# --------------------------------------------------------------------------


class ParserSafetyTest(HttpServerTestCase):
    def test_malformed_request_empty_body_no_html(self) -> None:
        """(32) A parser-level malformed request (bad request line)
        -> parser-selected status, EMPTY body, no stdlib HTML, no
        marker reflection."""
        marker = b"MARKER-7f3a9b"
        raw = self._exchange(
            b"THIS IS NOT HTTP " + marker + b"\r\n\r\n"
        )
        status, headers, body = _parse_response(raw)
        self.assertNotEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertNotIn(b"<html>", body.lower())
        self.assertNotIn(b"<!doctype", body.lower())
        self.assertNotIn(marker, raw)

    def test_bad_version_no_version_disclosure(self) -> None:
        """(33) A request with an unsupported HTTP version ->
        sanitized response, no Server/Date version disclosure."""
        raw = self._exchange(
            self._request_bytes(
                body=b"@@@",
            ).replace(b"HTTP/1.1", b"HTTP/9.9")
        )
        status, headers, body = _parse_response(raw)
        self.assertNotEqual(status, 200)
        self.assertEqual(body, b"")
        names = {name.lower() for name, _ in headers}
        self.assertNotIn("server", names)

    def test_garbage_bytes_sanitized(self) -> None:
        """(34) Pure garbage bytes -> sanitized parser response
        (empty body), never an unhandled crash."""
        raw = self._exchange(b"\x00\x01\x02garbage\r\n\r\n")
        status, headers, body = _parse_response(raw)
        self.assertEqual(body, b"")


# --------------------------------------------------------------------------
# LOGGING SECRET SAFETY
# --------------------------------------------------------------------------


class LoggingSafetyTest(HttpServerTestCase):
    def test_no_request_data_in_captured_stderr(self) -> None:
        """(35) Unique request/path/token markers never appear in
        captured stderr: access logging is fully disabled."""
        path_marker = "UNIQUE-PATH-MARKER-51c9"
        token_marker = "UNIQUE-TOKEN-MARKER-88d2"
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            self._exchange(
                self._request_bytes(
                    target=f"/{path_marker}",
                    body=b"",
                    headers=(
                        ("Content-Type", "application/json"),
                        ("X-Sentinel-Token", token_marker),
                    ),
                )
            )
        captured = stderr_capture.getvalue()
        self.assertNotIn(path_marker, captured)
        self.assertNotIn(token_marker, captured)
        self.assertNotIn("127.0.0.1", captured)


# --------------------------------------------------------------------------
# MINIMAL RESPONSE / VERSION DISCLOSURE
# --------------------------------------------------------------------------


class MinimalResponseTest(HttpServerTestCase):
    def test_no_server_header_success(self) -> None:
        """(36) No Server header, no BaseHTTP/Python version
        disclosure on the success response."""
        status, headers, body = self._exchange_parsed(
            self._request_bytes()
        )
        self.assertEqual(status, 204)
        names = {name.lower() for name, _ in headers}
        self.assertNotIn("server", names)
        raw_values = [value for _, value in headers]
        for value in raw_values:
            self.assertNotIn("Python", value)
            self.assertNotIn("BaseHTTP", value)

    def test_b4_allow_post_preserved(self) -> None:
        """(37) B4's Allow: POST header is preserved verbatim."""
        status, headers, body = self._exchange_parsed(
            self._request_bytes(method="GET", body=b"")
        )
        self.assertEqual(status, 405)
        self.assertIn(("Allow", HEARTBEAT_METHOD), headers)

    def test_b5_owned_statuses_content_length_zero(self) -> None:
        """(38) All B5-owned statuses: empty body + CL: 0."""
        scenarios: list[tuple[bytes, int]] = [
            (
                self._request_bytes(
                    include_content_length=False, body=b""
                ),
                411,
            ),
            (
                self._request_bytes(body=b"", content_length="-1"),
                400,
            ),
            (
                self._request_bytes(
                    body=b"",
                    content_length=MAX_HEARTBEAT_BODY_BYTES + 1,
                ),
                413,
            ),
            (
                self._request_bytes(
                    body=b"",
                    extra_lines=(b"Expect: 100-continue",),
                ),
                417,
            ),
        ]
        for raw_request, expected in scenarios:
            with self.subTest(expected=expected):
                parsed = self._exchange_parsed(raw_request)
                self._assert_empty_response(parsed, expected)


# --------------------------------------------------------------------------
# CONNECTION MODEL
# --------------------------------------------------------------------------


class ConnectionModelTest(HttpServerTestCase):
    def test_connection_closed_after_response(self) -> None:
        """(39) One request per connection: after the response the
        socket is at EOF (Connection: close, no keep-alive)."""
        with socket.create_connection(
            self.server_address, timeout=10.0
        ) as client:
            client.sendall(self._request_bytes())
            raw = _read_all(client)
            self._assert_empty_response(_parse_response(raw), 204)
            # Already at EOF: further reads stay at EOF.
            self.assertEqual(client.recv(65536), b"")

    def test_sequential_requests_separate_connections(self) -> None:
        """(40) Repeated requests over fresh connections all
        succeed (serial HTTPServer keeps serving)."""
        for _ in range(3):
            parsed = self._exchange_parsed(self._request_bytes())
            self._assert_empty_response(parsed, 204)
        self.assertEqual(self._row_count(), 3)


# --------------------------------------------------------------------------
# SERVER API
# --------------------------------------------------------------------------


class ServerApiTest(HttpServerTestCase):
    def test_ephemeral_port_binds_concrete_address(self) -> None:
        """(41) port=0 binds a concrete ephemeral port."""
        host, port = self.server.server_address  # type: ignore[attr-defined]
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)

    def test_server_is_plain_httpserver_not_threading(self) -> None:
        """(42) The server is exactly an HTTPServer subclass, NOT
        a ThreadingHTTPServer (serial connection model)."""
        from http.server import HTTPServer, ThreadingHTTPServer

        self.assertIsInstance(self.server, HTTPServer)
        self.assertNotIsInstance(self.server, ThreadingHTTPServer)


if __name__ == "__main__":
    unittest.main()
