"""Telegram sender primitive (Stage E2).

A deliberately small, bounded delivery primitive: render the accepted
Stage E1 ``Incident`` as one deterministic plain-text message and
perform exactly ONE outbound Telegram Bot API ``sendMessage``
attempt. E2 owns Telegram delivery settings, the frozen message
rendering, the single bounded request/response exchange and the
secret-safe failure boundary — nothing else.

Contract points (see docs/ARCHITECTURE.md section 23):

- the accepted E1 ``Incident`` is the sole notification input;
  incident semantics are never duplicated here;
- the Sentinel Telegram bot is sender-only: this module never
  fetches inbound updates, never registers inbound endpoints and
  never runs a receiving loop;
- the Telegram API origin is fixed (``https://api.telegram.org``)
  with no configurable API base URL; ``sendMessage`` is the only
  Telegram method invoked;
- ``TelegramSettings`` is immutable; the bot token is a secret
  validated against a conservative token alphabet so it can only
  ever occupy one safe URL path component, and it is excluded from
  the dataclass repr;
- ``render_incident_message`` is pure: the exact frozen five-line
  plain-text shape, no parse mode, no clock, no I/O;
- one ``send()`` performs at most one HTTP request attempt: one
  POST with a deterministic UTF-8 JSON body of exactly ``chat_id``
  and ``text`` (plus ``message_thread_id`` only when configured);
  redirect following is explicitly disabled and a 3xx response is
  a delivery failure;
- success requires HTTP 200 AND a bounded response body that
  parses as JSON with top-level ``"ok"`` exactly ``true``; the
  returned message object is neither required nor persisted;
- expected delivery failures raise ``TelegramDeliveryError`` with
  concise messages that never contain the bot token, the
  authenticated request URL or any part of the response body;
  every response-like object acquired from the transport — the
  returned response and the file-like ``HTTPError`` raised on the
  HTTP failure path alike — receives exactly one cleanup attempt,
  and cleanup is equally bounded: an expected cleanup failure
  never escapes as a raw transport exception and never replaces a
  delivery failure already determined for the exchange;
- there are no retries, no backoff, no queue and no persistence;
  runtime orchestration belongs to later stages.
"""

from __future__ import annotations

import http.client
import json
import math
import string
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import IO, Any, Protocol, cast, overload

from hermes_sentinel.incidents import Incident, IncidentKind

__all__ = [
    "TelegramDeliveryError",
    "TelegramSender",
    "TelegramSettings",
    "render_incident_message",
]

_API_ORIGIN = "https://api.telegram.org"
_API_METHOD = "sendMessage"
_RESPONSE_BODY_LIMIT_BYTES = 8192
_TOKEN_ALPHABET = frozenset(string.ascii_letters + string.digits + ":-_")


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")


def _require_bot_token(token: object) -> None:
    # The message never echoes the token: a rejected secret must not
    # travel back inside its own rejection text.
    if not isinstance(token, str):
        raise TypeError("bot_token must be a str")
    if not token:
        raise ValueError("bot_token must be a non-empty string")
    if not _TOKEN_ALPHABET.issuperset(token):
        raise ValueError(
            "bot_token contains characters outside the conservative "
            "Telegram token alphabet"
        )


def _require_chat_id(chat_id: object) -> None:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise TypeError("chat_id must be an integer (bool is not valid)")
    if chat_id == 0:
        raise ValueError(
            "chat_id must be non-zero (negative chat ids address "
            "Telegram groups and supergroups)"
        )


def _require_message_thread_id(thread_id: object) -> None:
    if thread_id is None:
        return
    if isinstance(thread_id, bool) or not isinstance(thread_id, int):
        raise TypeError(
            "message_thread_id must be an integer or None (bool is not valid)"
        )
    if thread_id < 1:
        raise ValueError("message_thread_id must be a positive integer")


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    """Immutable Telegram delivery settings (Stage E2).

    ``bot_token`` is a secret: it is excluded from the dataclass
    repr and must never surface in application error text. It is
    validated against a conservative token alphabet (ASCII letters,
    digits, ``:``, ``_`` and ``-`` — the documented shape of Telegram
    bot tokens) so whitespace, control characters, path separators,
    query/fragment markers and percent escapes are all rejected up
    front and the token can only ever occupy one safe URL path
    component. It is used verbatim, never stripped or normalized.

    ``chat_id`` may be negative (Telegram groups/supergroups) but is
    never zero and never a bool. ``message_thread_id`` is ``None`` or
    a positive integer. No environment parsing or secret loading
    happens here; runtime configuration wiring belongs to a later
    unit.
    """

    bot_token: str = field(repr=False)
    chat_id: int
    message_thread_id: int | None = None
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        _require_bot_token(self.bot_token)
        _require_chat_id(self.chat_id)
        _require_message_thread_id(self.message_thread_id)
        _require_positive("timeout_seconds", self.timeout_seconds)


def render_incident_message(incident: Incident) -> str:
    """Render the accepted E1 incident as its exact frozen plain text.

    The five-line shape is fixed for every incident kind (example):

    ::

        Hermes Sentinel
        DOWN
        Host: prod
        State: degraded -> down
        At: 2026-09-20T12:34:56+00:00

    Every fact is projected from the canonical ``Incident``: the kind
    line, ``incident.host``, ``incident.from_state.value``,
    ``incident.to_state.value`` and ``incident.at.isoformat()``.
    There is no parse mode, no escaping framework, no emoji, no
    summary field, no truncation, no wall-clock read and no I/O —
    the E1 ``Incident`` remains the canonical event source.
    """
    if incident.kind is IncidentKind.DOWN:
        kind_line = "DOWN"
    elif incident.kind is IncidentKind.RECOVERED:
        kind_line = "RECOVERED"
    else:
        # IncidentKind is closed at exactly two members; valid
        # incidents can never reach this branch.
        raise ValueError(f"unknown incident kind: {incident.kind!r}")
    return "\n".join(
        (
            "Hermes Sentinel",
            kind_line,
            f"Host: {incident.host}",
            f"State: {incident.from_state.value} -> {incident.to_state.value}",
            f"At: {incident.at.isoformat()}",
        )
    )


class TelegramDeliveryError(Exception):
    """A bounded Telegram delivery failure.

    Concise and secret-safe by construction: the message never
    contains the bot token, the authenticated request URL or any
    part of the response body.
    """


class ResponseLike(Protocol):
    """The minimal response surface one bounded attempt consumes."""

    status: int

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class CloseableLike(Protocol):
    """The minimal cleanup surface of an acquired response object.

    Both the response returned by the transport and the file-like
    ``urllib.error.HTTPError`` raised on the HTTP failure path own
    underlying response resources and satisfy this surface.
    """

    def close(self) -> None: ...


class OpenerLike(Protocol):
    """The injectable transport seam: open exactly one request.

    The production implementation is the standard-library HTTPS
    opener below; deterministic tests inject their own double.
    """

    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> ResponseLike: ...


def _http_failure_reason(status: int) -> str:
    if 300 <= status < 400:
        return f"unexpected redirect (HTTP {status})"
    return f"HTTP status {status}"


@overload
def _close_bounded_response(
    resource: CloseableLike, failure: TelegramDeliveryError
) -> TelegramDeliveryError: ...


@overload
def _close_bounded_response(
    resource: CloseableLike, failure: TelegramDeliveryError | None
) -> TelegramDeliveryError | None: ...


def _close_bounded_response(
    resource: CloseableLike,
    failure: TelegramDeliveryError | None,
) -> TelegramDeliveryError | None:
    """Attempt exactly one cleanup of an acquired response object.

    The single coherent cleanup mechanism for every response-like
    object acquired from the transport — the returned response and
    the file-like ``HTTPError`` raised on the HTTP failure path
    alike. Returns the bounded failure that must be raised: the
    primary ``failure`` when one is already determined (an expected
    cleanup failure is then suppressed), or a generic bounded
    cleanup error when an expected cleanup failure hits an
    otherwise-successful exchange. Raw cleanup exceptions never
    escape through this helper (they can carry the authenticated
    URL); unrelated programmer defects propagate unchanged.
    """
    try:
        resource.close()
    except (OSError, http.client.HTTPException):
        if failure is None:
            return TelegramDeliveryError(
                "Telegram delivery failed: response cleanup error"
            )
    return failure


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    Raising inside ``redirect_request`` turns any 3xx response into
    an ``HTTPError`` inside the opener, so following a redirect can
    never issue a second outbound request. The default stdlib
    redirect handler would transparently follow redirects; building
    the production opener with this handler replaces it.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes] | None,
        code: int,
        msg: str,
        headers: Any,
        newurl: Any,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class _StdlibHttpsOpener:
    """Production transport: standard-library HTTPS only.

    Built from ``urllib.request.build_opener`` with the default
    redirect handler replaced by ``_NoRedirectHandler``. The opener
    is reused across ``send()`` calls and adds no retry, backoff or
    queueing behaviour of its own.
    """

    __slots__ = ("_director",)

    def __init__(self) -> None:
        self._director = urllib.request.build_opener(_NoRedirectHandler())

    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> ResponseLike:
        # The stdlib opener returns an ``addinfourl``; for the surface
        # this seam consumes it is structurally the ``ResponseLike``
        # protocol (status / read / close).
        return cast(
            "ResponseLike", self._director.open(request, timeout=timeout)
        )


def _render_json_body(message: str, settings: TelegramSettings) -> bytes:
    """Render the deterministic UTF-8 JSON request body.

    Exactly ``chat_id`` and ``text`` — plus ``message_thread_id``
    only when it is configured. No parse mode and no other Telegram
    parameter is ever added; the bot token belongs only to the
    authenticated URL path and is never part of the body.
    """
    document: dict[str, object] = {
        "chat_id": settings.chat_id,
        "text": message,
    }
    if settings.message_thread_id is not None:
        document["message_thread_id"] = settings.message_thread_id
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _classify_bounded_response(
    status: int, raw: bytes
) -> TelegramDeliveryError | None:
    """Classify the already-read bounded exchange; ``None`` = delivered.

    Pure classification of the status line and the bounded body —
    no I/O, no cleanup, and nothing that raises past the bounded
    delivery taxonomy.
    """
    if status != 200:
        return TelegramDeliveryError(
            f"Telegram delivery failed: {_http_failure_reason(status)}"
        )
    if len(raw) > _RESPONSE_BODY_LIMIT_BYTES:
        return TelegramDeliveryError(
            "Telegram delivery failed: response body too large"
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError:
        # ValueError covers json.JSONDecodeError and
        # UnicodeDecodeError alike.
        return TelegramDeliveryError(
            "Telegram delivery failed: malformed response body"
        )
    if not isinstance(document, dict):
        return TelegramDeliveryError(
            "Telegram delivery failed: malformed response body"
        )
    if document.get("ok") is not True:
        return TelegramDeliveryError(
            "Telegram delivery failed: response ok is not true"
        )
    return None


class TelegramSender:
    """Bounded sender-only Telegram notifier.

    One ``send(incident)`` call renders the incident, performs at
    most one outbound ``sendMessage`` attempt through the opener
    seam, and returns quietly only on the full success contract:
    HTTP 200 AND a bounded response body whose top-level ``"ok"`` is
    exactly ``true``. Every expected delivery failure raises
    ``TelegramDeliveryError``; there are no retries, no queue and no
    persistence.
    """

    __slots__ = ("_opener", "_settings")

    def __init__(
        self,
        settings: TelegramSettings,
        opener: OpenerLike | None = None,
    ) -> None:
        self._settings = settings
        self._opener: OpenerLike = (
            opener if opener is not None else _StdlibHttpsOpener()
        )

    def send(self, incident: Incident) -> None:
        """Deliver exactly one notification attempt for the incident.

        Renders the frozen plain-text message, performs exactly one
        POST to the fixed ``sendMessage`` endpoint and fails closed
        with ``TelegramDeliveryError`` on any non-200 status, any
        redirect, any network/timeout failure, any malformed,
        oversized or non-``ok`` response, and any expected response
        cleanup failure. A bounded failure already determined for
        the exchange always wins over a concurrent cleanup failure.
        """
        request = self._build_request(render_incident_message(incident))
        try:
            response = self._opener.open(
                request, timeout=self._settings.timeout_seconds
            )
        except urllib.error.HTTPError as error:
            # HTTPError is itself the acquired response-like object
            # of the failure path: it owns the underlying stream and
            # receives exactly one cleanup attempt before the
            # bounded error escapes. Its body is never read and the
            # raw error is never stringified.
            reason = _http_failure_reason(error.code)
            bounded = _close_bounded_response(
                error,
                TelegramDeliveryError(f"Telegram delivery failed: {reason}"),
            )
            raise bounded from None
        except TimeoutError:
            raise TelegramDeliveryError(
                "Telegram delivery failed: timed out"
            ) from None
        except (OSError, http.client.HTTPException):
            # URLError (DNS/connect/TLS) and every other expected
            # transport-level failure land here; the raw exception
            # text is deliberately not forwarded because it can
            # carry the authenticated URL.
            raise TelegramDeliveryError(
                "Telegram delivery failed: network error"
            ) from None

        # Classify the bounded exchange first; nothing below may
        # leak a raw transport exception past the boundary.
        failure: TelegramDeliveryError | None
        try:
            status = response.status
            raw = response.read(_RESPONSE_BODY_LIMIT_BYTES + 1)
        except TimeoutError:
            failure = TelegramDeliveryError(
                "Telegram delivery failed: timed out"
            )
        except (OSError, http.client.HTTPException):
            failure = TelegramDeliveryError(
                "Telegram delivery failed: network error"
            )
        else:
            failure = _classify_bounded_response(status, raw)

        # The same coherent cleanup mechanism closes the acquired
        # response: the bounded failure already determined for the
        # exchange always wins over a cleanup failure.
        failure = _close_bounded_response(response, failure)
        if failure is not None:
            raise failure from None

    def _build_request(self, message: str) -> urllib.request.Request:
        """Build the single POST sendMessage request.

        The bot token appears only here, as the authenticated path
        component of the fixed Telegram API origin.
        """
        url = f"{_API_ORIGIN}/bot{self._settings.bot_token}/{_API_METHOD}"
        return urllib.request.Request(
            url,
            data=_render_json_body(message, self._settings),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
