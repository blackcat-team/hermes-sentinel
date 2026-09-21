"""Deterministic tests for the Stage E2 Telegram sender primitive.

Covers the authoritative E2 contract: the immutable TelegramSettings
shape and its secret-safe bot-token validation, the exact frozen
plain-text incident rendering, the single bounded sendMessage request
(method / fixed HTTPS origin / headers / JSON body), the fail-closed
response handling (HTTP 200 + top-level "ok" exactly true, bounded
body reads), the no-redirect / no-retry policy, the secret-safe
TelegramDeliveryError boundary and the sender-only scope. All
delivery tests run against an injectable recording transport fake —
no real Telegram calls, no real network, no sleeps, no real clock.
"""

from __future__ import annotations

import dataclasses
import email.message
import inspect
import json
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import telegram  # noqa: E402
from hermes_sentinel.domain import HostState, HostTransition  # noqa: E402
from hermes_sentinel.incidents import Incident, IncidentKind  # noqa: E402
from hermes_sentinel.telegram import (  # noqa: E402
    TelegramDeliveryError,
    TelegramSender,
    TelegramSettings,
    render_incident_message,
)

_TOKEN = "1234567890:AAbbcc_DD-eeFf0011223344556677_8"
_CHAT_ID = -1001234567890
_THREAD_ID = 12
_AT = datetime(2026, 9, 20, 12, 34, 56, tzinfo=UTC)


def _settings(**overrides: object) -> TelegramSettings:
    values: dict[str, object] = {
        "bot_token": _TOKEN,
        "chat_id": _CHAT_ID,
    }
    values.update(overrides)
    return TelegramSettings(**values)  # type: ignore[arg-type]


def _incident(
    kind: IncidentKind,
    *,
    host: str = "prod",
    from_state: HostState,
    to_state: HostState,
    at: datetime = _AT,
) -> Incident:
    return Incident(
        kind=kind,
        transition=HostTransition(
            host=host,
            from_state=from_state,
            to_state=to_state,
            at=at,
        ),
    )


def _down_incident() -> Incident:
    return _incident(
        IncidentKind.DOWN,
        from_state=HostState.DEGRADED,
        to_state=HostState.DOWN,
    )


def _recovered_incident() -> Incident:
    return _incident(
        IncidentKind.RECOVERED,
        from_state=HostState.DOWN,
        to_state=HostState.HEALTHY,
    )


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
        status,
        "Status Message",
        email.message.Message(),
        None,
    )


class _FakeResponse:
    """Deterministic response double with a bounded read.

    Optionally raises a synthetic exception from ``read``/``close``
    to exercise the cleanup and read error boundaries.
    """

    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        read_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._read_error = read_error
        self._close_error = close_error
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        if size < 0:
            return self._body
        return self._body[:size]

    def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class _RecordingOpener:
    """Transport seam double: records every open call verbatim."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[urllib.request.Request, float]] = []
        self._response = response
        self._error = error

    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> _FakeResponse:
        self.calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _ok_body() -> bytes:
    return b'{"ok": true, "result": {"message_id": 42}}'


def _secret_close_error() -> OSError:
    """Synthetic QA evidence: a close() failure carrying secrets.

    The raw message embeds the authenticated URL (and therefore the
    bot token) plus a distinctive marker proving that no part of the
    raw close exception text survives the boundary.
    """
    return OSError(
        "RAW-CLOSE-FAILURE https://api.telegram.org/bot"
        + _TOKEN
        + "/sendMessage"
    )


def _secret_stream_close_error() -> OSError:
    """Synthetic QA evidence for the HTTPError fp stream path."""
    return OSError(
        "RAW-STREAM-CLOSE-FAILURE https://api.telegram.org/bot"
        + _TOKEN
        + "/sendMessage"
    )


class _TrackingStream:
    """Real file-like body stream tracking close() attempts.

    Used as the ``fp`` of a real ``urllib.error.HTTPError`` so the
    production failure path exercises genuine stdlib close()
    semantics. A close error is one-shot so interpreter shutdown of
    an already-failed stream stays quiet.
    """

    def __init__(
        self,
        data: bytes = b"",
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._data = data
        self._close_error = close_error
        self.close_calls = 0
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if size >= 0:
            return self._data[:size]
        return self._data

    def readline(self, *args: object) -> bytes:
        return b""

    def close(self) -> None:
        self.close_calls += 1
        error = self._close_error
        if error is not None:
            self._close_error = None
            raise error


class _CloseTrackingHTTPError(urllib.error.HTTPError):
    """Real urllib.error.HTTPError counting its own close() calls."""

    def __init__(
        self,
        url: str,
        code: int,
        msg: str,
        hdrs: email.message.Message,
        fp: _TrackingStream | None,
    ) -> None:
        super().__init__(url, code, msg, hdrs, fp)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class TelegramSettingsContractTest(unittest.TestCase):
    """Immutable slotted settings with a secret-safe token field."""

    def test_is_frozen_and_slotted(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(TelegramSettings))
        settings = _settings()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.chat_id = 1  # type: ignore[misc]
        self.assertFalse(hasattr(settings, "__dict__"))

    def test_fields_are_exactly_the_contract(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(TelegramSettings)],
            [
                "bot_token",
                "chat_id",
                "message_thread_id",
                "timeout_seconds",
            ],
        )

    def test_defaults_are_none_thread_and_ten_seconds(self) -> None:
        settings = _settings()
        self.assertIsNone(settings.message_thread_id)
        self.assertEqual(settings.timeout_seconds, 10.0)

    def test_bot_token_is_absent_from_repr(self) -> None:
        text = repr(_settings(message_thread_id=_THREAD_ID))
        self.assertNotIn("bot_token", text)
        self.assertNotIn(_TOKEN, text)
        self.assertIn("chat_id", text)
        self.assertIn("message_thread_id", text)

    def test_empty_token_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _settings(bot_token="")

    def test_non_string_token_rejected(self) -> None:
        with self.assertRaises(TypeError):
            _settings(bot_token=12345)  # type: ignore[arg-type]

    def test_token_injection_rejected(self) -> None:
        cases = (
            "token with spaces",
            "token\twith\ttabs",
            "token\nwith\nnewlines",
            "carriage\rreturn",
            "null\x00byte",
            "path/../injection",
            "path//double/slash",
            "query?injection=1",
            "fragment#injection",
            "percent%2Finjection",
            "trailing/slash/",
            "semicolon;",
            "at@sign",
            "equals=sign",
            "amp&ersand",
            "unicode-tokén",
            "emoji-\U0001f6a8",
        )
        for token in cases:
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    _settings(bot_token=token)

    def test_valid_token_preserved_verbatim(self) -> None:
        token = "001234567:AAHfiqks_KZ8-WmoMTsEfBO2xtBjxsCgA4Y"
        self.assertEqual(_settings(bot_token=token).bot_token, token)

    def test_negative_chat_id_accepted(self) -> None:
        self.assertEqual(
            _settings(chat_id=-1001234567890).chat_id, -1001234567890
        )

    def test_positive_chat_id_accepted(self) -> None:
        self.assertEqual(_settings(chat_id=42).chat_id, 42)

    def test_zero_chat_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _settings(chat_id=0)

    def test_bool_chat_id_rejected(self) -> None:
        with self.assertRaises(TypeError):
            _settings(chat_id=True)  # type: ignore[arg-type]

    def test_non_int_chat_id_rejected(self) -> None:
        for value in ("-100", -100.5, None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    _settings(chat_id=value)  # type: ignore[arg-type]

    def test_none_thread_id_accepted(self) -> None:
        self.assertIsNone(_settings(message_thread_id=None).message_thread_id)

    def test_positive_thread_id_accepted(self) -> None:
        self.assertEqual(
            _settings(message_thread_id=_THREAD_ID).message_thread_id,
            _THREAD_ID,
        )

    def test_invalid_thread_ids_rejected(self) -> None:
        for value in (0, -1, -99, True, False, "5", 5.0):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    _settings(message_thread_id=value)  # type: ignore[arg-type]

    def test_timeout_must_be_finite_and_positive(self) -> None:
        for value in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _settings(timeout_seconds=value)
        self.assertEqual(_settings(timeout_seconds=0.25).timeout_seconds, 0.25)


class RenderIncidentMessageTest(unittest.TestCase):
    """The exact frozen five-line plain-text shape."""

    def test_down_renders_the_exact_frozen_shape(self) -> None:
        self.assertEqual(
            render_incident_message(_down_incident()),
            "Hermes Sentinel\n"
            "DOWN\n"
            "Host: prod\n"
            "State: degraded -> down\n"
            "At: 2026-09-20T12:34:56+00:00",
        )

    def test_recovered_renders_the_exact_frozen_shape(self) -> None:
        self.assertEqual(
            render_incident_message(_recovered_incident()),
            "Hermes Sentinel\n"
            "RECOVERED\n"
            "Host: prod\n"
            "State: down -> healthy\n"
            "At: 2026-09-20T12:34:56+00:00",
        )

    def test_down_to_degraded_renders_recovered(self) -> None:
        incident = _incident(
            IncidentKind.RECOVERED,
            host="vds-01.example.net",
            from_state=HostState.DOWN,
            to_state=HostState.DEGRADED,
        )
        self.assertEqual(
            render_incident_message(incident),
            "Hermes Sentinel\n"
            "RECOVERED\n"
            "Host: vds-01.example.net\n"
            "State: down -> degraded\n"
            "At: 2026-09-20T12:34:56+00:00",
        )

    def test_host_and_timestamp_are_projected_verbatim(self) -> None:
        # Microseconds and non-UTC offsets pass through isoformat()
        # untouched: rendering never reformats the canonical instant.
        incident = _incident(
            IncidentKind.DOWN,
            host="vds-02.example.net",
            from_state=HostState.HEALTHY,
            to_state=HostState.DOWN,
            at=datetime(
                2026,
                9,
                20,
                8,
                0,
                0,
                123456,
                tzinfo=timezone(timedelta(hours=3)),
            ),
        )
        self.assertEqual(
            render_incident_message(incident),
            "Hermes Sentinel\n"
            "DOWN\n"
            "Host: vds-02.example.net\n"
            "State: healthy -> down\n"
            "At: 2026-09-20T08:00:00.123456+03:00",
        )

    def test_render_is_deterministic(self) -> None:
        incident = _down_incident()
        self.assertEqual(
            render_incident_message(incident),
            render_incident_message(incident),
        )

    def test_render_performs_no_clock_network_or_io(self) -> None:
        source = inspect.getsource(render_incident_message)
        for forbidden in (
            "now(",
            "utcnow",
            "time(",
            "open(",
            "urlopen",
            "socket",
            "send(",
            "post(",
        ):
            self.assertNotIn(forbidden, source)
        with (
            mock.patch(
                "builtins.open", side_effect=AssertionError("I/O during render")
            ),
            mock.patch(
                "socket.socket",
                side_effect=AssertionError("network during render"),
            ),
            mock.patch(
                "time.time", side_effect=AssertionError("clock during render")
            ),
        ):
            text = render_incident_message(_down_incident())
        self.assertTrue(text.startswith("Hermes Sentinel\n"))


class SenderRequestContractTest(unittest.TestCase):
    """One send(): the exact single POST sendMessage request."""

    def setUp(self) -> None:
        self.opener = _RecordingOpener(response=_FakeResponse(200, _ok_body()))
        self.sender = TelegramSender(_settings(), opener=self.opener)

    def _sent_request(self) -> urllib.request.Request:
        self.sender.send(_down_incident())
        self.assertEqual(len(self.opener.calls), 1)
        request, _timeout = self.opener.calls[0]
        return request

    def test_send_uses_post(self) -> None:
        self.assertEqual(self._sent_request().get_method(), "POST")

    def test_request_uses_https_api_telegram_org_only(self) -> None:
        split = urllib.parse.urlsplit(self._sent_request().full_url)
        self.assertEqual(split.scheme, "https")
        self.assertEqual(split.netloc, "api.telegram.org")
        self.assertEqual(split.query, "")
        self.assertEqual(split.fragment, "")

    def test_request_targets_sendmessage_only(self) -> None:
        request = self._sent_request()
        path = urllib.parse.urlsplit(request.full_url).path
        self.assertEqual(path, f"/bot{_TOKEN}/sendMessage")
        self.assertTrue(request.full_url.startswith("https://api.telegram.org/"))

    def test_content_type_is_application_json(self) -> None:
        request = self._sent_request()
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get("content-type"), "application/json")

    def test_body_without_thread_contains_exactly_chat_id_and_text(self) -> None:
        request = self._sent_request()
        assert request.data is not None
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(set(payload), {"chat_id", "text"})
        self.assertEqual(payload["chat_id"], _CHAT_ID)
        self.assertEqual(
            payload["text"], render_incident_message(_down_incident())
        )

    def test_body_with_thread_adds_exactly_message_thread_id(self) -> None:
        opener = _RecordingOpener(response=_FakeResponse(200, _ok_body()))
        sender = TelegramSender(
            _settings(message_thread_id=_THREAD_ID), opener=opener
        )
        sender.send(_down_incident())
        self.assertEqual(len(opener.calls), 1)
        request, _timeout = opener.calls[0]
        assert request.data is not None
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            set(payload), {"chat_id", "text", "message_thread_id"}
        )
        self.assertEqual(payload["message_thread_id"], _THREAD_ID)

    def test_no_parse_mode_or_extra_telegram_parameters(self) -> None:
        for settings in (_settings(), _settings(message_thread_id=3)):
            with self.subTest(thread=settings.message_thread_id):
                opener = _RecordingOpener(
                    response=_FakeResponse(200, _ok_body())
                )
                TelegramSender(settings, opener=opener).send(_down_incident())
                request, _timeout = opener.calls[0]
                assert request.data is not None
                payload = json.loads(request.data.decode("utf-8"))
                for forbidden in (
                    "parse_mode",
                    "disable_notification",
                    "protect_content",
                    "reply_markup",
                    "disable_web_page_preview",
                    "link_preview_options",
                    "reply_to_message_id",
                    "entities",
                ):
                    self.assertNotIn(forbidden, payload)

    def test_bot_token_is_not_in_the_request_json_body(self) -> None:
        request = self._sent_request()
        assert request.data is not None
        self.assertNotIn(_TOKEN.encode("utf-8"), request.data)
        self.assertNotIn("bot_token", request.data.decode("utf-8"))

    def test_exactly_one_open_call_per_send(self) -> None:
        self.sender.send(_down_incident())
        self.assertEqual(len(self.opener.calls), 1)
        self.sender.send(_recovered_incident())
        self.assertEqual(len(self.opener.calls), 2)

    def test_configured_timeout_is_passed_to_transport(self) -> None:
        opener = _RecordingOpener(response=_FakeResponse(200, _ok_body()))
        sender = TelegramSender(
            _settings(timeout_seconds=7.5), opener=opener
        )
        sender.send(_down_incident())
        self.assertEqual(len(opener.calls), 1)
        _request, timeout = opener.calls[0]
        self.assertEqual(timeout, 7.5)


class DeliveryOutcomeTest(unittest.TestCase):
    """Fail-closed bounded response handling; no retries."""

    def _send_with(
        self,
        response: _FakeResponse | None = None,
        error: BaseException | None = None,
        settings: TelegramSettings | None = None,
    ) -> tuple[TelegramDeliveryError | None, int]:
        opener = _RecordingOpener(response=response, error=error)
        sender = TelegramSender(settings or _settings(), opener=opener)
        caught: TelegramDeliveryError | None = None
        try:
            sender.send(_down_incident())
        except TelegramDeliveryError as exc:
            caught = exc
        return caught, len(opener.calls)

    def test_http_200_and_ok_true_succeeds(self) -> None:
        caught, calls = self._send_with(response=_FakeResponse(200, _ok_body()))
        self.assertIsNone(caught)
        self.assertEqual(calls, 1)

    def test_http_status_other_than_200_fails(self) -> None:
        for error in (_http_error(500), _http_error(403), _http_error(429)):
            with self.subTest(status=error.code):
                caught, calls = self._send_with(error=error)
                self.assertIsInstance(caught, TelegramDeliveryError)
                self.assertEqual(calls, 1)

    def test_two_xx_other_than_200_fails(self) -> None:
        for status in (201, 202, 204, 299):
            with self.subTest(status=status):
                caught, calls = self._send_with(
                    response=_FakeResponse(status, _ok_body())
                )
                self.assertIsInstance(caught, TelegramDeliveryError)
                self.assertEqual(calls, 1)

    def test_redirect_fails_and_is_not_followed(self) -> None:
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                caught, calls = self._send_with(error=_http_error(status))
                self.assertIsInstance(caught, TelegramDeliveryError)
                assert caught is not None
                self.assertIn("redirect", str(caught))
                self.assertEqual(calls, 1)

    def test_ok_not_exactly_true_fails(self) -> None:
        for body in (
            b'{"ok": false, "description": "Bad Request: chat not found"}',
            b'{"ok": "true"}',
            b'{"ok": 1}',
            b"{}",
            b'{"result": {"message_id": 1}}',
        ):
            with self.subTest(body=body):
                caught, calls = self._send_with(
                    response=_FakeResponse(200, body)
                )
                self.assertIsInstance(caught, TelegramDeliveryError)
                self.assertEqual(calls, 1)

    def test_malformed_json_fails(self) -> None:
        for body in (b"not json at all", b'{"ok": tru', b""):
            with self.subTest(body=body):
                caught, calls = self._send_with(
                    response=_FakeResponse(200, body)
                )
                self.assertIsInstance(caught, TelegramDeliveryError)
                self.assertEqual(calls, 1)

    def test_non_object_json_fails(self) -> None:
        for body in (b"[1, 2]", b'"ok"', b"null", b"42"):
            with self.subTest(body=body):
                caught, _calls = self._send_with(
                    response=_FakeResponse(200, body)
                )
                self.assertIsInstance(caught, TelegramDeliveryError)

    def test_oversized_response_fails(self) -> None:
        limit = telegram._RESPONSE_BODY_LIMIT_BYTES
        oversized = b'{"ok": true, "padding": "' + b"a" * limit + b'"}'
        for body in (b"x" * (limit + 1), oversized):
            with self.subTest(length=len(body)):
                caught, calls = self._send_with(
                    response=_FakeResponse(200, body)
                )
                self.assertIsInstance(caught, TelegramDeliveryError)
                self.assertEqual(calls, 1)

    def test_body_at_exactly_the_limit_is_accepted(self) -> None:
        limit = telegram._RESPONSE_BODY_LIMIT_BYTES
        prefix = b'{"ok": true, "padding": "'
        suffix = b'"}'
        body = prefix + b"a" * (limit - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(body), limit)
        caught, _calls = self._send_with(response=_FakeResponse(200, body))
        self.assertIsNone(caught)

    def test_network_error_fails_with_delivery_error(self) -> None:
        for error in (
            urllib.error.URLError("connection refused"),
            ConnectionRefusedError(),
            OSError("network unreachable"),
        ):
            with self.subTest(error=type(error).__name__):
                caught, calls = self._send_with(error=error)
                self.assertIsInstance(caught, TelegramDeliveryError)
                self.assertEqual(calls, 1)

    def test_timeout_fails_with_delivery_error(self) -> None:
        caught, calls = self._send_with(error=TimeoutError())
        self.assertIsInstance(caught, TelegramDeliveryError)
        assert caught is not None
        self.assertIn("timed out", str(caught))
        self.assertEqual(calls, 1)

    def test_no_retry_occurs_after_any_failure(self) -> None:
        limit = telegram._RESPONSE_BODY_LIMIT_BYTES
        failures: list[tuple[str, _FakeResponse | None, BaseException | None]] = [
            ("http-500", None, _http_error(500)),
            ("redirect-302", None, _http_error(302)),
            ("ok-false", _FakeResponse(200, b'{"ok": false}'), None),
            ("malformed", _FakeResponse(200, b"nope"), None),
            ("oversized", _FakeResponse(200, b"x" * (limit + 1)), None),
            ("url-error", None, urllib.error.URLError("boom")),
            ("os-error", None, ConnectionError("reset")),
            ("timeout", None, TimeoutError()),
        ]
        for name, response, error in failures:
            with self.subTest(failure=name):
                _caught, calls = self._send_with(
                    response=response, error=error
                )
                self.assertEqual(calls, 1)


class CleanupBoundaryTest(unittest.TestCase):
    """Response cleanup never breaks the secret-safe error boundary.

    A failing close() on an otherwise-successful exchange becomes a
    bounded generic TelegramDeliveryError; a bounded delivery
    failure already determined for the exchange always wins over a
    concurrent cleanup failure; raw cleanup exceptions (which can
    carry the authenticated URL) never escape.
    """

    def _send(
        self, response: _FakeResponse
    ) -> tuple[TelegramDeliveryError | None, int, _FakeResponse]:
        opener = _RecordingOpener(response=response)
        sender = TelegramSender(_settings(), opener=opener)
        caught: TelegramDeliveryError | None = None
        try:
            sender.send(_down_incident())
        except TelegramDeliveryError as exc:
            caught = exc
        return caught, len(opener.calls), response

    def test_success_plus_failing_close_fails_bounded(self) -> None:
        # QA example A: HTTP 200 + ok=true, then close() raises.
        response = _FakeResponse(
            200, _ok_body(), close_error=_secret_close_error()
        )
        caught, calls, closed = self._send(response)
        self.assertIsInstance(caught, TelegramDeliveryError)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: response cleanup error"
        )
        self.assertEqual(calls, 1)
        self.assertEqual(closed.close_calls, 1)

    def test_cleanup_error_message_excludes_the_bot_token(self) -> None:
        response = _FakeResponse(
            200, _ok_body(), close_error=_secret_close_error()
        )
        caught, _calls, _closed = self._send(response)
        assert caught is not None
        self.assertNotIn(_TOKEN, str(caught))
        self.assertNotIn(_TOKEN.split(":", 1)[0], str(caught))

    def test_cleanup_error_message_excludes_the_authenticated_url(self) -> None:
        response = _FakeResponse(
            200, _ok_body(), close_error=_secret_close_error()
        )
        caught, _calls, _closed = self._send(response)
        assert caught is not None
        self.assertNotIn("api.telegram.org", str(caught))
        self.assertNotIn("/bot", str(caught))
        self.assertNotIn("sendMessage", str(caught))

    def test_cleanup_error_message_excludes_raw_close_exception_text(
        self,
    ) -> None:
        response = _FakeResponse(
            200, _ok_body(), close_error=_secret_close_error()
        )
        caught, _calls, _closed = self._send(response)
        assert caught is not None
        self.assertNotIn("RAW-CLOSE-FAILURE", str(caught))
        self.assertEqual(
            str(caught), "Telegram delivery failed: response cleanup error"
        )

    def test_cleanup_error_chaining_does_not_leak_the_raw_exception(
        self,
    ) -> None:
        response = _FakeResponse(
            200, _ok_body(), close_error=_secret_close_error()
        )
        caught, _calls, _closed = self._send(response)
        assert caught is not None
        self.assertIsNone(caught.__cause__)
        self.assertTrue(caught.__suppress_context__)
        if caught.__context__ is not None:
            self.assertNotIn(_TOKEN, str(caught.__context__))
            self.assertNotIn("api.telegram.org", str(caught.__context__))

    def test_http_failure_wins_over_cleanup_failure(self) -> None:
        # QA example B: the HTTP-status failure is already determined
        # when close() raises; it must remain the observed failure.
        response = _FakeResponse(
            500, b'{"ok": false}', close_error=_secret_close_error()
        )
        caught, calls, closed = self._send(response)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: HTTP status 500"
        )
        self.assertNotIn("cleanup", str(caught))
        self.assertEqual(calls, 1)
        self.assertEqual(closed.close_calls, 1)

    def test_read_timeout_wins_over_cleanup_failure(self) -> None:
        response = _FakeResponse(
            200,
            _ok_body(),
            read_error=TimeoutError("RAW-READ-FAILURE"),
            close_error=_secret_close_error(),
        )
        caught, _calls, closed = self._send(response)
        assert caught is not None
        self.assertEqual(str(caught), "Telegram delivery failed: timed out")
        self.assertNotIn("RAW-READ-FAILURE", str(caught))
        self.assertEqual(closed.close_calls, 1)

    def test_read_network_error_wins_over_cleanup_failure(self) -> None:
        response = _FakeResponse(
            200,
            _ok_body(),
            read_error=ConnectionResetError(
                "RAW-READ-FAILURE " + _TOKEN
            ),
            close_error=_secret_close_error(),
        )
        caught, _calls, closed = self._send(response)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: network error"
        )
        self.assertNotIn(_TOKEN, str(caught))
        self.assertEqual(closed.close_calls, 1)

    def test_malformed_response_wins_over_cleanup_failure(self) -> None:
        # QA example C: the malformed-response failure is already
        # determined when close() raises; it must be preserved.
        response = _FakeResponse(
            200, b"not json", close_error=_secret_close_error()
        )
        caught, _calls, closed = self._send(response)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: malformed response body"
        )
        self.assertEqual(closed.close_calls, 1)

    def test_invalid_ok_response_wins_over_cleanup_failure(self) -> None:
        response = _FakeResponse(
            200, b'{"ok": false}', close_error=_secret_close_error()
        )
        caught, _calls, closed = self._send(response)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: response ok is not true"
        )
        self.assertEqual(closed.close_calls, 1)

    def test_close_is_attempted_exactly_once(self) -> None:
        scenarios = [
            _FakeResponse(200, _ok_body()),
            _FakeResponse(200, _ok_body(), close_error=_secret_close_error()),
            _FakeResponse(500, b"x", close_error=_secret_close_error()),
            _FakeResponse(200, b"nope", close_error=_secret_close_error()),
            _FakeResponse(
                200, b"x" * (telegram._RESPONSE_BODY_LIMIT_BYTES + 1)
            ),
        ]
        for response in scenarios:
            with self.subTest(status=response.status):
                _caught, _calls, closed = self._send(response)
                self.assertEqual(closed.close_calls, 1)

    def test_cleanup_failure_introduces_no_retry(self) -> None:
        for response in (
            _FakeResponse(200, _ok_body(), close_error=_secret_close_error()),
            _FakeResponse(500, b"x", close_error=_secret_close_error()),
            _FakeResponse(200, b"nope", close_error=_secret_close_error()),
        ):
            with self.subTest(status=response.status):
                _caught, calls, _closed = self._send(response)
                self.assertEqual(calls, 1)

    def test_successful_exchange_with_clean_close_still_succeeds(self) -> None:
        response = _FakeResponse(200, _ok_body())
        caught, calls, closed = self._send(response)
        self.assertIsNone(caught)
        self.assertEqual(calls, 1)
        self.assertEqual(closed.close_calls, 1)


class HTTPErrorCleanupTest(unittest.TestCase):
    """The file-like HTTPError failure path receives one cleanup.

    Production urllib delivers HTTP failures — 4xx/5xx and the
    no-redirect 3xx path — as urllib.error.HTTPError, which is
    itself a closeable response object owning the underlying
    stream. These tests use REAL HTTPError objects with tracking
    fp streams (never _FakeResponse for the primary regression).
    """

    def _http_error(
        self, status: int, stream: _TrackingStream | None
    ) -> _CloseTrackingHTTPError:
        return _CloseTrackingHTTPError(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            status,
            "Status Message",
            email.message.Message(),
            stream,
        )

    def _send(
        self, error: urllib.error.HTTPError
    ) -> tuple[TelegramDeliveryError | None, int]:
        opener = _RecordingOpener(error=error)
        sender = TelegramSender(_settings(), opener=opener)
        caught: TelegramDeliveryError | None = None
        try:
            sender.send(_down_incident())
        except TelegramDeliveryError as exc:
            caught = exc
        return caught, len(opener.calls)

    def test_http_error_close_called_exactly_once_and_stream_closed(
        self,
    ) -> None:
        # QA example A: HTTPError(500), close() succeeds.
        stream = _TrackingStream(b"SECRET-HTTP-BODY")
        error = self._http_error(500, stream)
        caught, calls = self._send(error)
        self.assertIsInstance(caught, TelegramDeliveryError)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: HTTP status 500"
        )
        self.assertEqual(calls, 1)
        self.assertEqual(error.close_calls, 1)
        self.assertEqual(stream.close_calls, 1)

    def test_http_error_close_failure_preserves_primary_error(self) -> None:
        # QA example B: HTTPError(500), close() raises a
        # secret-bearing OSError through the underlying stream.
        stream = _TrackingStream(
            b"SECRET-HTTP-BODY", close_error=_secret_stream_close_error()
        )
        error = self._http_error(500, stream)
        caught, calls = self._send(error)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: HTTP status 500"
        )
        self.assertEqual(calls, 1)
        self.assertEqual(error.close_calls, 1)
        self.assertEqual(stream.close_calls, 1)

    def test_http_error_cleanup_failure_leaks_no_secrets(self) -> None:
        stream = _TrackingStream(
            b"SECRET-HTTP-BODY", close_error=_secret_stream_close_error()
        )
        error = self._http_error(500, stream)
        caught, _calls = self._send(error)
        assert caught is not None
        message = str(caught)
        self.assertNotIn(_TOKEN, message)
        self.assertNotIn(_TOKEN.split(":", 1)[0], message)
        self.assertNotIn("api.telegram.org", message)
        self.assertNotIn("/bot", message)
        self.assertNotIn("RAW-STREAM-CLOSE-FAILURE", message)
        self.assertNotIn("SECRET-HTTP-BODY", message)

    def test_http_error_cleanup_chaining_is_secret_safe(self) -> None:
        stream = _TrackingStream(
            b"SECRET-HTTP-BODY", close_error=_secret_stream_close_error()
        )
        error = self._http_error(500, stream)
        caught, _calls = self._send(error)
        assert caught is not None
        self.assertIsNone(caught.__cause__)
        self.assertTrue(caught.__suppress_context__)
        context = caught.__context__
        if context is not None:
            self.assertNotIn(_TOKEN, str(context))
            self.assertNotIn("api.telegram.org", str(context))
            self.assertNotIn("RAW-STREAM-CLOSE-FAILURE", str(context))
            self.assertNotIn("SECRET-HTTP-BODY", str(context))

    def test_redirect_http_error_cleanup_once_no_follow_one_attempt(
        self,
    ) -> None:
        # QA example C: the no-redirect 3xx path also produces a
        # closeable HTTPError; it is closed exactly once and never
        # followed.
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                stream = _TrackingStream(b"SECRET-HTTP-BODY")
                error = self._http_error(status, stream)
                caught, calls = self._send(error)
                assert caught is not None
                self.assertIn("redirect", str(caught))
                self.assertEqual(calls, 1)
                self.assertEqual(error.close_calls, 1)
                self.assertEqual(stream.close_calls, 1)

    def test_redirect_http_error_cleanup_failure_suppressed(self) -> None:
        stream = _TrackingStream(
            b"SECRET-HTTP-BODY", close_error=_secret_stream_close_error()
        )
        error = self._http_error(302, stream)
        caught, calls = self._send(error)
        assert caught is not None
        self.assertEqual(
            str(caught),
            "Telegram delivery failed: unexpected redirect (HTTP 302)",
        )
        self.assertNotIn(_TOKEN, str(caught))
        self.assertNotIn("RAW-STREAM-CLOSE-FAILURE", str(caught))
        self.assertEqual(calls, 1)
        self.assertEqual(error.close_calls, 1)

    def test_http_error_body_is_never_read_for_diagnostics(self) -> None:
        stream = _TrackingStream(b"SECRET-HTTP-BODY")
        error = self._http_error(403, stream)
        caught, _calls = self._send(error)
        assert caught is not None
        self.assertEqual(
            str(caught), "Telegram delivery failed: HTTP status 403"
        )
        # The failure body is neither read nor surfaced.
        self.assertEqual(stream.read_calls, 0)
        self.assertEqual(stream.close_calls, 1)

    def test_http_error_path_introduces_no_retry(self) -> None:
        scenarios = [
            self._http_error(500, _TrackingStream()),
            self._http_error(
                302,
                _TrackingStream(close_error=_secret_stream_close_error()),
            ),
            self._http_error(
                403,
                _TrackingStream(b"SECRET-HTTP-BODY"),
            ),
        ]
        for error in scenarios:
            with self.subTest(status=error.code):
                _caught, calls = self._send(error)
                self.assertEqual(calls, 1)


class SecretSafetyTest(unittest.TestCase):
    """Delivery failures never leak the token or the response body."""

    def _failure_messages(self) -> list[str]:
        limit = telegram._RESPONSE_BODY_LIMIT_BYTES
        scenarios: list[
            tuple[_FakeResponse | None, BaseException | None]
        ] = [
            # Adversarial raw exceptions whose own text embeds the
            # authenticated URL / token: none of it may survive.
            (None, _http_error(500)),
            (None, _http_error(302)),
            (
                None,
                urllib.error.URLError(
                    "getaddrinfo failed for " + _TOKEN + " example"
                ),
            ),
            (None, TimeoutError()),
            (
                _FakeResponse(
                    200,
                    b'{"ok": false, "description": "SECRET-BODY '
                    + _TOKEN.encode("utf-8")
                    + b'"}',
                ),
                None,
            ),
            (_FakeResponse(200, b"SECRET-BODY not json"), None),
            (
                _FakeResponse(200, b"SECRET-BODY " + b"x" * (limit + 1)),
                None,
            ),
            (_FakeResponse(404, b"SECRET-BODY forbidden"), None),
        ]
        messages: list[str] = []
        for response, error in scenarios:
            opener = _RecordingOpener(response=response, error=error)
            sender = TelegramSender(_settings(), opener=opener)
            with self.assertRaises(TelegramDeliveryError) as caught:
                sender.send(_down_incident())
            messages.append(str(caught.exception))
        return messages

    def test_failure_messages_exclude_the_bot_token(self) -> None:
        for message in self._failure_messages():
            self.assertNotIn(_TOKEN, message)
            self.assertNotIn(_TOKEN.split(":", 1)[0], message)

    def test_failure_messages_exclude_the_response_body(self) -> None:
        for message in self._failure_messages():
            self.assertNotIn("SECRET-BODY", message)
            self.assertNotIn("message_id", message)
            self.assertNotIn("description", message)

    def test_failure_messages_exclude_the_authenticated_url(self) -> None:
        for message in self._failure_messages():
            self.assertNotIn("api.telegram.org", message)
            self.assertNotIn("/bot", message)

    def test_failure_messages_are_concise_prefixed_sentences(self) -> None:
        for message in self._failure_messages():
            self.assertTrue(message.startswith("Telegram delivery failed: "))
            self.assertLessEqual(len(message), 80)

    def test_unsafe_exception_chaining_is_suppressed(self) -> None:
        opener = _RecordingOpener(error=_http_error(500))
        sender = TelegramSender(_settings(), opener=opener)
        with self.assertRaises(TelegramDeliveryError) as caught:
            sender.send(_down_incident())
        self.assertTrue(caught.exception.__suppress_context__)


class ProductionTransportTest(unittest.TestCase):
    """The default stdlib opener explicitly refuses redirects."""

    def test_default_sender_uses_the_stdlib_opener(self) -> None:
        sender = TelegramSender(_settings())
        self.assertIsInstance(sender._opener, telegram._StdlibHttpsOpener)

    def test_production_opener_has_redirects_disabled(self) -> None:
        opener = telegram._StdlibHttpsOpener()
        redirect_handlers = [
            handler
            for handler in opener._director.handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsInstance(
            redirect_handlers[0], telegram._NoRedirectHandler
        )

    def test_production_redirect_handler_refuses_to_follow(self) -> None:
        handler = telegram._NoRedirectHandler()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                email.message.Message(),
                "https://attacker.example/",
            )
        self.assertEqual(caught.exception.code, 302)


class StageBoundariesTest(unittest.TestCase):
    """E2 stays a bounded sender-only primitive."""

    _MODULE_SOURCE = inspect.getsource(telegram)

    def test_public_surface_is_bounded(self) -> None:
        self.assertEqual(
            sorted(telegram.__all__),
            [
                "TelegramDeliveryError",
                "TelegramSender",
                "TelegramSettings",
                "render_incident_message",
            ],
        )

    def test_no_inbound_update_or_receiving_semantics(self) -> None:
        lowered = self._MODULE_SOURCE.lower()
        for forbidden in (
            "getupdates",
            "get_updates",
            "webhook",
            "poll",
            "editmessage",
            "deletemessage",
            "setmycommands",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_no_print_or_logging(self) -> None:
        self.assertNotIn("print(", self._MODULE_SOURCE)
        lowered = self._MODULE_SOURCE.lower()
        self.assertNotIn("logging", lowered)
        self.assertNotIn("logger", lowered)

    def test_module_imports_no_async_persistence_or_config_loading(self) -> None:
        imported = {
            name
            for name, module in inspect.getmembers(telegram, inspect.ismodule)
        }
        for forbidden in (
            "asyncio",
            "sqlite3",
            "threading",
            "sched",
            "queue",
            "os",
            "environ",
        ):
            self.assertNotIn(forbidden, imported)

    def test_no_environment_or_secret_file_access(self) -> None:
        # Precise access spellings: the module prose may legitimately
        # use the word "environment", only actual access is banned.
        for forbidden in (
            "os.environ",
            "environ[",
            "environ.get",
            "getenv",
            "getpass",
            "keyring",
        ):
            self.assertNotIn(forbidden, self._MODULE_SOURCE)


if __name__ == "__main__":
    unittest.main()
