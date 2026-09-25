"""HTTPS liveness probe adapter for the external dead-man (Stage H1B-1).

The bounded transport half of the frozen H0 liveness observation: one
unauthenticated ``GET`` against the configured public HTTPS Sentinel
heartbeat endpoint, converted into the accepted Stage H1A probe
boundary. The healthy-signature logic itself is NEVER duplicated
here — a real HTTP response is handed to the accepted H1A classifier
verbatim (:func:`classify_deadman_probe` over
:class:`DeadManProbeResponse`), and this adapter owns only the
request, the observation conversion and the bounded failure mapping.

Contract points (see docs/ARCHITECTURE.md section 32):

- HTTPS only: the configured URL must use the ``https`` scheme,
  carry a non-empty parsed host, no userinfo (credentials) and no
  fragment, and its authority must be well formed (a malformed
  port/authority fails the bounded configuration validation, never
  a raw parser error); it is used VERBATIM — no path is added or
  normalized, no URL is derived from the host;
- no credentials of any kind travel with the probe: no reporter
  token, no authentication headers, no request body — the probe never
  creates a heartbeat;
- redirects are never followed and are never healthy evidence: the
  production opener replaces the stdlib redirect handler with one
  that turns any 3xx into an ``HTTPError``, and a 3xx observation
  classifies as a plain failed probe;
- exactly one request attempt per ``probe()`` call with a bounded
  configurable timeout: no retry, no backoff, no sleeps, no second
  request before or after any failure;
- DNS, connect, TLS, timeout and every other expected HTTP transport
  failure maps to the FAILED :class:`DeadManProbeOutcome` — never an
  unbounded or raw transport crash; unexpected non-transport defects
  propagate unchanged;
- every Allow header value is preserved verbatim (arrival order,
  multiplicity) into the H1A observation, and only enough response
  data is read to distinguish an empty from a non-empty body (a
  single bounded read); body contents are never surfaced anywhere;
- every response-like object acquired from the transport — the
  returned response and the file-like ``HTTPError`` of the failure
  path alike — receives exactly one cleanup attempt, and a cleanup
  failure never changes an already-observed outcome (the liveness
  evidence was fully read first) and never escapes as a raw
  exception;
- the adapter owns no debounce, no state, no clock and no
  notification semantics: those stay with the accepted H1A core and
  the later H1B orchestration.
"""

from __future__ import annotations

import http.client
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import IO, Any, Protocol, cast

from hermes_sentinel.deadman import (
    DeadManProbeOutcome,
    DeadManProbeResponse,
    classify_deadman_probe,
)

__all__ = [
    "DeadManProbeSettings",
    "DeadManProber",
]

#: The bounded body sample: exactly enough response data to
#: distinguish an empty body from a non-empty one (the H1A classifier
#: only distinguishes ``body == b""``), so nothing more is ever read.
_BODY_SAMPLE_BYTES = 1

#: The header whose values the H1A classification consumes.
_ALLOW_HEADER = "Allow"


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")


def _require_https_url(url: object) -> None:
    """Fail-closed validation of the probe URL (no secret echo)."""
    if not isinstance(url, str):
        raise TypeError("url must be a str")
    if not url:
        raise ValueError("url must be a non-empty string")
    for character in url:
        if character.isspace() or ord(character) < 0x20 or character == "\x7f":
            raise ValueError(
                "url must not contain whitespace or control characters"
            )
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError as error:
        raise ValueError(f"invalid probe URL: {error}") from None
    if split.scheme != "https":
        raise ValueError(
            f"probe URL must use HTTPS, got scheme {split.scheme!r}"
        )
    if not split.hostname:
        # Parsed-hostname semantics, never a mere netloc presence
        # check: "https://:443/..." has a non-empty netloc but no
        # host at all — an empty authority is not an endpoint.
        raise ValueError(
            "probe URL must carry a non-empty host in its authority"
        )
    if split.fragment:
        raise ValueError("probe URL must not carry a fragment")
    if "@" in split.netloc:
        raise ValueError(
            "probe URL must not carry credentials (userinfo): the"
            " dead-man probe is unauthenticated"
        )
    try:
        # Accessing the parsed port validates it (numeric and in
        # range): a malformed authority fails here as the adapter's
        # bounded configuration error, never as a raw urllib
        # ValueError leaking to the caller.
        split.port
    except ValueError as error:
        raise ValueError(f"invalid probe URL port: {error}") from None


@dataclass(frozen=True, slots=True)
class DeadManProbeSettings:
    """Immutable HTTPS dead-man probe settings (Stage H1B-1).

    ``url`` is the FULL heartbeat endpoint URL (for example
    ``https://sentinel.example/v1/heartbeat`` — concrete targets are
    operator deployment configuration, never tracked repository
    facts). It must use HTTPS, carry a non-empty parsed host and no
    userinfo or fragment, contain no whitespace or control
    characters, and its authority must be well formed (a malformed
    port is a validation failure, not a raw parser error); it is
    used verbatim with no path added, derived or normalized. It is
    not a secret and may appear in repr/errors.
    ``timeout_seconds`` bounds the single request attempt and must
    be finite and strictly positive.
    """

    url: str
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        _require_https_url(self.url)
        _require_positive("timeout_seconds", self.timeout_seconds)


class ProbeResponseLike(Protocol):
    """The minimal response surface one bounded observation consumes.

    Both the ``addinfourl`` the stdlib opener returns and the
    file-like ``urllib.error.HTTPError`` raised on the HTTP failure
    path (405, redirects, 4xx/5xx) satisfy this surface.
    """

    status: int
    headers: Message

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class ProbeOpenerLike(Protocol):
    """The injectable transport seam: open exactly one request.

    The production implementation is the standard-library HTTPS
    opener below; deterministic tests inject their own double.
    """

    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> ProbeResponseLike: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    Raising inside ``redirect_request`` turns any 3xx response into
    an ``HTTPError`` inside the opener, so following a redirect can
    never issue a second outbound request and a redirect can never
    become healthy evidence. The default stdlib redirect handler
    would transparently follow redirects; building the production
    opener with this handler replaces it. (Deliberately a private
    twin of the accepted E2 handler: the dead-man shares no code path
    with the central Sentinel's Telegram transport.)
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
    redirect handler replaced by ``_NoRedirectHandler``. The opener is
    reused across ``probe()`` calls and adds no retry, backoff or
    queueing behaviour of its own.
    """

    __slots__ = ("_director",)

    def __init__(self) -> None:
        self._director = urllib.request.build_opener(_NoRedirectHandler())

    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> ProbeResponseLike:
        # The stdlib opener returns an ``addinfourl``; for the surface
        # this seam consumes it is structurally the
        # ``ProbeResponseLike`` protocol (status / headers / read /
        # close) — the same cast precedent as the accepted E2 sender.
        return cast(
            "ProbeResponseLike", self._director.open(request, timeout=timeout)
        )


def _close_quietly(resource: ProbeResponseLike) -> None:
    """Attempt exactly one cleanup of an acquired response object.

    A cleanup failure never changes an already-observed outcome: the
    liveness evidence was fully read before this point, so a failing
    close is local resource noise, not target evidence. Raw cleanup
    exceptions (which can carry the request URL) never escape the
    boundary; unrelated programmer defects propagate unchanged.
    """
    try:
        resource.close()
    except (OSError, http.client.HTTPException):
        pass


def _observe(response: ProbeResponseLike) -> DeadManProbeOutcome:
    """Convert one already-acquired response into the H1A outcome.

    Reads the status, every ``Allow`` header value verbatim
    (multiplicity and arrival order preserved by the stdlib
    ``Message.get_all``) and exactly one bounded body sample, then
    delegates to the accepted H1A classifier. Expected transport
    failures during the observation and structurally impossible
    observations both map to the FAILED outcome — never healthy
    evidence, never a raw transport crash.
    """
    try:
        status = response.status
        allow_values = response.headers.get_all(_ALLOW_HEADER) or ()
        body = response.read(_BODY_SAMPLE_BYTES)
    except (TimeoutError, OSError, http.client.HTTPException):
        return DeadManProbeOutcome.FAILED
    try:
        observation = DeadManProbeResponse(
            status=status,
            allow_headers=tuple(allow_values),
            body=body,
        )
    except ValueError:
        # A status outside the HTTP range is not a valid observation
        # at all — a malformed transport fact fails closed, it can
        # never become healthy evidence.
        return DeadManProbeOutcome.FAILED
    return classify_deadman_probe(observation)


def _observe_and_close(response: ProbeResponseLike) -> DeadManProbeOutcome:
    """Observe one response, then close it exactly once."""
    outcome = _observe(response)
    _close_quietly(response)
    return outcome


class DeadManProber:
    """Bounded HTTPS liveness prober for the external dead-man.

    One ``probe()`` call performs exactly one unauthenticated GET
    against the configured HTTPS heartbeat endpoint and returns the
    H1A-classified outcome. Every expected transport failure maps to
    ``FAILED``; a real HTTP response is classified exclusively by the
    accepted H1A boundary. The prober owns no debounce, no state, no
    clock and no notification semantics.
    """

    __slots__ = ("_opener", "_settings")

    def __init__(
        self,
        settings: DeadManProbeSettings,
        opener: ProbeOpenerLike | None = None,
    ) -> None:
        self._settings = settings
        self._opener: ProbeOpenerLike = (
            opener if opener is not None else _StdlibHttpsOpener()
        )

    def probe(self) -> DeadManProbeOutcome:
        """Perform exactly one bounded liveness probe attempt.

        Builds the single GET request (no body, no credentials, no
        custom headers) against the configured URL, opens it once
        with the configured timeout and observes the result: the
        returned response — or the file-like ``HTTPError`` the stdlib
        raises for any non-2xx/redirect status, which is itself the
        acquired response of that path — is converted into the exact
        H1A observation boundary and closed exactly once. DNS,
        connect, TLS, timeout and other expected transport failures
        return ``FAILED`` without retry.
        """
        request = urllib.request.Request(
            self._settings.url,
            method="GET",
        )
        try:
            response = self._opener.open(
                request, timeout=self._settings.timeout_seconds
            )
        except urllib.error.HTTPError as error:
            # HTTPError is itself the acquired response-like object of
            # the failure path (405, 4xx/5xx, refused redirects): it
            # owns the underlying stream and is observed and closed
            # through exactly the same mechanism as a returned
            # response.
            return _observe_and_close(cast("ProbeResponseLike", error))
        except (TimeoutError, OSError, http.client.HTTPException):
            # URLError (DNS/connect/TLS), timeouts and every other
            # expected transport-level failure are a FAILED probe,
            # never an escaping exception.
            return DeadManProbeOutcome.FAILED
        return _observe_and_close(response)
