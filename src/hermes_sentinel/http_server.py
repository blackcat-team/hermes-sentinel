"""Heartbeat HTTP server bridge (Stage B5) — stdlib HTTPServer.

A minimal stdlib-only transport bridge that binds the Stage B4
request semantics to a real network endpoint:

    TCP client (reporter, plaintext)
                |
     http.server.HTTPServer + BaseHTTPRequestHandler   (B5)
     — stdlib raw HTTP parsing ONLY; no custom protocol parser
                |
     HeartbeatHttpAdapter.handle(HttpRequest) -> HttpResponse   (B4)
                |
     AuthenticatedHeartbeatAdapter (B3)
                |
     HostTelemetry -> HeartbeatIngestor (B2) -> SQLite (B1)

B5 owns ONLY the transport/framing bridge. All raw HTTP parsing —
request line, header lines, CRLF framing, HTTP grammar — is the
stdlib ``BaseHTTPRequestHandler`` parser; B5 implements no
alternative protocol semantics of its own.

**Plaintext boundary**: B5 is a plaintext *backend* listener. It
is NOT a production Internet-facing endpoint. Production reporters
reach Sentinel through outbound HTTPS only; TLS termination and
network exposure belong to later production hardening/deployment
stages (Stage F), not here.

Normative bridge contract (see docs/ARCHITECTURE.md §14):

- the server is exactly ``http.server.HTTPServer`` (serial, one
  request at a time — NOT ``ThreadingHTTPServer``), created via
  :func:`create_heartbeat_http_server`. ``port=0`` is allowed
  (ephemeral port discovery via ``server_address``);
  ``serve_forever``/``shutdown``/``server_close`` stay the standard
  ``HTTPServer`` lifecycle; there is no global singleton;
- one request per connection: after the response the connection is
  closed (``close_connection = True`` always). No keep-alive, no
  pipelining. B5 deliberately introduces NO production connection
  timeout policy — that is Stage F / deployment hardening scope;
- routing stays in B4. For a wrong path OR a wrong method the body
  is NOT read: the request is handed to B4 with ``body=b""`` and
  B4 decides 404/405. This includes announced-but-unsent bodies
  (``Content-Length: 999999`` with no body following) — the
  connection is closed without reading;
- ANY syntactically accepted method reaches B4: the handler
  dispatches every method through the stdlib mechanism (a generic
  ``do_*`` fallback via ``__getattr__``), so e.g. ``BREW`` becomes
  B4's 405 + ``Allow: POST`` — never the stdlib default 501;
- ANY ``Expect`` header on the exact heartbeat POST is rejected:
  417, empty body, no interim 100 response, body never read,
  B4/B3/B2/B1 never invoked. PRESENCE ALONE is authoritative —
  expectation values are never inspected, normalized or parsed
  (the stdlib ``handle_expect_100`` hook only covers the specific
  100-continue case, so the framing path detects any Expect via
  the multiplicity-preserving stdlib ``headers.get_all``). On a
  wrong route/method, Expect does not make the server wait for
  the body and does not disturb the authoritative B4 404/405
  behavior (no 100 is ever sent — the response is final);
- framing is required only for the exact ``POST /v1/heartbeat``:
  a missing Content-Length is 411 (not B4's empty-body 400 —
  B5 owns framing); duplicated (case-insensitively), invalid, or
  non-``[0-9]+``-after-OWS values are 400;
- ``Transfer-Encoding`` present (exact heartbeat POST, with or
  without Content-Length) is 400; chunked decoding is NOT
  implemented and the body is never read;
- pre-read size limit: a Content-Length whose NUMERIC value is
  above ``MAX_HEARTBEAT_BODY_BYTES`` (the B4 limit, 16384) is
  answered 413 immediately — WITHOUT reading the oversized body
  off the socket and WITHOUT converting the digit string with an
  unbounded ``int()`` (Python 3.11+'s integer-string digit safety
  limit makes that an uncaught ``ValueError``; the comparison
  uses the significant decimal representation, so leading zeroes
  are numerically insignificant: ``"0"*5000 + "1"`` is 1, never
  413 for textual length). ``16384`` is not rejected for size
  alone; ``0`` is forwarded to B4 as ``body=b""`` (B4 answers
  400);
- with a valid Content-Length <= 16384 exactly the declared bytes
  are read. A short EOF / client half-close before the declared
  size is 400; B4 is never called with a partial body. Bytes
  beyond the declared length are never read;
- the B4 :class:`HttpRequest` receives the stdlib-parsed headers
  as ``tuple[tuple[str, str], ...]``. Multiplicity is preserved
  for Content-Type and X-Sentinel-Token (duplicates stay visible
  as multiple pairs — B4 owns the duplicate semantics). B5 works
  only with the semantic header values the stdlib parser produced:
  no raw-header reconstruction, no token stripping/normalization/
  case-folding, no raw header bytes;
- response emission relays the actual B4 status/headers/body, plus
  ``Connection: close``. B5-owned statuses (400/411/413/417/500)
  are always empty-body with ``Content-Length: 0``. The stdlib
  HTML error pages are never used: ``send_error`` is overridden
  to a sanitized empty-body emission (parser-selected statuses are
  kept, but no raw request line, header/token/body values or
  exception text is ever reflected);
- internal 500 boundary: B4 deliberately propagates unexpected
  application exceptions. B5 catches ``Exception`` — ONLY around
  the ``HeartbeatHttpAdapter.handle`` call, never ``BaseException``
  — and answers a deterministic empty-body 500 with
  ``Content-Length: 0``. The caught application exception is
  deliberately NOT logged at all (no ``logger.exception``, no
  traceback, no ``exc_info``, no exception text — Stage F owns
  observability), so no exception message, traceback or
  potentially sensitive internal data is ever emitted. An
  internal failure is never turned into a 400;
- logging: the standard ``BaseHTTPRequestHandler`` access logging
  is disabled (``log_message`` overridden to emit nothing). No
  path, query, request line, headers, token, body, exception
  message or traceback is ever printed. Observability belongs to
  Stage F;
- version disclosure: no ``Server``/``Date`` headers carrying
  ``BaseHTTP/...`` or ``Python/...`` are emitted. Responses are
  emitted via ``send_response_only`` plus explicit safe headers
  (never the plain ``send_response``, which would add the
  stdlib ``Server`` header);
- unapproved contracts are deliberately absent: no custom 431
  head cap, no custom 505 version policy, no custom
  UTF-8/surrogateescape header parser, no custom HTTP grammar, no
  product connection timeout policy. Any status the stdlib parser
  itself generates is sanitized in its response, without adding an
  alternative protocol semantics of our own.

Out of scope for B5: TLS (plaintext backend listener — see the
plaintext boundary above), keep-alive/pipelining, chunked transfer,
compression, access logging, rate limiting, credential loaders,
token rotation, replay protection and deployment hardening.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from hermes_sentinel.http_api import (
    HEARTBEAT_METHOD,
    HEARTBEAT_PATH,
    HeartbeatHttpAdapter,
    HttpRequest,
    HttpResponse,
    MAX_HEARTBEAT_BODY_BYTES,
)

__all__ = [
    "MAX_CONTENT_LENGTH",
    "HeartbeatRequestHandler",
    "create_heartbeat_http_server",
]

#: Upper bound of the declared heartbeat body size. Same value as
#: the B4 ``MAX_HEARTBEAT_BODY_BYTES`` limit, enforced at the B5
#: pre-read boundary: a larger declared Content-Length is rejected
#: with 413 BEFORE the body is read off the socket.
MAX_CONTENT_LENGTH = MAX_HEARTBEAT_BODY_BYTES

#: B5-owned response statuses (always empty body, Content-Length: 0).
_STATUS_BAD_REQUEST = 400
_STATUS_LENGTH_REQUIRED = 411
_STATUS_PAYLOAD_TOO_LARGE = 413
_STATUS_EXPECTATION_FAILED = 417
_STATUS_INTERNAL_ERROR = 500

#: ASCII digits ONLY — the full accepted Content-Length grammar
#: after surrounding OWS. No signs, commas, decimal points, hex,
#: internal whitespace or Unicode digit look-alikes.
_DIGITS = frozenset("0123456789")


def _declared_length_status(raw: str) -> int | None:
    """Compare a grammar-valid ``[0-9]+`` string to MAX without
    unbounded ``int()`` conversion.

    Python 3.11+ limits decimal-string conversion length (the
    interpreter's integer-string digit safety limit), so an
    attacker-supplied 5000-digit Content-Length must never reach
    ``int()``. Numeric semantics stay exact:

    - leading ASCII zeroes are insignificant: ``"0" * 5000 + "1"``
      numerically equals 1 (no 413 merely for textual length);
    - ``"0" * 5000 + "16385"`` numerically equals 16385 → 413;
    - ``"0" * 5000`` numerically equals 0;

    comparison uses the significant decimal representation against
    the MAX text (length first, lexicographic second — valid for
    equal-length digit strings). Returns the B5 status (413) when
    the numeric value exceeds MAX, else ``None``; the caller only
    needs a bounded ``int()`` for at most five digits afterwards.
    """
    max_text = str(MAX_CONTENT_LENGTH)
    significant = raw.lstrip("0")
    if not significant:
        # All zeroes: numeric value 0, never oversized.
        return None
    if (
        len(significant) > len(max_text)
        or (
            len(significant) == len(max_text)
            and significant > max_text
        )
    ):
        return _STATUS_PAYLOAD_TOO_LARGE
    return None


def _content_length_or_none(headers: Any) -> int | None:
    """Strict framing validation of Content-Length (B5 boundary).

    Uses the multiplicity-preserving stdlib header representation
    (``headers.get_all``) — no raw-header parsing. Returns the
    valid declared length, or ``None`` when no Content-Length
    header is present. Raises ``_FramingError`` with the
    deterministic B5 status for any deviation:

    - more than one Content-Length header (case-insensitive
      duplicates are exactly what ``get_all`` exposes) → 400;
    - a value that is not, after surrounding OWS, plain ``[0-9]+``
      (empty, negative, ``+10``, comma list, decimal, hex, internal
      whitespace, Unicode digits) → 400;
    - a declared numeric length above ``MAX_CONTENT_LENGTH`` → 413
      (raised before any body byte is read — pre-read enforcement,
      and decided WITHOUT converting an arbitrarily long digit
      string with ``int()``: no attacker-controlled digit string
      can escape as an uncaught ``ValueError``).

    The returned ``int`` is only ever produced from a bounded
    (<= 5 significant digits) string.
    """
    values = headers.get_all("Content-Length")
    if values is None:
        return None
    if len(values) != 1:
        # Duplicated Content-Length (any case): fail closed.
        raise _FramingError(_STATUS_BAD_REQUEST)
    raw = values[0].strip(" \t")
    if not raw or any(char not in _DIGITS for char in raw):
        raise _FramingError(_STATUS_BAD_REQUEST)
    if _declared_length_status(raw) is not None:
        raise _FramingError(_STATUS_PAYLOAD_TOO_LARGE)
    significant = raw.lstrip("0")
    if not significant:
        return 0
    # Bounded conversion: significant is at most as long as the
    # MAX text (5 digits) — far below any interpreter limit.
    return int(significant)


class _FramingError(Exception):
    """A deterministic B5 framing failure with an HTTP status.

    Internal marker: mapped to an empty-body response with
    ``Content-Length: 0``. Never reflects any client value.
    """

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class HeartbeatRequestHandler(BaseHTTPRequestHandler):
    """Stdlib-parsed HTTP request handler for the heartbeat endpoint.

    All raw HTTP parsing (request line, header lines, CRLF framing)
    is inherited from ``BaseHTTPRequestHandler``. The class adds
    only the B4 handoff and the B5-owned framing rules documented
    in the module docstring.

    The class is instantiated per connection BY the stdlib server;
    the B4 adapter is injected through ``server.adapter`` (set up
    by :func:`create_heartbeat_http_server`).
    """

    # One request per connection: no keep-alive, no pipelining.
    # (Also disables the stdlib HTTP/1.1 default keep-alive.)
    protocol_version = "HTTP/1.1"

    # --- secret-safe logging / error surfaces --------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Access logging disabled: request-controlled data (path,
        query, request line, headers, token, body) is never
        printed. Observability belongs to Stage F."""

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        """Error logging disabled for the same reason: no request
        line, header values or exception text is ever printed."""

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Sanitized stdlib parser-error emission.

        The stdlib ``send_error`` builds an HTML body echoing the
        raw request line and the error message — both are
        unacceptable reflections. This override keeps the
        parser-selected status but emits a deterministic empty
        body with ``Content-Length: 0`` and no extra headers. It
        never reconstructs the request, so no raw bytes, header
        values or token material can leak.

        A request whose request line never parsed leaves the stdlib
        ``request_version`` at its ``HTTP/0.9`` default, which
        silences ``send_response_only``/``send_header`` entirely
        (HTTP/0.9 has no status line or headers). Forcing a 1.1
        request version here is purely an emission detail of this
        sanitized path — the client gets a well-formed, empty
        status response instead of a bare connection close.
        """
        self.close_connection = True
        self.request_version = "HTTP/1.1"
        self.send_response_only(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    # --- generic method dispatch (any accepted method reaches B4) -------

    def __getattr__(self, name: str) -> Any:
        """Dispatch EVERY ``do_<METHOD>`` to one handler.

        The stdlib parser accepts arbitrary token methods but only
        calls ``do_<method>`` handlers; unknown methods fall back
        to a 501 from ``send_error`` — which would bypass B4. This
        generic fallback routes every syntactically accepted
        method (``BREW``, ``FROB``, anything) into the same B4
        handoff, so B4 stays the single routing authority (405 +
        ``Allow: POST`` for wrong methods).
        """
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    # --- Expect: 100-continue -------------------------------------------

    def handle_expect_100(self) -> bool:
        """Reject ``Expect: 100-continue`` on the exact heartbeat
        POST: 417, empty body, no interim 100, body never read.

        The stdlib only calls this hook for the specific
        100-continue expectation; ANY OTHER Expect value is caught
        by the framing path (:meth:`_resolve_body`), which rejects
        every Expect presence on the exact heartbeat POST with the
        same final 417 — presence alone is authoritative there.

        On a wrong route/method the request is not a heartbeat
        POST: returning True continues normal processing WITHOUT
        sending an interim 100 — the final authoritative B4
        404/405 response follows promptly and the body is never
        read. Expect never makes the server wait for a body or
        disturbs the B4 behavior.
        """
        if (
            self.command == HEARTBEAT_METHOD
            and self.path == HEARTBEAT_PATH
        ):
            self.close_connection = True
            self.send_response_only(_STATUS_EXPECTATION_FAILED)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return False
        # Non-heartbeat route/method: continue to the B4 dispatch;
        # no interim 100 is ever sent (the final response is next).
        return True

    # --- the single dispatch path ----------------------------------------

    def _dispatch(self) -> None:
        """Serve one parsed request through the B4 adapter.

        Deterministic order:

        1. build the stdlib-parsed header tuple (multiplicity
           preserved — duplicates stay visible for B4);
        2. framing/body policy (only the exact heartbeat POST
           requires a body — see :meth:`_resolve_body`);
        3. hand the request to B4 and relay its response verbatim;
        4. B5-owned exceptions (framing) map to empty-body
           responses; ONLY around the B4 ``handle`` call,
           ``Exception`` is caught and mapped to a SILENT,
           empty-body 500 (never ``BaseException`` — internal
           failures are never turned into 400, and the exception
           object is never logged or exposed in any way).
        """
        headers = tuple(self.headers.items())
        try:
            body = self._resolve_body(headers)
        except _FramingError as framing_error:
            self._emit_b5_response(framing_error.status)
            return

        request = HttpRequest(
            method=self.command,
            path=self.path,
            headers=headers,
            body=body,
        )
        try:
            response = self.server.adapter.handle(request)  # type: ignore[attr-defined]
        except Exception:
            # Generic internal-failure mapping (B5 responsibility).
            # Deliberately SILENT: no logger.exception, no
            # traceback, no exc_info, no exception text — the
            # caught application exception object is never exposed
            # (Stage F owns observability). Deterministic
            # empty-body 500 only. Never catches BaseException:
            # KeyboardInterrupt/SystemExit stay unmasked. Internal
            # failures are never turned into 400.
            self._emit_b5_response(_STATUS_INTERNAL_ERROR)
            return
        self._emit_response(response)

    def _resolve_body(
        self, headers: tuple[tuple[str, str], ...]
    ) -> bytes:
        """Framing/body policy for one parsed request.

        Only the exact ``POST /v1/heartbeat`` requires framing.
        Framing precedence (only AFTER routing established the
        exact heartbeat POST):

        1. ANY Expect header present → 417 (presence alone
           decides; expectation values are never inspected or
           normalized; the body is never read and B4/B3/B2/B1
           are never invoked — zero persistence rows). Expect
           takes precedence over all other framing: Expect +
           Transfer-Encoding (with or without Content-Length)
           is 417, never 400/413;
        2. Transfer-Encoding present → 400 (fail closed, chunked
           decoding is not implemented; the body is never read).
           Content-Length alongside Transfer-Encoding is also 400;
        3. Content-Length missing → 411; invalid/duplicated → 400
           (via :func:`_content_length_or_none`); declared
           numeric length above ``MAX_CONTENT_LENGTH`` → 413
           before any body byte is read — decided WITHOUT an
           unbounded ``int()`` conversion, so no attacker-supplied
           digit string can escape as an uncaught ``ValueError``;

        and for a wrong path or wrong method: the body is NOT
        read — the request reaches B4 with ``body=b""`` and B4
        decides 404/405 (this includes announced-but-unsent
        bodies). Otherwise exactly the declared bytes are read. A
        short EOF / client half-close before the declared size →
        400; B4 is never called with a partial body. Bytes beyond
        the declared length are never read.
        """
        if (
            self.command != HEARTBEAT_METHOD
            or self.path != HEARTBEAT_PATH
        ):
            # Routing stays in B4: do not read the body, do not
            # enforce heartbeat framing on non-heartbeat requests.
            return b""

        # ANY Expect header on the exact heartbeat POST is rejected
        # FIRST (presence alone is authoritative — values are never
        # inspected, normalized or parsed). Expect presence takes
        # precedence over ALL other framing for the exact heartbeat
        # POST: Expect + Transfer-Encoding (with or without
        # Content-Length) is 417, never 400/413. The stdlib calls
        # handle_expect_100() only for the specific 100-continue
        # case, so arbitrary expectation values must be caught
        # here in the framing path as well: 417, body never read,
        # B4/B3/B2/B1 never invoked.
        expects = self.headers.get_all("Expect")
        if expects:
            raise _FramingError(_STATUS_EXPECTATION_FAILED)

        transfer_encodings = self.headers.get_all("Transfer-Encoding")
        if transfer_encodings:
            # Identity framing only: fail closed. The body is not
            # read (no chunked decoder exists). TE + CL is equally
            # 400 — Transfer-Encoding presence alone decides.
            raise _FramingError(_STATUS_BAD_REQUEST)

        content_length = _content_length_or_none(self.headers)
        if content_length is None:
            raise _FramingError(_STATUS_LENGTH_REQUIRED)
        if content_length == 0:
            return b""

        body = self.rfile.read(content_length)
        if len(body) != content_length:
            # Short EOF / client half-close before the declared
            # size: B4 is never called with a partial body.
            raise _FramingError(_STATUS_BAD_REQUEST)
        return body

    # --- response emission ------------------------------------------------

    def _emit_response(self, response: HttpResponse) -> None:
        """Relay an actual B4 response: status, headers, body."""
        self.close_connection = True
        self.send_response_only(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        if response.body:
            # B4 responses always carry an empty body; a non-empty
            # body would desync the relayed Content-Length. The
            # B5-owned statuses below are the only bodyless path.
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)
        else:
            # B4 always emits Content-Length: 0 itself; keep the
            # emission total without duplicating it.
            if not any(
                name.lower() == "content-length"
                for name, _ in response.headers
            ):
                self.send_header("Content-Length", "0")
            self.end_headers()

    def _emit_b5_response(self, status: int) -> None:
        """Emit a B5-owned status: empty body, ``Content-Length: 0``."""
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


class _HeartbeatHTTPServer(HTTPServer):
    """``HTTPServer`` carrying the B4 adapter to its handlers.

    Serial by inheritance (``socketserver.BaseServer`` serves one
    request at a time — NOT ``ThreadingHTTPServer``). No behavior
    is added beyond the ``adapter`` attribute.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        adapter: HeartbeatHttpAdapter,
    ) -> None:
        # Bind immediately via the standard HTTPServer lifecycle;
        # allow_reuse_address keeps the stdlib default semantics.
        super().__init__(server_address, HeartbeatRequestHandler)
        self.adapter = adapter


def create_heartbeat_http_server(
    adapter: HeartbeatHttpAdapter,
    host: str,
    port: int,
) -> _HeartbeatHTTPServer:
    """Create (bind) the heartbeat HTTP server; do not serve yet.

    Returns a ready-to-serve ``HTTPServer`` subclass instance:

    - ``serve_forever()`` / ``shutdown()`` / ``server_close()`` —
      the standard lifecycle; the caller owns it (no global
      singleton);
    - ``server_address`` — the concrete bound address (``port=0``
      requests an ephemeral port, resolved here);
    - ``server.adapter`` — the injected B4 ``HeartbeatHttpAdapter``.

    Fails fast on bind errors (the stdlib constructor raises).
    """
    return _HeartbeatHTTPServer((host, port), adapter)
