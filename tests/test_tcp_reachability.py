"""Deterministic tests for the Stage D2 external TCP reachability probe.

Every test patches the exact production boundary —
``socket.create_connection`` as used by
``hermes_sentinel.reachability`` — via ``unittest.mock``, so the
suite requires no real network, no DNS, no Internet connectivity
and no wall-clock behaviour, and stays fully deterministic.

Coverage groups: public contract, success semantics, normal network
failure semantics, error boundary, config boundary, statelessness
and architectural no-coupling boundaries, and the network boundary
(no global socket state mutation, no application-level retry).
"""

from __future__ import annotations

import dataclasses
import inspect
import socket as socket_module
import sys
import unittest
from pathlib import Path
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import reachability  # noqa: E402
from hermes_sentinel.config import ExternalCheckSettings  # noqa: E402
from hermes_sentinel.reachability import (  # noqa: E402
    TcpReachability,
    probe_tcp_reachability,
)

#: The exact production seam tests patch. No real connection is
#: ever attempted by this suite.
_PROBE_BOUNDARY = "hermes_sentinel.reachability.socket.create_connection"


def _settings(**overrides: object) -> ExternalCheckSettings:
    values: dict[str, object] = {
        "tcp_host": "vds-01.example.net",
        "tcp_port": 443,
        "timeout_seconds": 2.5,
    }
    values.update(overrides)
    return ExternalCheckSettings(**values)  # type: ignore[arg-type]


class EnumContractTest(unittest.TestCase):
    def test_exact_enum_values(self) -> None:
        self.assertEqual(TcpReachability.REACHABLE.value, "reachable")
        self.assertEqual(TcpReachability.UNREACHABLE.value, "unreachable")

    def test_enum_has_exactly_two_members_in_order(self) -> None:
        self.assertEqual(
            {member.value for member in TcpReachability},
            {"reachable", "unreachable"},
        )
        self.assertEqual(
            tuple(member.name for member in TcpReachability),
            ("REACHABLE", "UNREACHABLE"),
        )


class PublicContractTest(unittest.TestCase):
    def test_settings_is_the_only_argument_and_keyword_only(self) -> None:
        params = inspect.signature(probe_tcp_reachability).parameters
        self.assertEqual(set(params), {"settings"})
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in params.values()
            )
        )

    def test_positional_call_rejected_without_probing(self) -> None:
        with mock.patch(_PROBE_BOUNDARY) as create_connection:
            with self.assertRaises(TypeError):
                probe_tcp_reachability(_settings())  # type: ignore[misc]
            create_connection.assert_not_called()


class SuccessSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch(_PROBE_BOUNDARY)
        self.create_connection = patcher.start()
        self.addCleanup(patcher.stop)
        self.opened_socket = mock.MagicMock(name="opened_socket")
        self.create_connection.return_value = self.opened_socket

    def test_successful_connect_returns_reachable(self) -> None:
        result = probe_tcp_reachability(settings=_settings())
        self.assertIs(result, TcpReachability.REACHABLE)

    def test_exact_target_and_timeout_passed(self) -> None:
        probe_tcp_reachability(
            settings=_settings(
                tcp_host="vds-01.example.net",
                tcp_port=443,
                timeout_seconds=7.25,
            )
        )
        self.create_connection.assert_called_once_with(
            ("vds-01.example.net", 443), timeout=7.25
        )

    def test_opened_socket_is_closed(self) -> None:
        probe_tcp_reachability(settings=_settings())
        self.opened_socket.close.assert_called_once_with()

    def test_no_data_sent_or_received(self) -> None:
        probe_tcp_reachability(settings=_settings())
        for name in (
            "send",
            "sendall",
            "sendto",
            "sendmsg",
            "sendfile",
            "recv",
            "recv_into",
            "recvmsg",
        ):
            with self.subTest(call=name):
                getattr(self.opened_socket, name).assert_not_called()

    def test_exactly_one_application_level_call(self) -> None:
        probe_tcp_reachability(settings=_settings())
        self.assertEqual(self.create_connection.call_count, 1)

    def test_no_sleep_on_success(self) -> None:
        with mock.patch("time.sleep") as sleep:
            probe_tcp_reachability(settings=_settings())
            sleep.assert_not_called()


class FailureSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch(_PROBE_BOUNDARY)
        self.create_connection = patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_unreachable(self, exc: BaseException) -> None:
        self.create_connection.side_effect = exc
        result = probe_tcp_reachability(settings=_settings())
        self.assertIs(result, TcpReachability.UNREACHABLE)

    def test_connection_refused_is_unreachable(self) -> None:
        self._assert_unreachable(ConnectionRefusedError())

    def test_timeout_error_is_unreachable(self) -> None:
        self._assert_unreachable(TimeoutError("connect timed out"))

    def test_socket_timeout_family_is_unreachable(self) -> None:
        # socket.timeout is the socket timeout family; since Python
        # 3.10 it is an alias of TimeoutError and an OSError
        # subclass, so one except clause covers it.
        self.assertIs(socket_module.timeout, TimeoutError)
        self._assert_unreachable(socket_module.timeout("timed out"))

    def test_gaierror_is_unreachable(self) -> None:
        self._assert_unreachable(
            socket_module.gaierror(-2, "Name or service not known")
        )

    def test_generic_oserror_is_unreachable(self) -> None:
        self._assert_unreachable(OSError("network is down"))

    def test_unreachable_errno_variants(self) -> None:
        for exc in (
            OSError(101, "Network is unreachable"),
            OSError(113, "No route to host"),
            socket_module.herror(1, "Unknown host"),
            ConnectionResetError(),
            BrokenPipeError(),
            ConnectionAbortedError(),
        ):
            with self.subTest(exc=repr(exc)):
                self._assert_unreachable(exc)

    def test_failure_family_are_oserror_subclasses(self) -> None:
        # Documents why a single ``except OSError`` covers the whole
        # normal network failure family.
        for exc_type in (
            ConnectionRefusedError,
            TimeoutError,
            socket_module.timeout,
            socket_module.gaierror,
            socket_module.herror,
            ConnectionResetError,
        ):
            with self.subTest(exc_type=exc_type.__name__):
                self.assertTrue(issubclass(exc_type, OSError))

    def test_network_failure_does_not_retry(self) -> None:
        self.create_connection.side_effect = ConnectionRefusedError()
        result = probe_tcp_reachability(settings=_settings())
        self.assertIs(result, TcpReachability.UNREACHABLE)
        self.assertEqual(self.create_connection.call_count, 1)

    def test_no_sleep_or_backoff_on_failure(self) -> None:
        self.create_connection.side_effect = TimeoutError()
        with mock.patch("time.sleep") as sleep:
            result = probe_tcp_reachability(settings=_settings())
            sleep.assert_not_called()
        self.assertIs(result, TcpReachability.UNREACHABLE)


class ErrorBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch(_PROBE_BOUNDARY)
        self.create_connection = patcher.start()
        self.addCleanup(patcher.stop)

    def test_runtime_error_propagates(self) -> None:
        self.create_connection.side_effect = RuntimeError("defect")
        with self.assertRaises(RuntimeError):
            probe_tcp_reachability(settings=_settings())

    def test_value_error_propagates(self) -> None:
        self.create_connection.side_effect = ValueError("defect")
        with self.assertRaises(ValueError):
            probe_tcp_reachability(settings=_settings())

    def test_keyboard_interrupt_propagates(self) -> None:
        self.create_connection.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            probe_tcp_reachability(settings=_settings())

    def test_system_exit_propagates(self) -> None:
        self.create_connection.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            probe_tcp_reachability(settings=_settings())


class ConfigBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch(_PROBE_BOUNDARY)
        self.create_connection = patcher.start()
        self.addCleanup(patcher.stop)
        self.create_connection.return_value = mock.MagicMock()

    def test_tcp_host_passed_verbatim_without_normalization(self) -> None:
        # Mixed case and surrounding whitespace must survive intact:
        # no lowercasing, stripping, URL parsing or substitution.
        host = "  VDS-01.Example.NET  "
        probe_tcp_reachability(settings=_settings(tcp_host=host))
        self.create_connection.assert_called_once_with(
            (host, 443), timeout=2.5
        )

    def test_numeric_ip_host_passed_verbatim(self) -> None:
        probe_tcp_reachability(settings=_settings(tcp_host="203.0.113.7"))
        self.create_connection.assert_called_once_with(
            ("203.0.113.7", 443), timeout=2.5
        )

    def test_tcp_port_passed_verbatim(self) -> None:
        probe_tcp_reachability(settings=_settings(tcp_port=65535))
        self.create_connection.assert_called_once_with(
            ("vds-01.example.net", 65535), timeout=2.5
        )

    def test_timeout_seconds_passed_verbatim(self) -> None:
        probe_tcp_reachability(settings=_settings(timeout_seconds=0.5))
        self.create_connection.assert_called_once_with(
            ("vds-01.example.net", 443), timeout=0.5
        )

    def test_down_confirmations_do_not_affect_success(self) -> None:
        for down in (1, 3, 99):
            with self.subTest(down_confirmations=down):
                self.create_connection.reset_mock()
                result = probe_tcp_reachability(
                    settings=_settings(down_confirmations=down)
                )
                self.assertIs(result, TcpReachability.REACHABLE)
                self.assertEqual(self.create_connection.call_count, 1)

    def test_down_confirmations_do_not_affect_failure(self) -> None:
        self.create_connection.side_effect = ConnectionRefusedError()
        for down in (1, 3, 99):
            with self.subTest(down_confirmations=down):
                self.create_connection.reset_mock()
                result = probe_tcp_reachability(
                    settings=_settings(down_confirmations=down)
                )
                self.assertIs(result, TcpReachability.UNREACHABLE)
                self.assertEqual(self.create_connection.call_count, 1)

    def test_recovery_confirmations_do_not_affect_success(self) -> None:
        for recovery in (1, 2, 50):
            with self.subTest(recovery_confirmations=recovery):
                self.create_connection.reset_mock()
                result = probe_tcp_reachability(
                    settings=_settings(recovery_confirmations=recovery)
                )
                self.assertIs(result, TcpReachability.REACHABLE)
                self.assertEqual(self.create_connection.call_count, 1)

    def test_recovery_confirmations_do_not_affect_failure(self) -> None:
        self.create_connection.side_effect = TimeoutError()
        for recovery in (1, 2, 50):
            with self.subTest(recovery_confirmations=recovery):
                self.create_connection.reset_mock()
                result = probe_tcp_reachability(
                    settings=_settings(recovery_confirmations=recovery)
                )
                self.assertIs(result, TcpReachability.UNREACHABLE)
                self.assertEqual(self.create_connection.call_count, 1)

    def test_settings_object_not_mutated(self) -> None:
        settings = _settings()
        snapshot = _settings()
        probe_tcp_reachability(settings=settings)
        self.assertEqual(settings, snapshot)
        self.assertEqual(
            dataclasses.asdict(settings), dataclasses.asdict(snapshot)
        )


class StatelessnessTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch(_PROBE_BOUNDARY)
        self.create_connection = patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_memory_between_calls(self) -> None:
        # Failure, then success, then failure again: every call is
        # one independent instantaneous observation — an earlier
        # failure never influences a later probe.
        self.create_connection.side_effect = ConnectionRefusedError()
        self.assertIs(
            probe_tcp_reachability(settings=_settings()),
            TcpReachability.UNREACHABLE,
        )
        self.create_connection.side_effect = None
        self.create_connection.return_value = mock.MagicMock()
        self.assertIs(
            probe_tcp_reachability(settings=_settings()),
            TcpReachability.REACHABLE,
        )
        self.create_connection.side_effect = TimeoutError()
        self.assertIs(
            probe_tcp_reachability(settings=_settings()),
            TcpReachability.UNREACHABLE,
        )
        self.assertEqual(self.create_connection.call_count, 3)

    def test_each_probe_is_exactly_one_application_level_call(self) -> None:
        self.create_connection.return_value = mock.MagicMock()
        for _ in range(3):
            probe_tcp_reachability(settings=_settings())
        self.assertEqual(self.create_connection.call_count, 3)


class ArchitectureBoundaryTest(unittest.TestCase):
    """D2 stays a bounded evidence primitive: no state decisions,
    no D1 coupling, no persistence, no notification, no global
    socket state, no application-level retry machinery."""

    def setUp(self) -> None:
        self.source = inspect.getsource(reachability)

    def test_no_host_state_resolution(self) -> None:
        for token in (
            "HostState",
            "HostTransition",
            "HEALTHY",
            "DEGRADED",
            "DOWN",
        ):
            self.assertNotIn(token, self.source)

    def test_no_d1_health_coupling(self) -> None:
        for token in (
            "hermes_sentinel.health",
            "HeartbeatFreshness",
            "ResourceAssessment",
            "evaluate_heartbeat_freshness",
            "evaluate_resource_thresholds",
        ):
            self.assertNotIn(token, self.source)

    def test_no_persistence_coupling(self) -> None:
        for token in (
            "sqlite",
            "HeartbeatRepository",
            "hermes_sentinel.persistence",
        ):
            self.assertNotIn(token, self.source)

    def test_no_notification_coupling(self) -> None:
        for token in ("telegram", "incident"):
            self.assertNotIn(token, self.source)

    def test_no_confirmation_counter_consumption(self) -> None:
        for token in ("down_confirmations", "recovery_confirmations"):
            self.assertNotIn(token, self.source)

    def test_no_third_party_or_application_protocol_dependency(self) -> None:
        for token in (
            "httpx",
            "requests",
            "aiohttp",
            "urllib",
            "asyncio",
            "http",
            "ssl",
            "starttls",
            "wrap_socket",
        ):
            self.assertNotIn(token, self.source)

    def test_create_connection_is_the_only_connection_primitive(self) -> None:
        self.assertIn("socket.create_connection", self.source)
        self.assertNotIn("socket.socket(", self.source)
        self.assertNotIn("bind(", self.source)
        self.assertNotIn("listen(", self.source)

    def test_no_global_socket_state_mutation(self) -> None:
        self.assertNotIn("setdefaulttimeout", self.source)

    def test_no_sleep_retry_or_backoff_machinery(self) -> None:
        for token in ("sleep", "while", "retry", "backoff"):
            self.assertNotIn(token, self.source)

    def test_imports_only_config_from_the_package(self) -> None:
        imported = {
            line.strip().split()[1]
            for line in self.source.splitlines()
            if line.strip().startswith("from hermes_sentinel")
        }
        self.assertEqual(imported, {"hermes_sentinel.config"})


class NetworkBoundaryTest(unittest.TestCase):
    def test_default_socket_timeout_not_mutated(self) -> None:
        before = socket_module.getdefaulttimeout()
        with mock.patch(_PROBE_BOUNDARY) as create_connection:
            create_connection.return_value = mock.MagicMock()
            probe_tcp_reachability(settings=_settings())
        self.assertEqual(socket_module.getdefaulttimeout(), before)

    def test_boundary_is_late_bound_and_patchable(self) -> None:
        # The module must resolve socket.create_connection at call
        # time, so patching the exact seam after import redirects
        # the probe — the deterministic testability contract.
        with mock.patch(_PROBE_BOUNDARY) as create_connection:
            create_connection.side_effect = ConnectionRefusedError()
            result = probe_tcp_reachability(settings=_settings())
        self.assertIs(result, TcpReachability.UNREACHABLE)
        self.assertEqual(create_connection.call_count, 1)


if __name__ == "__main__":
    unittest.main()
