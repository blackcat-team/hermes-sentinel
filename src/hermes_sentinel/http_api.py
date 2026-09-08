"""Heartbeat HTTP request semantics (Stage B4).

A minimal, stdlib-only HTTP request adapter in front of the Stage B3
authenticated wire boundary:

    future network listener (Stage B5)
                |
    HeartbeatHttpAdapter.handle(HttpRequest) -> HttpResponse   (B4)
                |
    strict UTF-8 JSON Mapping + presented token header value
                |
    AuthenticatedHeartbeatAdapter (B3)
                |
    HostTelemetry -> HeartbeatIngestor (B2) -> SQLite (B1)

B4 deliberately starts no network listener and parses no raw HTTP
bytes: socket binding, the connection lifecycle and wire-format
parsing belong to Stage B5. B4 defines only the request/response
semantics of the single heartbeat endpoint over a minimal typed
abstraction (``HttpRequest``/``HttpResponse``).

Endpoint contract (see docs/ARCHITECTURE.md §13):

- route: exactly ``POST /v1/heartbeat`` — no path normalization of
  any kind. Trailing slashes, query strings and percent-encoded
  alternates are 404; the right path with a wrong method is 405 plus
  ``Allow: POST`` (HTTP methods are case-sensitive: ``post`` is a
  wrong method);
- body: raw ``bytes`` with a hard limit of 16 KiB
  (``MAX_HEARTBEAT_BODY_BYTES``). A larger body is 413; an empty
  body is 400. The limit is enforced on the already-received body —
  socket-level pre-read enforcement (Content-Length/read limits) is
  a Stage B5 responsibility;
- Content-Type: exactly ``application/json``, optionally with
  ``; charset=utf-8``; media type and charset tokens compare
  case-insensitively. A missing or unsupported Content-Type is 415;
  a syntactically malformed or duplicated Content-Type is 400. No
  permissive guessing: ``text/json``, ``application/*+json``, other
  charsets and quoted charset values are all rejected;
- token: the reporter token travels in its own ``X-Sentinel-Token``
  header — never in the JSON payload. The header must appear exactly
  once (names match case-insensitively); missing or duplicated means
  a generic 401. The value is forwarded to B3 VERBATIM: no
  stripping, no case folding, no Unicode normalization. B3 stays
  authoritative for token shape, exact comparison and per-node
  binding — B4 implements no second authentication system;
- JSON: strict stdlib decoding — invalid UTF-8 is 400, malformed
  JSON is 400, a non-object root is 400, duplicate object keys at
  ANY nesting depth (via ``object_pairs_hook``) are 400, and the
  non-standard NaN/Infinity/-Infinity constants (via
  ``parse_constant``) are 400. Pathologically deep client JSON that
  exhausts the stdlib parser recursion budget (``RecursionError``)
  is likewise a malformed/unprocessable client input → 400, caught
  strictly inside the bounded JSON parsing boundary. The decoded
  Mapping reaches B3 without semantic mutation; wire schema
  validation stays in B3;
- success: 204 No Content with an empty body. The B3
  ``HeartbeatReceipt`` is deliberately not exposed to the HTTP
  surface.

Deterministic check order (first match wins):

1. malformed request structure — non-``str`` method/path/header
   names or values, non-``tuple`` headers, non-``bytes`` body, a
   header pair that is not a 2-tuple, an empty header name, or
   CR/LF/other control characters inside header names/values → 400;
2. wrong path → 404;
3. wrong method → 405 + ``Allow: POST``;
4. body larger than ``MAX_HEARTBEAT_BODY_BYTES`` → 413;
5. empty body → 400;
6. missing/unsupported Content-Type → 415;
   malformed/duplicated Content-Type → 400;
7. missing or duplicated ``X-Sentinel-Token`` header → 401;
8. invalid UTF-8 / malformed JSON / duplicate keys / non-object
   root / non-finite constants / parser recursion exhaustion → 400;
9. ``MalformedHeartbeatPayloadError`` from B3 → 400;
   ``HeartbeatAuthenticationError`` from B3 → 401.

Internal failure semantics: after successful HTTP-level parsing B4
catches ONLY the two expected B3 external input errors above; the
ONLY additional catch is ``RecursionError``, and only inside the
bounded JSON parsing boundary, where it denotes malformed/
unprocessable client JSON (a client 400 — see the JSON rule above).
Any other exception — repository, B2 or B3 internals, ``MemoryError``
or any arbitrary internal failure — propagates unchanged to the
caller, and no broad exception handling is ever wrapped around the
B3/B2/B1 call chain. The future B5 server boundary owns the generic
HTTP 500 mapping and logging policy; an internal failure is never
masked as 204/400/401.

Secret safety: every response B4 produces has an empty body, a
deterministic ``Content-Length: 0`` and never contains exception
text, token material or node existence details.
``HttpRequest.__repr__`` never renders header values (the token
header carries a secret) nor the body content, and B4 creates no
exception message containing header or body values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, NoReturn

from hermes_sentinel.wire import (
    AuthenticatedHeartbeatAdapter,
    HeartbeatAuthenticationError,
    MalformedHeartbeatPayloadError,
)

__all__ = [
    "HEARTBEAT_PATH",
    "HEARTBEAT_METHOD",
    "MAX_HEARTBEAT_BODY_BYTES",
    "HttpRequest",
    "HttpResponse",
    "HeartbeatHttpAdapter",
]

#: Exact heartbeat route. No normalization of any kind: trailing
#: slashes, query strings and percent-encoded alternates are 404.
HEARTBEAT_PATH = "/v1/heartbeat"

#: Exact heartbeat method. HTTP methods are case-sensitive: "post"
#: is a wrong method (405).
HEARTBEAT_METHOD = "POST"

#: Hard upper bound of the heartbeat request body: 16 KiB. Enforced
#: on the already-received body; socket-level pre-read enforcement
#: belongs to Stage B5.
MAX_HEARTBEAT_BODY_BYTES = 16384

#: Header names are matched case-insensitively (lowercased here).
_CONTENT_TYPE_HEADER = "content-type"
_TOKEN_HEADER = "x-sentinel-token"

#: The only acceptable media type (compared case-insensitively).
_JSON_MEDIA_TYPE = "application/json"
#: The only acceptable charset parameter value (case-insensitive).
_UTF8_CHARSET = "utf-8"

#: Every response B4 produces carries this header deterministically.
_CONTENT_LENGTH_ZERO = (("Content-Length", "0"),)


# --- typed request/response model ----------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class HttpRequest:
    """Minimal immutable typed HTTP request model (B4 boundary input).

    Framework-free by design: the future Stage B5 listener
    constructs this value from its own raw wire parsing; B4 never
    parses raw HTTP bytes itself. Construction is deliberately
    permissive — structural validation happens fail-closed at the
    :class:`HeartbeatHttpAdapter` boundary, so a malformed request
    maps to a safe 400 instead of a constructor exception.

    ``headers`` is a tuple of ``(name, value)`` pairs — not a dict —
    because duplicate headers must stay deterministically
    detectable. Header names are matched case-insensitively; header
    values are forwarded verbatim.
    """

    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __repr__(self) -> str:
        """Secret-safe total repr: a compile-time constant.

        The request carries attacker-controlled and secret-bearing
        data (header values include the token), so the repr shows NO
        field data at all — no method, no path, no header names or
        values, no body content and no body size. It never invokes
        ``repr()``/``str()`` on any caller-controlled field, so a
        hostile object or ``str`` subclass with a raising or
        secret-disclosing ``__repr__`` can neither make
        ``repr(request)`` fail nor leak through it, and the repr is
        total for any malformed request shape. Diagnostic richness
        is deliberately sacrificed for secret safety.
        """
        return "HttpRequest(<redacted>)"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Minimal immutable typed HTTP response (B4 boundary output).

    Every response B4 produces is deterministic: an integer status,
    header pairs (always ``Content-Length: 0``; 405 additionally
    ``Allow: POST``) and an always-empty body. No exception text,
    token material, node identity or node existence details ever
    enter a response, so the default dataclass repr is safe.
    """

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


# --- strict JSON decoding -------------------------------------------------


class _StrictJsonInputError(ValueError):
    """Internal marker: client JSON input violates the strict rules.

    Never escapes this module (mapped to an empty-body 400).
    Messages deliberately name no offending value: a hostile client
    may place secret-looking material inside the body.
    """


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook``: reject duplicate keys at any depth.

    Applied to every JSON object, including nested ones.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonInputError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_name: str) -> NoReturn:
    """``parse_constant``: NaN/Infinity/-Infinity are not JSON."""
    raise _StrictJsonInputError("non-finite JSON constants are rejected")


def _decode_strict_json(body: bytes) -> dict[str, Any] | None:
    """Strictly decode UTF-8 JSON object bytes; ``None`` on failure.

    Strict rules: valid UTF-8, well-formed JSON, an object root, no
    duplicate object keys at any nesting depth and no
    NaN/Infinity/-Infinity constants. Pathologically deep nesting
    that exhausts the stdlib parser recursion budget
    (``RecursionError``) is also a client input failure — it is
    caught ONLY inside this bounded JSON parsing boundary, never
    around B3/B2/B1 calls. The decoded mapping is returned without
    semantic mutation — wire schema validation stays in B3.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except ValueError:
        # json.JSONDecodeError, duplicate keys and rejected constants
        # are all deterministic client input failures.
        return None
    except RecursionError:
        # Malformed/unprocessable client JSON (deep nesting), raised
        # directly by the stdlib parser INSIDE this bounded parsing
        # boundary: a client 400, not an internal server failure.
        # Deliberately narrow — MemoryError/BaseException/arbitrary
        # internal exceptions are never caught, and no exception
        # handling is added around the B3/B2/B1 call chain.
        return None
    if not isinstance(payload, dict):
        return None
    return payload


# --- header and Content-Type semantics ------------------------------------


def _header_values(
    headers: tuple[tuple[str, str], ...], name: str
) -> list[str]:
    """All values of header ``name`` (case-insensitive, in order).

    Only called after structural validation, so names and values are
    known to be ``str``. Duplicate headers stay visible as multiple
    entries — that is exactly why headers are a tuple of pairs.
    """
    return [
        value for header_name, value in headers
        if header_name.lower() == name
    ]


def _is_malformed_media_type(token: str) -> bool:
    """A media type is ``type "/" subtype`` of visible ASCII only."""
    stripped = token.strip()
    if not stripped or stripped.count("/") != 1:
        return True
    return any(ch <= " " or ch >= "\x7f" for ch in stripped)


def _content_type_rejection(value: str) -> int | None:
    """Classify one Content-Type header value (case-insensitive).

    ``None`` when acceptable — exactly ``application/json`` or
    ``application/json; charset=utf-8`` (tokens compared
    case-insensitively, optional surrounding whitespace tolerated);
    415 for a syntactically valid but unsupported media type,
    charset or parameter; 400 for a syntactically malformed value.
    No permissive guessing.
    """
    media_type, *parameters = value.split(";")
    if _is_malformed_media_type(media_type):
        return 400
    if media_type.strip().lower() != _JSON_MEDIA_TYPE:
        return 415
    saw_charset = False
    for parameter in parameters:
        token = parameter.strip()
        name, separator, charset = token.partition("=")
        if not token or not separator:
            # e.g. "application/json;" or ";charset=utf-8".
            return 400
        name = name.strip().lower()
        charset = charset.strip().lower()
        if not name or not charset:
            # Empty parameter name or value, e.g. "charset=".
            return 400
        if name != "charset":
            # Unsupported parameter on a supported media type.
            return 415
        if saw_charset:
            # Duplicated charset parameter: fail closed.
            return 400
        saw_charset = True
        if charset != _UTF8_CHARSET:
            # Unsupported charset, e.g. latin-1 or a quoted value.
            return 415
    return None


# --- structural request validation ----------------------------------------


def _is_malformed_header_name(name: object) -> bool:
    """Header names: non-empty ``str`` of visible ASCII only.

    CR/LF, other control characters, spaces (a trailing space must
    never smuggle a second header past an exact-name match), DEL and
    non-ASCII bytes are all malformed.
    """
    if not isinstance(name, str) or not name:
        return True
    return any(ch <= " " or ch >= "\x7f" for ch in name)


def _is_malformed_header_value(value: object) -> bool:
    """Header values: ``str`` without CR/LF/controls (HTAB allowed).

    NUL, CR, LF, other C0 controls (except HTAB) and DEL are
    malformed. Visible ASCII, space, HTAB and non-ASCII (obs-text)
    pass — values are otherwise opaque to B4.
    """
    if not isinstance(value, str):
        return True
    return any(
        (ch < " " and ch != "\t") or ch == "\x7f" for ch in value
    )


def _has_malformed_structure(request: HttpRequest) -> bool:
    """Fail-closed structural validation of the request abstraction.

    Valid structure: ``method`` and ``path`` are ``str``, ``body``
    is ``bytes``, ``headers`` is a tuple of ``(name, value)``
    2-tuples of ``str`` with well-formed names/values. Anything else
    is a malformed request → deterministic 400. No offending value
    is ever reflected (the response body is empty regardless).
    """
    if not isinstance(request.method, str):
        return True
    if not isinstance(request.path, str):
        return True
    if not isinstance(request.body, bytes):
        return True
    if not isinstance(request.headers, tuple):
        return True
    for header in request.headers:
        if not isinstance(header, tuple) or len(header) != 2:
            return True
        name, value = header
        if _is_malformed_header_name(name):
            return True
        if _is_malformed_header_value(value):
            return True
    return False


def _response(
    status: int, extra_headers: tuple[tuple[str, str], ...] = ()
) -> HttpResponse:
    """Deterministic empty-body response with ``Content-Length: 0``."""
    return HttpResponse(
        status=status,
        headers=_CONTENT_LENGTH_ZERO + extra_headers,
        body=b"",
    )


# --- the adapter -----------------------------------------------------------


class HeartbeatHttpAdapter:
    """HTTP request semantics for the heartbeat endpoint (Stage B4).

    Wraps the accepted B3 :class:`AuthenticatedHeartbeatAdapter`.
    B4 adds HTTP routing/header/body/JSON semantics only — no second
    authentication system, no wire schema validation, no persistence
    logic of its own.
    """

    def __init__(self, wire: AuthenticatedHeartbeatAdapter) -> None:
        self._wire = wire

    def handle(self, request: HttpRequest) -> HttpResponse:
        """Serve one heartbeat request; never raises on client input.

        The deterministic check order and status mapping are
        documented in the module docstring. Expected client input
        failures map to safe empty-body responses; unexpected
        internal exceptions after successful HTTP-level parsing
        propagate unchanged (never masked as 204/400/401) — the
        future B5 server boundary owns the generic 500 mapping.
        """
        if _has_malformed_structure(request):
            return _response(400)

        if request.path != HEARTBEAT_PATH:
            return _response(404)
        if request.method != HEARTBEAT_METHOD:
            return _response(405, (("Allow", HEARTBEAT_METHOD),))

        if len(request.body) > MAX_HEARTBEAT_BODY_BYTES:
            return _response(413)
        if not request.body:
            return _response(400)

        content_types = _header_values(request.headers, _CONTENT_TYPE_HEADER)
        if len(content_types) > 1:
            # Duplicate Content-Type: fail closed, deterministic 400.
            return _response(400)
        if not content_types:
            return _response(415)
        rejection = _content_type_rejection(content_types[0])
        if rejection is not None:
            return _response(rejection)

        tokens = _header_values(request.headers, _TOKEN_HEADER)
        if len(tokens) != 1:
            # Missing or duplicated token header: generic 401, no
            # reflection of any presented value.
            return _response(401)

        payload = _decode_strict_json(request.body)
        if payload is None:
            return _response(400)

        try:
            self._wire.handle(payload, tokens[0])
        except MalformedHeartbeatPayloadError:
            return _response(400)
        except HeartbeatAuthenticationError:
            return _response(401)
        return _response(204)
