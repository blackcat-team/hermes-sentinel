"""Deterministic process tests for the Stage H1B-2 dead-man oneshot
entrypoint.

These tests prove the process boundary of
``run_deadman_process``: a normal completed cycle returns exit 0
with a minimal secret-safe stdout line, every bounded operational
failure category (configuration, state store, Telegram delivery)
returns a non-zero exit with one concise stderr line, no secret ever
leaks into stdout/stderr, and the process performs exactly ONE cycle
per invocation — one probe, at most one delivery, one clock read, no
daemon and no retry loop.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import deadman_process, deadman_runtime  # noqa: E402
from hermes_sentinel.deadman import (  # noqa: E402
    INITIAL_DEADMAN_STATUS,
    DeadManProbeOutcome,
    advance_deadman_status,
    encode_deadman_status,
)
from hermes_sentinel.deadman_process import run_deadman_process  # noqa: E402
from hermes_sentinel.deadman_store import DeadManStateStore  # noqa: E402
from hermes_sentinel.deadman_telegram import (  # noqa: E402
    DeadManTelegramDeliveryError,
)

SECRET_TOKEN = "900001:SECRET_TOKEN_XYZ"

T1 = datetime(2026, 9, 25, 12, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 25, 12, 2, tzinfo=timezone.utc)
T3 = datetime(2026, 9, 25, 12, 3, tzinfo=timezone.utc)
T4 = datetime(2026, 9, 25, 12, 4, tzinfo=timezone.utc)
T5 = datetime(2026, 9, 25, 12, 5, tzinfo=timezone.utc)
T6 = datetime(2026, 9, 25, 12, 6, tzinfo=timezone.utc)


class FakeProber:
    def __init__(self, outcome: DeadManProbeOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    def probe(self) -> DeadManProbeOutcome:
        self.calls += 1
        return self._outcome


class FakeSender:
    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure
        self.sent: list = []

    def send(self, notification) -> None:
        self.sent.append(notification)
        if self._failure is not None:
            raise self._failure


class FakeClock:
    def __init__(self, *moments: datetime) -> None:
        self._moments = moments
        self.calls = 0

    def __call__(self) -> datetime:
        moment = self._moments[min(self.calls, len(self._moments) - 1)]
        self.calls += 1
        return moment


def _down_status_with_pending():
    """A DOWN status with one pending DOWN intent (id 1), produced
    through the accepted H1A state machine."""
    status = advance_deadman_status(
        status=INITIAL_DEADMAN_STATUS,
        outcome=DeadManProbeOutcome.HEALTHY,
        now=T1,
    )
    for moment in (T2, T3, T4):
        status = advance_deadman_status(
            status=status, outcome=DeadManProbeOutcome.FAILED, now=moment
        )
    return status


class ProcessSuccessTest(unittest.TestCase):
    def test_successful_cycle_exits_zero_with_minimal_stdout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            prober = FakeProber(DeadManProbeOutcome.HEALTHY)
            sender = FakeSender()
            clock = FakeClock(T1)

            stdout, stderr, code = _run(env, prober, sender, clock)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("dead-man cycle completed", stdout)
            self.assertIn("state=up", stdout)
            self.assertIn("pending=0", stdout)
            self.assertEqual(prober.calls, 1)
            self.assertEqual(clock.calls, 1)
            self.assertEqual(sender.sent, [])
            self.assertNotIn(SECRET_TOKEN, stdout)

    def test_target_down_observation_is_normal_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            # A FAILED observation on the fresh initial state: the
            # target is not yet DOWN (debounce), state processing
            # succeeds — a NORMAL cycle with exit 0.
            prober = FakeProber(DeadManProbeOutcome.FAILED)
            stdout, stderr, code = _run(
                env, prober, FakeSender(), FakeClock(T1)
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("state=unknown", stdout)


class ProcessFailureTest(unittest.TestCase):
    def test_config_failure_exits_nonzero(self) -> None:
        env = dict(
            HERMES_SENTINEL_DEADMAN_PROBE_URL=(
                "https://sentinel.example/v1/heartbeat"
            ),
            HERMES_SENTINEL_DEADMAN_STATE_PATH=(
                "/var/lib/hermes-sentinel/deadman-state.json"
            ),
            # bot token missing entirely
            HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID="-1001234567890",
        )
        stdout, stderr, code = _run(env, FakeProber(DeadManProbeOutcome.HEALTHY))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("configuration error", stderr)
        self.assertIn(
            "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN", stderr
        )

    def test_state_store_failure_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            # A corrupt state document: the real store fails closed
            # on load BEFORE any probe.
            state_path.write_text(
                "{not valid json", encoding="utf-8"
            )
            env = _env_for(state_path)
            prober = FakeProber(DeadManProbeOutcome.HEALTHY)

            stdout, stderr, code = _run(env, prober)

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("state store failure", stderr)
            self.assertEqual(prober.calls, 0)

    def test_delivery_failure_exits_nonzero_pending_survives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            state_path.write_text(
                encode_deadman_status(_down_status_with_pending()),
                encoding="utf-8",
            )
            env = _env_for(state_path)
            prober = FakeProber(DeadManProbeOutcome.FAILED)
            sender = FakeSender(
                failure=DeadManTelegramDeliveryError(
                    "Dead-man Telegram delivery failed: HTTP status 502"
                )
            )

            stdout, stderr, code = _run(env, prober, sender, FakeClock(T5))

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("delivery failed", stderr)
            # Exactly one delivery attempt; the pending intent
            # survived in the persisted state (persist-before-deliver,
            # no acknowledgement, no second persist).
            self.assertEqual(len(sender.sent), 1)
            self.assertEqual(sender.sent[0].notification_id, 1)
            reloaded = DeadManStateStore(state_path).load()
            self.assertEqual(
                [n.notification_id for n in reloaded.pending_notifications],
                [1],
            )

    def test_unexpected_runtime_failure_exits_nonzero(self) -> None:
        class ExplodingProber:
            calls = 0

            def probe(self):
                self.calls += 1
                raise RuntimeError(
                    f"boom {SECRET_TOKEN}"  # must never be echoed
                )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            stdout, stderr, code = _run(
                env, ExplodingProber(), FakeSender(), FakeClock(T1)
            )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("unexpected runtime failure", stderr)
            self.assertIn("RuntimeError", stderr)
            # Raw exception text (secret-bearing here) never echoed.
            self.assertNotIn(SECRET_TOKEN, stderr)
            self.assertNotIn("boom", stderr)


class AdapterConstructionSecretSafetyTest(unittest.TestCase):
    """QA remediation regression: PRODUCTION adapter construction is
    inside the bounded secret-safe process boundary — an unexpected
    constructor failure produces a non-zero exit with one generic
    stderr category line, never a raw traceback, never the raw
    exception text, and the Telegram sender / runtime cycle are never
    invoked after the failed setup."""

    def test_prober_constructor_failure_is_bounded(self) -> None:
        marker = "SENSITIVE-PROBER-SETUP-MARKER"

        def exploding_prober(settings):
            raise RuntimeError(marker)

        def forbidden_sender(settings):
            raise AssertionError(
                "the sender must not be constructed after the prober"
                " setup failure"
            )

        def forbidden_cycle(**kwargs):
            raise AssertionError("the runtime cycle must not be invoked")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            with (
                mock.patch.object(
                    deadman_process, "DeadManProber", exploding_prober
                ),
                mock.patch.object(
                    deadman_process,
                    "DeadManTelegramSender",
                    forbidden_sender,
                ),
                mock.patch.object(
                    deadman_process, "run_deadman_cycle", forbidden_cycle
                ),
            ):
                stdout, stderr, code = _run(env)

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("setup failed", stderr)
            self.assertNotIn(marker, stdout + stderr)
            self.assertNotIn("SENSITIVE", stdout + stderr)
            self.assertNotIn(SECRET_TOKEN, stdout + stderr)
            self.assertNotIn("Traceback", stdout + stderr)
            # The cycle never ran: no state file was ever written.
            self.assertFalse(state_path.exists())

    def test_sender_constructor_failure_is_bounded(self) -> None:
        marker = "SENSITIVE-SENDER-SETUP-MARKER"

        def exploding_sender(settings):
            raise RuntimeError(marker)

        def forbidden_cycle(**kwargs):
            raise AssertionError("the runtime cycle must not be invoked")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            # The prober is the REAL accepted adapter: its
            # construction performs no network I/O, proving the setup
            # boundary fails closed at the sender exactly as at the
            # prober.
            with (
                mock.patch.object(
                    deadman_process,
                    "DeadManTelegramSender",
                    exploding_sender,
                ),
                mock.patch.object(
                    deadman_process, "run_deadman_cycle", forbidden_cycle
                ),
            ):
                stdout, stderr, code = _run(env)

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("setup failed", stderr)
            self.assertNotIn(marker, stdout + stderr)
            self.assertNotIn("SENSITIVE", stdout + stderr)
            self.assertNotIn(SECRET_TOKEN, stdout + stderr)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertFalse(state_path.exists())

    def test_injected_adapters_skip_production_construction(
        self,
    ) -> None:
        """The injected deterministic doubles bypass the production
        constructors entirely: the setup boundary must not fire and
        the normal cycle path still succeeds."""
        def forbidden_prober(settings):
            raise AssertionError(
                "the production prober must not be constructed when a"
                " double is injected"
            )

        def forbidden_sender(settings):
            raise AssertionError(
                "the production sender must not be constructed when a"
                " double is injected"
            )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            with (
                mock.patch.object(
                    deadman_process, "DeadManProber", forbidden_prober
                ),
                mock.patch.object(
                    deadman_process,
                    "DeadManTelegramSender",
                    forbidden_sender,
                ),
            ):
                stdout, stderr, code = _run(
                    env,
                    FakeProber(DeadManProbeOutcome.HEALTHY),
                    FakeSender(),
                    FakeClock(T1),
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("dead-man cycle completed", stdout)


class ProcessSecretSafetyTest(unittest.TestCase):
    def test_no_secret_leakage_across_outcomes(self) -> None:
        outcomes: list[tuple[str, str, int]] = []
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            state_path.write_text(
                encode_deadman_status(_down_status_with_pending()),
                encoding="utf-8",
            )
            env = _env_for(state_path)
            # success path (delivery + ack), delivery failure path,
            # and config failure path.
            outcomes.append(
                _run(
                    env,
                    FakeProber(DeadManProbeOutcome.FAILED),
                    FakeSender(),
                    FakeClock(T5),
                )
            )
            outcomes.append(
                _run(
                    env,
                    FakeProber(DeadManProbeOutcome.FAILED),
                    FakeSender(
                        failure=DeadManTelegramDeliveryError(
                            "Dead-man Telegram delivery failed:"
                            " network error"
                        )
                    ),
                    FakeClock(T6),
                )
            )
            outcomes.append(
                _run(
                    {"HERMES_SENTINEL_DEADMAN_PROBE_URL": "x"},
                    FakeProber(DeadManProbeOutcome.HEALTHY),
                )
            )
        for stdout, stderr, code in outcomes:
            with self.subTest(code=code, stdout=stdout, stderr=stderr):
                self.assertNotIn(SECRET_TOKEN, stdout + stderr)
                self.assertNotIn("api.telegram.org", stdout + stderr)


class ProcessShapeTest(unittest.TestCase):
    def test_exactly_one_cycle_per_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deadman-state.json"
            env = _env_for(state_path)
            prober = FakeProber(DeadManProbeOutcome.HEALTHY)
            sender = FakeSender()
            clock = FakeClock(T1, T2, T3, T4)
            code = _run(env, prober, sender, clock)[2]
            self.assertEqual(code, 0)
            self.assertEqual(prober.calls, 1)
            self.assertEqual(clock.calls, 1)
            self.assertLessEqual(len(sender.sent), 1)

    def test_no_daemon_or_retry_loop_in_source(self) -> None:
        source = (
            inspect.getsource(deadman_process)
            + inspect.getsource(deadman_runtime)
        )
        self.assertNotIn("while True", source)
        self.assertNotIn("sleep(", source)
        self.assertNotIn("import time", source)

    def test_main_is_env_passthrough_entrypoint(self) -> None:
        source = inspect.getsource(deadman_process.main)
        self.assertIn("run_deadman_process(os.environ)", source)


def _env_for(state_path: Path) -> dict[str, str]:
    return {
        "HERMES_SENTINEL_DEADMAN_PROBE_URL": (
            "https://sentinel.example/v1/heartbeat"
        ),
        "HERMES_SENTINEL_DEADMAN_STATE_PATH": str(state_path),
        "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN": SECRET_TOKEN,
        "HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID": "-1001234567890",
    }


def _run(
    env,
    prober=None,
    sender=None,
    clock=None,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
        stderr
    ):
        code = run_deadman_process(
            env,
            prober=prober,
            sender=sender,
            clock=clock,
        )
    return stdout.getvalue(), stderr.getvalue(), code


if __name__ == "__main__":
    unittest.main()
