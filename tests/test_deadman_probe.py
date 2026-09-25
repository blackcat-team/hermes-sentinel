"""Deterministic tests for the Stage H1B-1 dead-man HTTPS probe.

Covers the authoritative H1B-1 probe contract: the immutable
HTTPS-only settings shape (scheme/authority/userinfo/whitespace
validation), the exact single GET request (no body, no credentials,
no custom headers, configured timeout), the conversion of a real
HTTP response into the exact H1A observation boundary (all Allow
values verbatim with multiplicity, a single bounded body sample),
delegation to the accepted H1A classifier, the redirect-is-never-
healthy policy, the DNS/TLS/timeout/transport failure mapping to
FAILED outcomes, the exactly-once bounded cleanup and the H1B-1
architectural boundaries. All probe tests run against an injectable
recording transport fake or real urllib.error.HTTPError objects —
no real network, no sleeps, no real clock.
"""

from __future__ import annotations

import dataclasses
import email.message
import inspect
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import deadman_probe  # noqa: E402
from hermes_sentinel.deadman import (  # noqa: E402
    DeadManProbeOutcome,
    DeadManProbeResponse,
)
from hermes_sentinel.deadman_probe import (  # noqa: E402
    DeadManProber,
    DeadManProbeSettings,
)

_URL = "https://sentinel.example/v1/heartbeat"


def _headers(*allow_values: str) -> email.message.Message:
    """Real stdlib header message carrying the given Allow values."""
    message = email.message.Message()
    for value in allow_values:
        message["Allow"] = value
    return message


class _FakeResponse:
    """Deterministic response double with a bounded read.

    Records every read size (to prove the single bounded sample) and
    every close attempt; optionally raises a synthetic exception from
    ``read``/``close`` to exercise the read and cleanup boundaries.
    """

    def __init__(
        self,
        status: int,
        headers: email.message.Message,
        body: bytes = b"",
        *,
        read_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = headers
        self._body = body
        self._read_error = read_error
        self._close_error = close_error
        self.read_sizes: list[int] = []
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        self.read_sizes.append(size)
        if size < 0:
            return self._body
        return self._body[:size]

    def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class _TrackingStream:
    """Real file-like body stream tracking read/close attempts.

    Used as the ``fp`` of a real ``urllib.error.HTTPError`` so the
    production failure path exercises genuine stdlib read/close
    semantics.
    """

    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.read_sizes: list[int] = []
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            return self._data
        return self._data[:size]

    def readline(self, *args: object) -> bytes:
        return b""

    def close(self) -> None:
        self.close_calls += 1


def _http_error(
    status: int,
    headers: email.message.Message,
    stream: _TrackingStream,
) -> urllib.error.HTTPError:
    """A real production-shaped HTTPError owning a real fp stream."""
    return urllib.error.HTTPError(
        _URL, status, "Status Message", headers, stream
    )


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


class DeadManProbeSettingsContractTest(unittest.TestCase):
    """Immutable settings with HTTPS/authority/credential validation."""

    def test_is_frozen_and_slotted(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(DeadManProbeSettings))
        settings = DeadManProbeSettings(url=_URL)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.url = "https://other.example/"  # type: ignore[misc]
        self.assertFalse(hasattr(settings, "__dict__"))

    def test_fields_are_exactly_the_contract(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(DeadManProbeSettings)],
            ["url", "timeout_seconds"],
        )

    def test_default_timeout_is_ten_seconds(self) -> None:
        self.assertEqual(
            DeadManProbeSettings(url=_URL).timeout_seconds, 10.0
        )

    def test_valid_url_is_preserved_verbatim(self) -> None:
        url = "https://sentinel.example/v1/heartbeat"
        self.assertEqual(DeadManProbeSettings(url=url).url, url)

    def test_uppercase_scheme_is_still_https(self) -> None:
        # urlsplit lowercases the scheme for the comparison; the URL
        # itself stays verbatim for the request.
        settings = DeadManProbeSettings(url="HTTPS://sentinel.example/")
        self.assertEqual(settings.url, "HTTPS://sentinel.example/")

    def test_non_https_scheme_rejected(self) -> None:
        for url in (
            "http://sentinel.example/v1/heartbeat",
            "ftp://sentinel.example/",
            "file:///etc/hostname",
            "sentinel.example/v1/heartbeat",
            "//sentinel.example/v1/heartbeat",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DeadManProbeSettings(url=url)

    def test_missing_authority_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManProbeSettings(url="https:///v1/heartbeat")

    def test_userinfo_credentials_rejected(self) -> None:
        for url in (
            "https://user:pass@sentinel.example/v1/heartbeat",
            "https://token@sentinel.example/v1/heartbeat",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DeadManProbeSettings(url=url)

    def test_whitespace_and_control_characters_rejected(self) -> None:
        for url in (
            "https://sentinel.example /v1/heartbeat",
            "https://sentinel.example/v1/heart beat",
            "https://sentinel.example/\t",
            "https://sentinel.example/\n",
            "https://sentinel.example/\r\n",
            "https://sentinel.example/\x00",
            "https://sentinel.example/\x7f",
        ):
            with self.subTest(url=repr(url)):
                with self.assertRaises(ValueError):
                    DeadManProbeSettings(url=url)

    def test_empty_and_non_string_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeadManProbeSettings(url="")
        with self.assertRaises(TypeError):
            DeadManProbeSettings(url=12345)  # type: ignore[arg-type]

    def test_timeout_must_be_finite_and_positive(self) -> None:
        for value in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DeadManProbeSettings(url=_URL, timeout_seconds=value)
        self.assertEqual(
            DeadManProbeSettings(url=_URL, timeout_seconds=0.25).timeout_seconds,
            0.25,
        )


class ProbeUrlEndpointValidationTest(unittest.TestCase):
    """The probe target must be an unambiguous HTTPS endpoint.

    Regression coverage for the QA finding: parsed-hostname
    semantics (not a mere netloc presence check), fragment
    rejection and bounded malformed-authority/port failures.
    """

    def test_qa_repro_empty_hostname_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            DeadManProbeSettings(url="https://:443/v1/heartbeat")
        self.assertIn("non-empty host", str(caught.exception))

    def test_empty_hostname_variants_rejected(self) -> None:
        for url in (
            "https://:/v1/heartbeat",
            "https://:443/",
            "https://user@/v1/heartbeat",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DeadManProbeSettings(url=url)

    def test_qa_repro_fragment_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            DeadManProbeSettings(
                url="https://sentinel.example/v1/heartbeat#extra"
            )
        self.assertIn("fragment", str(caught.exception))

    def test_fragment_variants_rejected(self) -> None:
        for url in (
            "https://sentinel.example/#frag",
            "https://sentinel.example:443/v1/heartbeat#x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DeadManProbeSettings(url=url)

    def test_malformed_port_fails_bounded(self) -> None:
        for url in (
            "https://sentinel.example:notaport/v1/heartbeat",
            "https://sentinel.example:443:80/v1/heartbeat",
            "https://sentinel.example:-1/v1/heartbeat",
            "https://sentinel.example:99999/v1/heartbeat",
            "https://[::1]:notaport/v1/heartbeat",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    DeadManProbeSettings(url=url)
                # The adapter's bounded configuration error, never a
                # raw urllib exception class or unlabelled failure.
                self.assertIn("probe URL", str(caught.exception))

    def test_malformed_authority_fails_bounded(self) -> None:
        # urlsplit itself rejects these shapes; the failure must
        # still surface as the bounded configuration ValueError.
        for url in (
            "https://[::1/v1/heartbeat",
            "https://sentinel.example]/v1/heartbeat",
            "https://[not-a-host]/v1/heartbeat",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    DeadManProbeSettings(url=url)
                self.assertIn("probe URL", str(caught.exception))

    def test_valid_synthetic_targets_still_accepted(self) -> None:
        for url in (
            "https://sentinel.example/v1/heartbeat",
            "https://sentinel.example:443/v1/heartbeat",
            "https://sentinel.example/",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    DeadManProbeSettings(url=url).url, url
                )


class ProbeRequestContractTest(unittest.TestCase):
    """One probe(): the exact single unauthenticated GET request."""

    def setUp(self) -> None:
        self.opener = _RecordingOpener(
            response=_FakeResponse(200, _headers("POST"))
        )
        self.prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=self.opener
        )

    def _probe_request(self) -> urllib.request.Request:
        self.prober.probe()
        self.assertEqual(len(self.opener.calls), 1)
        request, _timeout = self.opener.calls[0]
        return request

    def test_probe_uses_get(self) -> None:
        self.assertEqual(self._probe_request().get_method(), "GET")

    def test_request_url_is_the_configured_url_verbatim(self) -> None:
        request = self._probe_request()
        self.assertEqual(request.full_url, _URL)

    def test_request_has_no_body(self) -> None:
        self.assertIsNone(self._probe_request().data)

    def test_request_carries_no_headers_or_credentials(self) -> None:
        request = self._probe_request()
        self.assertEqual(request.header_items(), [])

    def test_configured_timeout_is_passed_to_transport(self) -> None:
        opener = _RecordingOpener(
            response=_FakeResponse(200, _headers("POST"))
        )
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL, timeout_seconds=4.5),
            opener=opener,
        )
        prober.probe()
        self.assertEqual(len(opener.calls), 1)
        _request, timeout = opener.calls[0]
        self.assertEqual(timeout, 4.5)

    def test_exactly_one_open_call_per_probe(self) -> None:
        self.prober.probe()
        self.assertEqual(len(self.opener.calls), 1)
        self.prober.probe()
        self.assertEqual(len(self.opener.calls), 2)

    def test_default_prober_uses_the_stdlib_opener(self) -> None:
        prober = DeadManProber(DeadManProbeSettings(url=_URL))
        self.assertIsInstance(
            prober._opener, deadman_probe._StdlibHttpsOpener
        )

    def test_production_opener_has_redirects_disabled(self) -> None:
        opener = deadman_probe._StdlibHttpsOpener()
        redirect_handlers = [
            handler
            for handler in opener._director.handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsInstance(
            redirect_handlers[0], deadman_probe._NoRedirectHandler
        )

    def test_production_redirect_handler_refuses_to_follow(self) -> None:
        handler = deadman_probe._NoRedirectHandler()
        request = urllib.request.Request(_URL, method="GET")
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


class ProbeOutcomeTest(unittest.TestCase):
    """The H1A classification boundary over observed responses."""

    def _probe_with(
        self,
        response: _FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> tuple[DeadManProbeOutcome, int]:
        opener = _RecordingOpener(response=response, error=error)
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=opener
        )
        outcome = prober.probe()
        return outcome, len(opener.calls)

    def test_canonical_healthy_405_is_healthy(self) -> None:
        response = _FakeResponse(405, _headers("POST"), b"")
        outcome, calls = self._probe_with(response=response)
        self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)
        self.assertEqual(calls, 1)

    def test_canonical_healthy_405_via_real_http_error(self) -> None:
        # The true production path: the stdlib raises 405 (any
        # non-2xx) as a file-like HTTPError owning the response. It
        # must be observed and classified exactly like a returned
        # response.
        stream = _TrackingStream(b"")
        error = _http_error(405, _headers("POST"), stream)
        opener = _RecordingOpener(error=error)
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=opener
        )
        outcome = prober.probe()
        self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)
        self.assertEqual(stream.read_sizes, [1])
        self.assertEqual(stream.close_calls, 1)

    def test_redirect_is_failed_and_never_followed(self) -> None:
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                error = _http_error(
                    status, _headers("POST"), _TrackingStream(b"")
                )
                outcome, calls = self._probe_with(error=error)
                self.assertIs(outcome, DeadManProbeOutcome.FAILED)
                self.assertEqual(calls, 1)

    def test_wrong_allow_is_failed(self) -> None:
        for headers in (
            _headers("GET"),
            _headers("GET, HEAD"),
            _headers("post"),
            _headers("POSTX"),
            email.message.Message(),
        ):
            with self.subTest(allow=str(headers.get_all("Allow"))):
                response = _FakeResponse(405, headers, b"")
                outcome, _calls = self._probe_with(response=response)
                self.assertIs(outcome, DeadManProbeOutcome.FAILED)

    def test_multiple_allow_values_are_preserved(self) -> None:
        # Multiplicity and arrival order survive into the H1A
        # observation: POST may appear in any of several values.
        for headers in (
            _headers("GET", "POST"),
            _headers("GET, HEAD", "POST"),
            _headers("POST", "GET"),
        ):
            with self.subTest(allow=str(headers.get_all("Allow"))):
                response = _FakeResponse(405, headers, b"")
                outcome, _calls = self._probe_with(response=response)
                self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)

    def test_non_empty_body_is_failed(self) -> None:
        for body in (b"x", b" ", b"\n", b"\x00"):
            with self.subTest(body=body):
                response = _FakeResponse(405, _headers("POST"), body)
                outcome, _calls = self._probe_with(response=response)
                self.assertIs(outcome, DeadManProbeOutcome.FAILED)

    def test_status_other_than_405_is_failed(self) -> None:
        for status in (200, 204, 301, 404, 500, 502, 503):
            with self.subTest(status=status):
                response = _FakeResponse(status, _headers("POST"), b"")
                outcome, _calls = self._probe_with(response=response)
                self.assertIs(outcome, DeadManProbeOutcome.FAILED)

    def test_status_outside_the_http_range_is_failed(self) -> None:
        # A structurally impossible observation never becomes healthy
        # evidence and never crashes the probe.
        response = _FakeResponse(700, _headers("POST"), b"")
        outcome, _calls = self._probe_with(response=response)
        self.assertIs(outcome, DeadManProbeOutcome.FAILED)

    def test_transport_failures_map_to_failed(self) -> None:
        for error in (
            urllib.error.URLError("name resolution failed"),
            ConnectionRefusedError(),
            ConnectionResetError("connection reset"),
            OSError("network unreachable"),
            TimeoutError("timed out"),
        ):
            with self.subTest(error=type(error).__name__):
                outcome, calls = self._probe_with(error=error)
                self.assertIs(outcome, DeadManProbeOutcome.FAILED)
                self.assertEqual(calls, 1)

    def test_read_failure_maps_to_failed(self) -> None:
        for read_error in (
            TimeoutError("read timed out"),
            ConnectionResetError("reset during read"),
            OSError("read failure"),
        ):
            with self.subTest(error=type(read_error).__name__):
                response = _FakeResponse(
                    405,
                    _headers("POST"),
                    b"",
                    read_error=read_error,
                )
                outcome, _calls = self._probe_with(response=response)
                self.assertIs(outcome, DeadManProbeOutcome.FAILED)

    def test_classification_is_delegated_to_the_h1a_classifier(
        self,
    ) -> None:
        response = _FakeResponse(405, _headers("GET", "POST"), b"")
        opener = _RecordingOpener(response=response)
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=opener
        )
        seen: list[object] = []
        original = deadman_probe.classify_deadman_probe

        def spy(observation: object) -> DeadManProbeOutcome:
            seen.append(observation)
            return original(observation)

        with mock.patch.object(
            deadman_probe, "classify_deadman_probe", side_effect=spy
        ):
            outcome = prober.probe()
        self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)
        self.assertEqual(len(seen), 1)
        observation = seen[0]
        self.assertIsInstance(observation, DeadManProbeResponse)
        assert isinstance(observation, DeadManProbeResponse)
        self.assertEqual(observation.status, 405)
        self.assertEqual(observation.allow_headers, ("GET", "POST"))
        self.assertEqual(observation.body, b"")


class BoundedReadAndCleanupTest(unittest.TestCase):
    """The single bounded body sample and exactly-once cleanup."""

    def _probe(
        self, response: _FakeResponse
    ) -> tuple[DeadManProbeOutcome, _FakeResponse]:
        opener = _RecordingOpener(response=response)
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=opener
        )
        outcome = prober.probe()
        self.assertEqual(len(opener.calls), 1)
        return outcome, response

    def test_exactly_one_bounded_read_of_one_byte(self) -> None:
        response = _FakeResponse(405, _headers("POST"), b"body")
        outcome, closed = self._probe(response)
        self.assertIs(outcome, DeadManProbeOutcome.FAILED)
        self.assertEqual(closed.read_sizes, [1])

    def test_empty_body_reads_one_bounded_byte(self) -> None:
        response = _FakeResponse(405, _headers("POST"), b"")
        outcome, observed = self._probe(response)
        self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)
        self.assertEqual(observed.read_sizes, [1])

    def test_close_is_attempted_exactly_once_on_every_path(self) -> None:
        scenarios = [
            _FakeResponse(405, _headers("POST"), b""),
            _FakeResponse(405, _headers("POST"), b"body"),
            _FakeResponse(200, _headers("POST"), b""),
            _FakeResponse(
                405, _headers("POST"), b"", read_error=OSError("boom")
            ),
        ]
        for response in scenarios:
            with self.subTest(status=response.status):
                _outcome, observed = self._probe(response)
                self.assertEqual(observed.close_calls, 1)

    def test_cleanup_failure_never_changes_an_observed_outcome(
        self,
    ) -> None:
        # The liveness evidence was fully read before the close: a
        # failing close is local resource noise, not target evidence.
        response = _FakeResponse(
            405,
            _headers("POST"),
            b"",
            close_error=OSError(
                "RAW-CLOSE-FAILURE https://sentinel.example/"
            ),
        )
        outcome, observed = self._probe(response)
        self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)
        self.assertEqual(observed.close_calls, 1)

    def test_cleanup_failure_on_failed_outcome_stays_failed(self) -> None:
        response = _FakeResponse(
            502,
            _headers("POST"),
            b"",
            close_error=OSError("RAW-CLOSE-FAILURE"),
        )
        outcome, observed = self._probe(response)
        self.assertIs(outcome, DeadManProbeOutcome.FAILED)
        self.assertEqual(observed.close_calls, 1)

    def test_raw_cleanup_exception_never_escapes(self) -> None:
        response = _FakeResponse(
            405,
            _headers("POST"),
            b"",
            close_error=ConnectionResetError("RAW-RESET"),
        )
        outcome, _observed = self._probe(response)
        self.assertIs(outcome, DeadManProbeOutcome.HEALTHY)

    def test_http_error_stream_closed_exactly_once(self) -> None:
        stream = _TrackingStream(b"")
        error = _http_error(503, _headers("POST"), stream)
        opener = _RecordingOpener(error=error)
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=opener
        )
        outcome = prober.probe()
        self.assertIs(outcome, DeadManProbeOutcome.FAILED)
        self.assertEqual(stream.close_calls, 1)

    def test_transport_error_path_has_no_response_to_close(self) -> None:
        opener = _RecordingOpener(
            error=urllib.error.URLError("DNS failure")
        )
        prober = DeadManProber(
            DeadManProbeSettings(url=_URL), opener=opener
        )
        outcome = prober.probe()
        self.assertIs(outcome, DeadManProbeOutcome.FAILED)
        self.assertEqual(len(opener.calls), 1)


class StageBoundariesTest(unittest.TestCase):
    """H1B-1 probe stays a bounded adapter, not an orchestrator."""

    _MODULE_SOURCE = inspect.getsource(deadman_probe)

    def test_public_surface_is_bounded(self) -> None:
        self.assertEqual(
            sorted(deadman_probe.__all__),
            ["DeadManProbeSettings", "DeadManProber"],
        )

    def test_no_sleep_or_socket_state_mutation(self) -> None:
        # Retry/backoff absence is proven behaviorally (exactly one
        # open call per probe, also after failures); these checks
        # target precise code spellings only.
        for forbidden in ("sleep(", "setdefaulttimeout"):
            self.assertNotIn(forbidden, self._MODULE_SOURCE)

    def test_no_clock_reads(self) -> None:
        for forbidden in (
            "time.time",
            "monotonic(",
            "utcnow",
            "datetime.now",
        ):
            self.assertNotIn(forbidden, self._MODULE_SOURCE)

    def test_no_environment_secret_or_logging_access(self) -> None:
        for forbidden in (
            "os.environ",
            "environ[",
            "environ.get",
            "getenv",
            "getpass",
            "keyring",
            "print(",
            "logging",
            "logger",
        ):
            self.assertNotIn(forbidden.lower(), self._MODULE_SOURCE.lower())

    def test_no_state_machine_or_notification_authority(self) -> None:
        for forbidden in (
            "advance_deadman_status",
            "acknowledge_deadman_notification",
            "DeadManStatus",
            "DeadManNotification",
            "INITIAL_DEADMAN_STATUS",
        ):
            self.assertNotIn(forbidden, self._MODULE_SOURCE)

    def test_module_is_independent_of_the_central_telegram(self) -> None:
        # The probe shares no code path or import with any central
        # delivery transport; prose references stay harmless.
        imported = {
            name
            for name, module in inspect.getmembers(
                deadman_probe, inspect.ismodule
            )
        }
        self.assertNotIn("telegram", imported)


if __name__ == "__main__":
    unittest.main()
