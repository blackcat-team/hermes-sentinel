"""Deterministic tests for the Stage E7 application composition root.

Covers the authoritative E7 contract with constructor doubles patched
onto the composition module's imported names: each accepted
collaborator is constructed exactly once, the exact CentralSettings
values and the exact constructed instances flow through the whole
chain (one SQLite connection, ONE HeartbeatRepository shared by the
B2 ingestor and the D4 engine), build_application performs no
runtime/serving/monitoring/Telegram activity, run_forever delegates
exactly once to the accepted E5 runtime with the exact stop
predicate and propagates runtime exceptions unchanged, close()
attempts server_close (never shutdown) plus the SQLite close without
swallowing cleanup failures, context-manager exit performs the same
owned cleanup, construction failures roll back every already-opened
resource with the ORIGINAL failure always escaping even when a
rollback cleanup itself fails, the repr exposes no secret material,
and no hidden process-environment read exists. One bounded real-graph
test composes the actual accepted B1–E6 components against a
temporary database and an ephemeral listener. No real network,
Telegram or monitoring activity is ever triggered.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import application  # noqa: E402
from hermes_sentinel.application import (  # noqa: E402
    SentinelApplication,
    build_application,
)
from hermes_sentinel.config import (  # noqa: E402
    ExternalCheckSettings,
    HeartbeatSettings,
    HostConfig,
    SentinelConfig,
)
from hermes_sentinel.runtime import SentinelRuntime  # noqa: E402
from hermes_sentinel.settings import CentralSettings  # noqa: E402
from hermes_sentinel.telegram import TelegramSettings  # noqa: E402
from hermes_sentinel.wire import NodeCredentials  # noqa: E402

_HOST_NAME = "alpha"
_BOT_TOKEN = "800111222:AAt-e2e_secret-token"
_NODE_TOKEN = "alpha-node-secret-token"


def _settings() -> CentralSettings:
    """A representative valid aggregate (synthetic secrets only)."""
    return CentralSettings(
        config=SentinelConfig(
            hosts=(
                HostConfig(
                    name=_HOST_NAME,
                    heartbeat=HeartbeatSettings(
                        expected_interval_seconds=60.0,
                        stale_after_seconds=180.0,
                    ),
                    external=ExternalCheckSettings(
                        tcp_host="192.0.2.10", tcp_port=22
                    ),
                ),
            )
        ),
        credentials=NodeCredentials({_HOST_NAME: _NODE_TOKEN}),
        telegram=TelegramSettings(bot_token=_BOT_TOKEN, chat_id=-100999888),
        database_path="e7-composition.sqlite3",
        listen_host="192.0.2.1",
        listen_port=8443,
        monitor_interval_seconds=30.0,
        poll_interval_seconds=0.25,
    )


class _FakeConnection:
    """sqlite3.Connection double: counts close() attempts."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.on_close: Any = None

    def close(self) -> None:
        self.close_calls += 1
        if self.on_close is not None:
            self.on_close()


class _Collaborator:
    """Generic construction double: remembers its exact kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeHeartbeatServer:
    """B5 server double: tracks lifecycle method usage, never serves."""

    def __init__(self, **_kwargs: Any) -> None:
        self.timeout: float | None = None
        self.server_close_calls = 0
        self.on_server_close: Any = None
        self.handle_request_calls = 0
        self.serve_forever_calls = 0
        self.shutdown_calls = 0

    def server_close(self) -> None:
        self.server_close_calls += 1
        if self.on_server_close is not None:
            self.on_server_close()

    def handle_request(self) -> None:
        self.handle_request_calls += 1

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.serve_forever_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeRuntime:
    """E5 runtime double: records run_forever delegations."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.run_forever_predicates: list[Any] = []
        self.on_run_forever: Any = None

    def run_forever(self, should_stop: Any = None) -> None:
        self.run_forever_predicates.append(should_stop)
        if self.on_run_forever is not None:
            self.on_run_forever()


class _FakeSender:
    """E2 sender double: records send() attempts (never expected)."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sent: list[Any] = []

    def send(self, incident: Any) -> None:
        self.sent.append(incident)


class _FakeCycle:
    """E4 monitoring-cycle double: counts run() calls (never expected)."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.run_calls = 0

    def run(self) -> None:
        self.run_calls += 1


#: The constructor names the composition root imports and calls.
_CONSTRUCTORS = (
    "HeartbeatRepository",
    "HeartbeatIngestor",
    "AuthenticatedHeartbeatAdapter",
    "HeartbeatHttpAdapter",
    "create_heartbeat_http_server",
    "HealthEngine",
    "TelegramSender",
    "NotificationCoordinator",
    "MonitoringCycle",
    "SentinelRuntime",
)


class _Composition:
    """One fully patched build_application environment.

    Every collaborator constructor the composition module imports is
    replaced by a recording double: ``calls[name]`` holds the exact
    kwargs of each construction, ``instances[name]`` the constructed
    doubles. ``fail(name, error)`` makes one constructor raise the
    given failure instead of constructing.
    """

    def __init__(self) -> None:
        self.connection = _FakeConnection()
        self.connect_calls: list[Any] = []
        self.calls: dict[str, list[dict[str, Any]]] = {
            name: [] for name in _CONSTRUCTORS
        }
        self.instances: dict[str, list[Any]] = {
            name: [] for name in _CONSTRUCTORS
        }
        self.failures: dict[str, BaseException] = {}
        self.server = _FakeHeartbeatServer()
        doubles: dict[str, Any] = {
            "create_heartbeat_http_server": lambda **_kwargs: self.server,
            "SentinelRuntime": _FakeRuntime,
            "TelegramSender": _FakeSender,
            "MonitoringCycle": _FakeCycle,
        }
        self.constructors: dict[str, Any] = {
            name: self._recording(name, doubles.get(name, _Collaborator))
            for name in _CONSTRUCTORS
        }

    def _recording(self, name: str, construct: Any) -> Any:
        def constructor(**kwargs: Any) -> Any:
            failure = self.failures.get(name)
            if failure is not None:
                raise failure
            self.calls[name].append(kwargs)
            instance = construct(**kwargs)
            self.instances[name].append(instance)
            return instance

        return constructor

    def connect(self, path: Any) -> _FakeConnection:
        self.connect_calls.append(path)
        return self.connection

    def fail(self, name: str, error: BaseException) -> None:
        self.failures[name] = error


class CompositionTestCase(unittest.TestCase):
    """Identity, pass-through, lifecycle and rollback behavior."""

    def setUp(self) -> None:
        self.composition = _Composition()
        self.settings = _settings()
        patchers = [
            mock.patch.object(application, "connect", self.composition.connect)
        ]
        for name, constructor in self.composition.constructors.items():
            patchers.append(
                mock.patch.object(application, name, constructor)
            )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self) -> SentinelApplication:
        return build_application(self.settings)

    # --- construction: once, exact values, shared resources ---------

    def test_build_constructs_each_collaborator_exactly_once(self) -> None:
        self.build()
        self.assertEqual(
            [self.settings.database_path], self.composition.connect_calls
        )
        for name in _CONSTRUCTORS:
            with self.subTest(constructor=name):
                self.assertEqual(1, len(self.composition.calls[name]))

    def test_exact_settings_values_and_instances_flow_through(self) -> None:
        composition = self.composition
        self.build()
        repository = composition.instances["HeartbeatRepository"][0]
        ingestor = composition.instances["HeartbeatIngestor"][0]
        authenticated = composition.instances[
            "AuthenticatedHeartbeatAdapter"
        ][0]
        http_adapter = composition.instances["HeartbeatHttpAdapter"][0]
        engine = composition.instances["HealthEngine"][0]
        sender = composition.instances["TelegramSender"][0]
        coordinator = composition.instances["NotificationCoordinator"][0]
        cycle = composition.instances["MonitoringCycle"][0]
        self.assertEqual(
            {"connection": composition.connection},
            composition.calls["HeartbeatRepository"][0],
        )
        self.assertEqual(
            {"config": self.settings.config, "repository": repository},
            composition.calls["HeartbeatIngestor"][0],
        )
        self.assertEqual(
            {
                "config": self.settings.config,
                "credentials": self.settings.credentials,
                "ingestor": ingestor,
            },
            composition.calls["AuthenticatedHeartbeatAdapter"][0],
        )
        self.assertEqual(
            {"wire": authenticated},
            composition.calls["HeartbeatHttpAdapter"][0],
        )
        self.assertEqual(
            {
                "adapter": http_adapter,
                "host": self.settings.listen_host,
                "port": self.settings.listen_port,
            },
            composition.calls["create_heartbeat_http_server"][0],
        )
        self.assertEqual(
            {"config": self.settings.config, "repository": repository},
            composition.calls["HealthEngine"][0],
        )
        self.assertEqual(
            {"settings": self.settings.telegram},
            composition.calls["TelegramSender"][0],
        )
        self.assertEqual(
            {"sender": sender},
            composition.calls["NotificationCoordinator"][0],
        )
        self.assertEqual(
            {
                "config": self.settings.config,
                "engine": engine,
                "coordinator": coordinator,
            },
            composition.calls["MonitoringCycle"][0],
        )
        self.assertEqual(
            {
                "heartbeat_server": composition.server,
                "monitoring_cycle": cycle,
                "monitor_interval_seconds": (
                    self.settings.monitor_interval_seconds
                ),
                "poll_interval_seconds": self.settings.poll_interval_seconds,
            },
            composition.calls["SentinelRuntime"][0],
        )
        # Identity, not merely equality, for every settings object the
        # aggregate hands down: the accepted instances flow through
        # untouched.
        self.assertIs(
            self.settings.config,
            composition.calls["HeartbeatIngestor"][0]["config"],
        )
        self.assertIs(
            self.settings.config,
            composition.calls["HealthEngine"][0]["config"],
        )
        self.assertIs(
            self.settings.credentials,
            composition.calls["AuthenticatedHeartbeatAdapter"][0][
                "credentials"
            ],
        )
        self.assertIs(
            self.settings.telegram,
            composition.calls["TelegramSender"][0]["settings"],
        )

    def test_one_connection_one_repository_shared_by_write_and_read(
        self,
    ) -> None:
        composition = self.composition
        app = self.build()
        self.assertEqual(1, len(composition.connect_calls))
        self.assertEqual(1, len(composition.calls["HeartbeatRepository"]))
        repository = composition.instances["HeartbeatRepository"][0]
        self.assertIs(
            composition.connection,
            composition.calls["HeartbeatRepository"][0]["connection"],
        )
        write_side = composition.calls["HeartbeatIngestor"][0]["repository"]
        read_side = composition.calls["HealthEngine"][0]["repository"]
        self.assertIs(repository, write_side)
        self.assertIs(write_side, read_side)
        self.assertIs(composition.connection, app.connection)

    def test_application_owns_the_composed_resources(self) -> None:
        app = self.build()
        runtime = self.composition.instances["SentinelRuntime"][0]
        self.assertIs(runtime, app.runtime)
        self.assertIs(self.composition.server, app.heartbeat_server)
        self.assertIs(self.composition.connection, app.connection)

    # --- side-effect boundary ----------------------------------------

    def test_build_starts_no_runtime_serving_monitoring_or_sending(
        self,
    ) -> None:
        self.build()
        server = self.composition.server
        self.assertEqual(0, server.handle_request_calls)
        self.assertEqual(0, server.serve_forever_calls)
        self.assertEqual(0, server.shutdown_calls)
        self.assertEqual(0, server.server_close_calls)
        self.assertEqual(
            [],
            self.composition.instances["SentinelRuntime"][
                0
            ].run_forever_predicates,
        )
        self.assertEqual(
            0, self.composition.instances["MonitoringCycle"][0].run_calls
        )
        self.assertEqual(
            [], self.composition.instances["TelegramSender"][0].sent
        )
        self.assertEqual(0, self.composition.connection.close_calls)

    # --- lifecycle -----------------------------------------------------

    def test_run_forever_delegates_once_with_the_exact_predicate(
        self,
    ) -> None:
        app = self.build()
        runtime = self.composition.instances["SentinelRuntime"][0]

        def stop() -> bool:
            return False

        app.run_forever(stop)
        self.assertEqual([stop], runtime.run_forever_predicates)
        app.run_forever()
        self.assertEqual([stop, None], runtime.run_forever_predicates)

    def test_runtime_exception_propagates_unchanged(self) -> None:
        app = self.build()
        runtime = self.composition.instances["SentinelRuntime"][0]
        error = RuntimeError("runtime exploded")

        def explode() -> None:
            raise error

        runtime.on_run_forever = explode
        with self.assertRaises(RuntimeError) as raised:
            app.run_forever()
        self.assertIs(error, raised.exception)

    def test_close_closes_listener_and_connection_without_shutdown(
        self,
    ) -> None:
        app = self.build()
        app.close()
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)
        self.assertEqual(0, self.composition.server.shutdown_calls)

    def test_close_still_closes_connection_when_listener_close_fails(
        self,
    ) -> None:
        app = self.build()
        failure = ValueError("listener close failed")

        def fail_server_close() -> None:
            raise failure

        self.composition.server.on_server_close = fail_server_close
        with self.assertRaises(ValueError) as raised:
            app.close()
        self.assertIs(failure, raised.exception)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)

    def test_close_surfaces_connection_close_failure(self) -> None:
        app = self.build()
        failure = OSError("sqlite close failed")

        def fail_connection_close() -> None:
            raise failure

        self.composition.connection.on_close = fail_connection_close
        with self.assertRaises(OSError) as raised:
            app.close()
        self.assertIs(failure, raised.exception)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)

    def test_context_manager_exit_closes_both_owned_resources(self) -> None:
        with self.build() as app:
            self.assertIsInstance(app, SentinelApplication)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)
        self.assertEqual(0, self.composition.server.shutdown_calls)

    def test_context_manager_exit_cleans_up_on_exception_too(self) -> None:
        error = RuntimeError("body failed")
        with self.assertRaises(RuntimeError) as raised:
            with self.build():
                raise error
        self.assertIs(error, raised.exception)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)

    def test_repr_exposes_no_secret_material(self) -> None:
        app = self.build()
        text = repr(app)
        self.assertIn("SentinelApplication", text)
        self.assertNotIn(_BOT_TOKEN, text)
        self.assertNotIn(_NODE_TOKEN, text)
        self.assertNotIn(self.settings.database_path, text)
        self.assertNotIn(self.settings.listen_host, text)

    # --- construction failure rollback --------------------------------

    def test_failure_before_binding_closes_connection_only(self) -> None:
        error = ValueError("ingestor rejected")
        self.composition.fail("HeartbeatIngestor", error)
        with self.assertRaises(ValueError) as raised:
            self.build()
        self.assertIs(error, raised.exception)
        self.assertEqual(1, self.composition.connection.close_calls)
        # No listener was ever bound, so no server cleanup may exist.
        self.assertEqual(
            [], self.composition.calls["create_heartbeat_http_server"]
        )
        self.assertEqual(0, self.composition.server.server_close_calls)

    def test_failure_at_listener_bind_closes_connection(self) -> None:
        error = OSError("address already in use")
        self.composition.fail("create_heartbeat_http_server", error)
        with self.assertRaises(OSError) as raised:
            self.build()
        self.assertIs(error, raised.exception)
        self.assertEqual(1, self.composition.connection.close_calls)
        self.assertEqual(0, self.composition.server.server_close_calls)

    def test_failure_after_binding_closes_listener_and_connection(
        self,
    ) -> None:
        error = ValueError("cycle rejected")
        self.composition.fail("MonitoringCycle", error)
        with self.assertRaises(ValueError) as raised:
            self.build()
        self.assertIs(error, raised.exception)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)
        self.assertEqual(0, self.composition.server.shutdown_calls)

    def test_failure_at_runtime_construction_closes_both_resources(
        self,
    ) -> None:
        error = ValueError("interval rejected by E5")
        self.composition.fail("SentinelRuntime", error)
        with self.assertRaises(ValueError) as raised:
            self.build()
        self.assertIs(error, raised.exception)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)

    def test_rollback_cleanup_failure_never_replaces_original_failure(
        self,
    ) -> None:
        error = ValueError("sender rejected")
        self.composition.fail("TelegramSender", error)

        def fail_server_close() -> None:
            raise RuntimeError("secondary server_close failure")

        self.composition.server.on_server_close = fail_server_close
        with self.assertRaises(ValueError) as raised:
            self.build()
        self.assertIs(error, raised.exception)
        # Both rollback cleanups were still attempted.
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)

    def test_rollback_connection_close_failure_never_replaces_original(
        self,
    ) -> None:
        error = ValueError("engine rejected")
        self.composition.fail("HealthEngine", error)

        def fail_connection_close() -> None:
            raise RuntimeError("secondary connection close failure")

        self.composition.connection.on_close = fail_connection_close
        with self.assertRaises(ValueError) as raised:
            self.build()
        self.assertIs(error, raised.exception)
        self.assertEqual(1, self.composition.server.server_close_calls)
        self.assertEqual(1, self.composition.connection.close_calls)

    # --- purity --------------------------------------------------------

    def test_no_hidden_process_environment_read(self) -> None:
        class _PoisonEnviron:
            """Any read attempt fails the test."""

            def __getitem__(self, key: object) -> str:
                raise AssertionError(
                    f"composition read the environment for {key!r}"
                )

            def get(self, key: object, default: object = None) -> str:
                raise AssertionError(
                    f"composition read the environment for {key!r}"
                )

        with mock.patch.object(os, "environ", _PoisonEnviron()):
            app = self.build()
        self.assertIsInstance(app, SentinelApplication)
        source = inspect.getsource(application)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


class RealCompositionTestCase(unittest.TestCase):
    """One bounded real-graph test: the actual accepted B1–E6
    components composed against a temporary database and an ephemeral
    listener — proving the real wiring constructs, binds and cleans
    up, with no runtime ever started."""

    def test_real_components_build_bind_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "sentinel-e7.sqlite3"
            settings = CentralSettings(
                config=SentinelConfig(
                    hosts=(
                        HostConfig(
                            name=_HOST_NAME,
                            heartbeat=HeartbeatSettings(
                                expected_interval_seconds=60.0,
                                stale_after_seconds=180.0,
                            ),
                            external=ExternalCheckSettings(
                                tcp_host="192.0.2.10", tcp_port=22
                            ),
                        ),
                    )
                ),
                credentials=NodeCredentials({_HOST_NAME: _NODE_TOKEN}),
                telegram=TelegramSettings(
                    bot_token=_BOT_TOKEN, chat_id=-100999888
                ),
                database_path=str(database_path),
                listen_host="127.0.0.1",
                listen_port=0,
                monitor_interval_seconds=30.0,
                poll_interval_seconds=0.5,
            )
            with build_application(settings) as app:
                self.assertIsInstance(app, SentinelApplication)
                self.assertIsInstance(app.runtime, SentinelRuntime)
                # Side effect 1 happened: the database was created.
                self.assertTrue(database_path.exists())
                # Side effect 2 happened: the listener is concretely
                # bound (port 0 resolved to an ephemeral port).
                self.assertNotEqual(0, app.heartbeat_server.server_address[1])
            # Context exit closed both owned resources: the listening
            # socket and the SQLite connection.
            self.assertEqual(-1, app.heartbeat_server.socket.fileno())
            with self.assertRaises(sqlite3.ProgrammingError):
                app.connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
