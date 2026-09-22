"""Deterministic tests for the Stage E8 process lifecycle boundary.

Covers the authoritative E8 contract with doubles patched onto the
process module's imported names: the exact supplied environment
Mapping reaches the accepted E6 loader exactly once, the exact
returned settings object reaches the accepted E7 builder exactly
once, one context-managed application runs ``run_forever`` exactly
once with the exact process-owned stop predicate (False before a
stop signal, True after the first SIGTERM or SIGINT, stable under
repeated delivery, and flipped by handlers that do nothing else),
graceful return performs the E7-owned context cleanup, runtime and
startup failures propagate as the same exception objects with
cleanup and previous-handler restoration still performed, a partial
signal installation failure rolls back everything already changed
while the original installation failure escapes, ``main`` passes the
live ``os.environ`` object through untouched, and no hidden
environment/config-file reads, concurrency or direct E7 cleanup
exist in the module. No real OS signal is ever delivered: the signal
API is a recording double that can also fail any single signal()
attempt at an exact position, proving attempt order, rollback and
escaping-exception identity on every install/restore failure path.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import signal
import sys
import tomllib
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import process  # noqa: E402
from hermes_sentinel.settings import CentralSettingsError  # noqa: E402


def _previous_sigint_handler(signum: int, frame: Any) -> None:
    """A plausible non-default previous SIGINT handler (never run)."""


class _FakeSignalModule:
    """signal-module double: records getsignal/signal, never signals.

    ``previous`` maps a signum to what ``getsignal`` reports (the
    captured previous handler); ``signal_attempts`` logs EVERY
    ``signal`` attempt in order including failed ones, while
    ``signal_calls`` logs only the attempts that succeeded;
    ``fail_at`` raises the mapped exception for the ``signal``
    attempt at one exact zero-based position (installation and
    restoration positions alike); ``fail_on`` maps a signum to an
    exception ``signal`` raises for it; ``fail_getsignal_on`` does
    the same for ``getsignal``.
    """

    def __init__(self) -> None:
        self.previous: dict[int, Any] = {}
        self.signal_attempts: list[tuple[int, Any]] = []
        self.signal_calls: list[tuple[int, Any]] = []
        self.getsignal_calls: list[int] = []
        self.fail_at: dict[int, BaseException] = {}
        self.fail_on: dict[int, BaseException] = {}
        self.fail_getsignal_on: dict[int, BaseException] = {}

    def getsignal(self, signum: int) -> Any:
        self.getsignal_calls.append(signum)
        failure = self.fail_getsignal_on.get(signum)
        if failure is not None:
            raise failure
        return self.previous.get(signum, signal.SIG_DFL)

    def signal(self, signum: int, handler: Any) -> Any:
        position = len(self.signal_attempts)
        self.signal_attempts.append((signum, handler))
        failure = self.fail_at.get(position)
        if failure is None:
            failure = self.fail_on.get(signum)
        if failure is not None:
            raise failure
        self.signal_calls.append((signum, handler))
        return handler

    def installs_for(self, signum: int) -> list[Any]:
        """Every handler ever installed for ``signum``, in order."""
        return [handler for num, handler in self.signal_calls if num == signum]


class _FakeApplication:
    """SentinelApplication double: context manager + run_forever recorder.

    ``exit_calls`` counts context-manager exits (the E7-owned cleanup
    boundary E8 must use); ``close_calls`` counts direct ``close()``
    calls (which E8 must never make); ``initial_stop_value`` captures
    the stop predicate's value at the moment run_forever starts;
    ``on_run_forever_error`` is raised from run_forever when set.
    """

    def __init__(self) -> None:
        self.enter_calls = 0
        self.exit_calls = 0
        self.exit_exc_types: list[Any] = []
        self.close_calls = 0
        self.run_forever_calls = 0
        self.run_forever_predicates: list[Any] = []
        self.initial_stop_values: list[bool] = []
        self.on_run_forever: Any = None
        self.on_run_forever_error: BaseException | None = None

    def __enter__(self) -> _FakeApplication:
        self.enter_calls += 1
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        self.exit_calls += 1
        self.exit_exc_types.append(exc_type)

    def close(self) -> None:
        self.close_calls += 1

    def run_forever(self, should_stop: Any = None) -> None:
        self.run_forever_calls += 1
        self.run_forever_predicates.append(should_stop)
        if should_stop is not None:
            self.initial_stop_values.append(should_stop())
        if self.on_run_forever is not None:
            self.on_run_forever(self)
        if self.on_run_forever_error is not None:
            raise self.on_run_forever_error


class _Boundary:
    """One fully patched run_process environment.

    ``load`` and ``build`` are the patched E6/E7 boundaries (they
    record exact arguments and count calls); ``signals`` is the
    patched signal module; ``app`` the single fake application.
    """

    def __init__(self) -> None:
        self.env: Mapping[str, str] = {
            "SENTINEL_DATABASE_PATH": "irrelevant-to-e8"
        }
        self.settings = object()
        self.app = _FakeApplication()
        self.signals = _FakeSignalModule()
        self.signals.previous[signal.SIGTERM] = signal.SIG_IGN
        self.signals.previous[signal.SIGINT] = _previous_sigint_handler
        self.load_calls: list[Mapping[str, str]] = []
        self.build_calls: list[Any] = []
        self.load_error: BaseException | None = None
        self.build_error: BaseException | None = None

    def load(self, env: Mapping[str, str]) -> Any:
        self.load_calls.append(env)
        if self.load_error is not None:
            raise self.load_error
        return self.settings

    def build(self, settings: Any) -> _FakeApplication:
        self.build_calls.append(settings)
        if self.build_error is not None:
            raise self.build_error
        return self.app


class ProcessBoundaryTestCase(unittest.TestCase):
    """Identity, call-order, signal-lifecycle and failure behavior."""

    def setUp(self) -> None:
        self.boundary = _Boundary()
        patchers = [
            mock.patch.object(
                process, "load_central_settings", self.boundary.load
            ),
            mock.patch.object(
                process, "build_application", self.boundary.build
            ),
            mock.patch.object(process, "signal", self.boundary.signals),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- E6 -> E7 -> E5 exact delegation --------------------------------

    def test_exact_env_reaches_load_exactly_once(self) -> None:
        process.run_process(self.boundary.env)
        load_calls = self.boundary.load_calls
        self.assertEqual(1, len(load_calls))
        # Object identity, not equality: a distinct-but-equal mapping
        # must NOT satisfy this proof.
        self.assertIs(self.boundary.env, load_calls[0])

    def test_exact_settings_reach_build_exactly_once(self) -> None:
        process.run_process(self.boundary.env)
        self.assertEqual([self.boundary.settings], self.boundary.build_calls)

    def test_exactly_one_application_is_built_and_run(self) -> None:
        process.run_process(self.boundary.env)
        self.assertEqual(1, self.boundary.app.enter_calls)

    # --- signal installation and capture -------------------------------

    def test_previous_handlers_are_captured_before_installation(self) -> None:
        process.run_process(self.boundary.env)
        signals = self.boundary.signals
        self.assertEqual(
            [signal.SIGTERM, signal.SIGINT], signals.getsignal_calls
        )
        # Each capture precedes its installation in the call log.
        self.assertEqual(
            [signal.SIGTERM, signal.SIGINT],
            [num for num, _handler in signals.signal_calls[:2]],
        )

    def test_both_e8_handlers_are_installed(self) -> None:
        process.run_process(self.boundary.env)
        signals = self.boundary.signals
        term_handlers = signals.installs_for(signal.SIGTERM)
        int_handlers = signals.installs_for(signal.SIGINT)
        self.assertEqual(2, len(term_handlers))  # install + restore
        self.assertEqual(2, len(int_handlers))
        installed_term, installed_int = term_handlers[0], int_handlers[0]
        self.assertNotEqual(signal.SIG_IGN, installed_term)
        self.assertIsNot(_previous_sigint_handler, installed_int)
        self.assertTrue(callable(installed_term))
        self.assertTrue(callable(installed_int))

    def test_previous_handlers_are_restored_after_success(self) -> None:
        process.run_process(self.boundary.env)
        signals = self.boundary.signals
        self.assertEqual(
            [
                (signal.SIGTERM, signals.installs_for(signal.SIGTERM)[0]),
                (signal.SIGINT, signals.installs_for(signal.SIGINT)[0]),
                (signal.SIGINT, _previous_sigint_handler),
                (signal.SIGTERM, signal.SIG_IGN),
            ],
            signals.signal_calls,
        )

    # --- stop request semantics -----------------------------------------

    def test_stop_predicate_is_false_before_any_signal(self) -> None:
        process.run_process(self.boundary.env)
        self.assertEqual([False], self.boundary.app.initial_stop_values)

    def test_run_forever_receives_the_exact_stop_predicate(self) -> None:
        process.run_process(self.boundary.env)
        app = self.boundary.app
        self.assertEqual(1, app.run_forever_calls)
        self.assertIsNotNone(app.run_forever_predicates[0])

    def test_sigterm_handler_flips_the_exact_predicate(self) -> None:
        process.run_process(self.boundary.env)
        handler = self.boundary.signals.installs_for(signal.SIGTERM)[0]
        predicate = self.boundary.app.run_forever_predicates[0]
        handler(signal.SIGTERM, None)
        self.assertIs(True, predicate())
        self.assertEqual(1, self.boundary.app.run_forever_calls)

    def test_sigint_handler_flips_the_exact_predicate(self) -> None:
        process.run_process(self.boundary.env)
        handler = self.boundary.signals.installs_for(signal.SIGINT)[0]
        predicate = self.boundary.app.run_forever_predicates[0]
        handler(signal.SIGINT, None)
        self.assertIs(True, predicate())

    def test_repeated_signal_delivery_stays_true_with_one_runtime_call(
        self,
    ) -> None:
        process.run_process(self.boundary.env)
        term = self.boundary.signals.installs_for(signal.SIGTERM)[0]
        interrupt = self.boundary.signals.installs_for(signal.SIGINT)[0]
        predicate = self.boundary.app.run_forever_predicates[0]
        for _ in range(3):
            term(signal.SIGTERM, None)
            interrupt(signal.SIGINT, None)
        self.assertIs(True, predicate())
        self.assertEqual(1, self.boundary.app.run_forever_calls)

    def test_handlers_do_only_mark_stop_during_runtime(self) -> None:
        observed: dict[str, Any] = {}

        def during_runtime(app: _FakeApplication) -> None:
            term = self.boundary.signals.installs_for(signal.SIGTERM)[0]
            interrupt = self.boundary.signals.installs_for(signal.SIGINT)[0]

            def forbidden_exit(code: Any = None) -> None:
                raise AssertionError("signal handler exited the process")

            with mock.patch.object(sys, "exit", forbidden_exit):
                term(signal.SIGTERM, None)
                interrupt(signal.SIGINT, None)
            observed["close_calls"] = app.close_calls
            observed["run_forever_calls"] = app.run_forever_calls
            observed["predicate_value"] = app.run_forever_predicates[0]()

        self.boundary.app.on_run_forever = during_runtime
        process.run_process(self.boundary.env)
        self.assertEqual(0, observed["close_calls"])
        self.assertEqual(1, observed["run_forever_calls"])
        self.assertIs(True, observed["predicate_value"])

    # --- lifecycle and cleanup ------------------------------------------

    def test_graceful_return_performs_context_cleanup_only(self) -> None:
        process.run_process(self.boundary.env)
        app = self.boundary.app
        self.assertEqual(1, app.enter_calls)
        self.assertEqual(1, app.exit_calls)
        self.assertEqual([None], app.exit_exc_types)
        self.assertEqual(0, app.close_calls)

    def test_runtime_exception_propagates_and_cleans_up_and_restores(
        self,
    ) -> None:
        error = RuntimeError("runtime exploded")
        self.boundary.app.on_run_forever_error = error
        with self.assertRaises(RuntimeError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        app = self.boundary.app
        self.assertEqual(1, app.exit_calls)
        self.assertEqual([RuntimeError], app.exit_exc_types)
        self.assertEqual(0, app.close_calls)
        signals = self.boundary.signals
        self.assertEqual(
            _previous_sigint_handler,
            signals.installs_for(signal.SIGINT)[-1],
        )
        self.assertEqual(
            signal.SIG_IGN, signals.installs_for(signal.SIGTERM)[-1]
        )

    # --- startup failures -------------------------------------------------

    def test_settings_failure_propagates_with_nothing_started(self) -> None:
        error = CentralSettingsError("SENTINEL_DATABASE_PATH is missing")
        self.boundary.load_error = error
        with self.assertRaises(CentralSettingsError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        self.assertEqual([], self.boundary.build_calls)
        self.assertEqual(0, self.boundary.app.run_forever_calls)
        signals = self.boundary.signals
        self.assertEqual([], signals.getsignal_calls)
        self.assertEqual([], signals.signal_calls)

    def test_build_failure_propagates_and_restores_handlers(self) -> None:
        error = OSError("address already in use")
        self.boundary.build_error = error
        with self.assertRaises(OSError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        self.assertEqual(0, self.boundary.app.run_forever_calls)
        self.assertEqual(0, self.boundary.app.enter_calls)
        self.assertEqual(0, self.boundary.app.exit_calls)
        signals = self.boundary.signals
        self.assertEqual(
            _previous_sigint_handler,
            signals.installs_for(signal.SIGINT)[-1],
        )
        self.assertEqual(
            signal.SIG_IGN, signals.installs_for(signal.SIGTERM)[-1]
        )

    # --- partial signal installation rollback ---------------------------

    def test_partial_install_failure_restores_and_propagates(self) -> None:
        error = OSError("cannot install SIGINT handler")
        self.boundary.signals.fail_on[signal.SIGINT] = error
        with self.assertRaises(OSError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        signals = self.boundary.signals
        # SIGTERM was installed and rolled back; SIGINT never was.
        self.assertEqual(
            [
                (signal.SIGTERM, signals.installs_for(signal.SIGTERM)[0]),
                (signal.SIGTERM, signal.SIG_IGN),
            ],
            signals.signal_calls,
        )
        # The failing SIGINT installation never took effect.
        self.assertEqual([], signals.installs_for(signal.SIGINT))
        self.assertIs(
            signal.SIG_IGN, signals.installs_for(signal.SIGTERM)[-1]
        )
        # Nothing application-shaped was opened, so nothing leaked.
        self.assertEqual([], self.boundary.build_calls)
        self.assertEqual(0, self.boundary.app.run_forever_calls)

    def test_capture_failure_after_install_restores_and_propagates(
        self,
    ) -> None:
        error = ValueError("cannot capture previous SIGINT handler")
        self.boundary.signals.fail_getsignal_on[signal.SIGINT] = error
        with self.assertRaises(ValueError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        signals = self.boundary.signals
        self.assertEqual([], self.boundary.build_calls)
        self.assertEqual(
            signal.SIG_IGN, signals.installs_for(signal.SIGTERM)[-1]
        )


class SignalFailurePositionTestCase(unittest.TestCase):
    """Failure injection at exact signal() attempt positions.

    Position map for one full run_process lifecycle (the zero-based
    order of signal() attempts): 0 install SIGTERM, 1 install SIGINT,
    2 restore SIGINT, 3 restore SIGTERM. ``fail_at`` makes exactly one
    chosen attempt raise; ``signal_attempts`` proves which attempts
    were made at all (failed ones included), ``signal_calls`` /
    ``installs_for`` which succeeded, and identity assertions prove
    exactly which exception object escapes as the primary failure.
    """

    def setUp(self) -> None:
        self.boundary = _Boundary()
        patchers = [
            mock.patch.object(
                process, "load_central_settings", self.boundary.load
            ),
            mock.patch.object(
                process, "build_application", self.boundary.build
            ),
            mock.patch.object(process, "signal", self.boundary.signals),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- A: the very first installation attempt fails -------------------

    def test_first_install_failure_starts_nothing_and_propagates(
        self,
    ) -> None:
        error = OSError("cannot install the first stop handler")
        self.boundary.signals.fail_at[0] = error
        with self.assertRaises(OSError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        signals = self.boundary.signals
        # Exactly one attempt was ever made — the failed SIGTERM
        # installation: no later installation, no runtime work and no
        # restoration of a handler that was never installed.
        self.assertEqual(1, len(signals.signal_attempts))
        self.assertEqual([], signals.signal_calls)
        self.assertEqual([], self.boundary.build_calls)
        self.assertEqual(0, self.boundary.app.run_forever_calls)
        self.assertEqual(0, self.boundary.app.enter_calls)

    # --- B: normal return, first restoration attempt fails --------------

    def test_first_restoration_failure_still_restores_other_and_raises(
        self,
    ) -> None:
        error = ValueError("cannot restore the SIGINT handler")
        self.boundary.signals.fail_at[2] = error
        with self.assertRaises(ValueError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        signals = self.boundary.signals
        # Both installs and BOTH restoration attempts were made.
        self.assertEqual(4, len(signals.signal_attempts))
        self.assertEqual(
            (signal.SIGINT, _previous_sigint_handler),
            signals.signal_attempts[2],
        )
        self.assertEqual(
            (signal.SIGTERM, signal.SIG_IGN), signals.signal_attempts[3]
        )
        # The other (SIGTERM) restoration succeeded; the failed SIGINT
        # restoration left the E8 handler as its last successful
        # install, so the shutdown was NOT completed successfully.
        self.assertEqual(
            signal.SIG_IGN, signals.installs_for(signal.SIGTERM)[-1]
        )
        self.assertIs(
            signals.installs_for(signal.SIGINT)[0],
            signals.installs_for(signal.SIGINT)[-1],
        )
        # Application cleanup had already happened before restoration.
        self.assertEqual(1, self.boundary.app.exit_calls)

    # --- C: normal return, second restoration attempt fails -------------

    def test_second_restoration_failure_raises_after_first_succeeds(
        self,
    ) -> None:
        error = ValueError("cannot restore the SIGTERM handler")
        self.boundary.signals.fail_at[3] = error
        with self.assertRaises(ValueError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(error, raised.exception)
        signals = self.boundary.signals
        self.assertEqual(4, len(signals.signal_attempts))
        # The first restoration (SIGINT) succeeded before the second
        # (SIGTERM) raised, so the exact restoration exception stays
        # observable instead of a normal return.
        self.assertEqual(
            _previous_sigint_handler,
            signals.installs_for(signal.SIGINT)[-1],
        )
        self.assertIs(
            signals.installs_for(signal.SIGTERM)[0],
            signals.installs_for(signal.SIGTERM)[-1],
        )
        self.assertEqual(1, self.boundary.app.exit_calls)

    # --- D: runtime failure combined with restoration failure -----------

    def test_runtime_failure_with_restoration_failure_keeps_runtime_primary(
        self,
    ) -> None:
        for failing_positions in ([2], [3], [2, 3]):
            with self.subTest(failing_positions=failing_positions):
                boundary = _Boundary()
                restore_error = ValueError("cannot restore a handler")
                for position in failing_positions:
                    boundary.signals.fail_at[position] = restore_error
                runtime_error = RuntimeError("runtime exploded")
                boundary.app.on_run_forever_error = runtime_error
                with contextlib.ExitStack() as stack:
                    for patcher in (
                        mock.patch.object(
                            process,
                            "load_central_settings",
                            boundary.load,
                        ),
                        mock.patch.object(
                            process, "build_application", boundary.build
                        ),
                        mock.patch.object(
                            process, "signal", boundary.signals
                        ),
                    ):
                        stack.enter_context(patcher)
                    with self.assertRaises(RuntimeError) as raised:
                        process.run_process(boundary.env)
                # The ORIGINAL runtime failure stays the primary
                # escaping failure; the restoration failure never
                # replaces it.
                self.assertIs(runtime_error, raised.exception)
                signals = boundary.signals
                # Both applicable restoration attempts were still made.
                self.assertEqual(4, len(signals.signal_attempts))
                self.assertEqual(
                    (signal.SIGINT, _previous_sigint_handler),
                    signals.signal_attempts[2],
                )
                self.assertEqual(
                    (signal.SIGTERM, signal.SIG_IGN),
                    signals.signal_attempts[3],
                )
                # E7 cleanup still occurred through the context manager.
                self.assertEqual(1, boundary.app.exit_calls)
                self.assertEqual(
                    [RuntimeError], boundary.app.exit_exc_types
                )

    # --- E: build failure combined with restoration failure -------------

    def test_build_failure_with_restoration_failure_keeps_build_primary(
        self,
    ) -> None:
        build_error = OSError("bind failed")
        self.boundary.build_error = build_error
        self.boundary.signals.fail_at[2] = ValueError(
            "cannot restore the SIGINT handler"
        )
        with self.assertRaises(OSError) as raised:
            process.run_process(self.boundary.env)
        self.assertIs(build_error, raised.exception)
        signals = self.boundary.signals
        # Both installs and both restoration attempts were made.
        self.assertEqual(4, len(signals.signal_attempts))
        self.assertEqual(
            (signal.SIGINT, _previous_sigint_handler),
            signals.signal_attempts[2],
        )
        self.assertEqual(
            (signal.SIGTERM, signal.SIG_IGN), signals.signal_attempts[3]
        )
        # The runtime was never called and no application lifecycle
        # was ever entered.
        app = self.boundary.app
        self.assertEqual(0, app.run_forever_calls)
        self.assertEqual(0, app.enter_calls)
        self.assertEqual(0, app.exit_calls)


class MainBoundaryTestCase(unittest.TestCase):
    """The main() environment boundary."""

    def test_main_passes_the_live_os_environ_object_through(self) -> None:
        received: list[Mapping[str, str]] = []

        def fake_run_process(env: Mapping[str, str]) -> None:
            received.append(env)

        with mock.patch.object(process, "run_process", fake_run_process):
            process.main()
        self.assertEqual(1, len(received))
        self.assertIs(os.environ, received[0])


class ProcessPurityTestCase(unittest.TestCase):
    """No hidden reads, no concurrency, no direct E7 cleanup, one
    canonical entrypoint — verified against module structure and the
    patched boundary."""

    def test_run_process_never_reads_the_process_environment(self) -> None:
        class _PoisonEnviron:
            def __getitem__(self, key: object) -> str:
                raise AssertionError(f"run_process read environ[{key!r}]")

            def get(self, key: object, default: object = None) -> str:
                raise AssertionError(f"run_process read environ.get {key!r}")

        boundary = _Boundary()
        with (
            mock.patch.object(
                process, "load_central_settings", boundary.load
            ),
            mock.patch.object(process, "build_application", boundary.build),
            mock.patch.object(process, "signal", boundary.signals),
            mock.patch.object(os, "environ", _PoisonEnviron()),
        ):
            process.run_process(boundary.env)
        self.assertEqual(1, len(boundary.load_calls))

    def test_module_imports_are_bounded_and_serial(self) -> None:
        tree = ast.parse(inspect.getsource(process))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(
            {
                "__future__",
                "collections",
                "os",
                "signal",
                "types",
                "typing",
                "hermes_sentinel",
            },
            roots,
        )

    def test_no_direct_e7_cleanup_or_forbidden_process_apis(self) -> None:
        source = inspect.getsource(process)
        for forbidden in (
            "server_close",
            "shutdown",
            "connection.close",
            "sys.exit",
            "os._exit",
            "signal.pause",
            "atexit",
            "threading",
            "asyncio",
            "multiprocessing",
            "concurrent",
            "ThreadingHTTPServer",
            "dotenv",
            "configparser",
            "tomllib",
            "load_dotenv",
        ):
            self.assertNotIn(forbidden, source, forbidden)

    def test_console_entrypoint_is_exactly_the_process_main(self) -> None:
        pyproject = (
            Path(__file__).resolve().parents[1] / "pyproject.toml"
        )
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
        self.assertEqual(
            {"hermes-sentinel": "hermes_sentinel.process:main"},
            data["project"]["scripts"],
        )


if __name__ == "__main__":
    unittest.main()
