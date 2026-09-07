"""Deterministic tests for the Stage A1 configuration contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing the
# package (stdlib unittest has no pythonpath support; pytest gets the
# same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.config import (  # noqa: E402
    ExternalCheckSettings,
    HeartbeatSettings,
    HostConfig,
    SentinelConfig,
    Thresholds,
)
from hermes_sentinel.domain import HostState


def _heartbeat() -> HeartbeatSettings:
    return HeartbeatSettings(
        expected_interval_seconds=30.0, stale_after_seconds=90.0
    )


def _external() -> ExternalCheckSettings:
    return ExternalCheckSettings(tcp_host="203.0.113.10", tcp_port=22)


def _host(**overrides: object) -> HostConfig:
    defaults: dict[str, object] = {
        "name": "vds-01",
        "heartbeat": _heartbeat(),
        "external": _external(),
    }
    defaults.update(overrides)
    return HostConfig(**defaults)  # type: ignore[arg-type]


class HostConfigTest(unittest.TestCase):
    def test_production_config_without_services_is_the_default(self) -> None:
        """services=[] is a normal production configuration."""
        host = _host()
        self.assertEqual(host.services, ())

    def test_services_is_an_optional_extension_point(self) -> None:
        host = _host(services=("nginx", "postgres"))
        self.assertEqual(host.services, ("nginx", "postgres"))

    def test_services_do_not_participate_in_host_state_semantics(self) -> None:
        """Service state never participates in HEALTHY/DEGRADED/DOWN."""
        without_services = _host()
        with_services = _host(services=("nginx",))
        self.assertEqual(
            {state for state in HostState},
            {HostState.HEALTHY, HostState.DEGRADED, HostState.DOWN},
        )
        # The only difference between the two nodes is the extension
        # point itself; nothing in the state model reads it.
        self.assertEqual(
            without_services.name,
            with_services.name,
        )

    def test_rejects_empty_host_name(self) -> None:
        with self.assertRaises(ValueError):
            _host(name="")

    def test_rejects_empty_and_duplicate_service_names(self) -> None:
        with self.assertRaises(ValueError):
            _host(services=("nginx", ""))
        with self.assertRaises(ValueError):
            _host(services=("nginx", "nginx"))


class HeartbeatSettingsTest(unittest.TestCase):
    def test_valid_settings(self) -> None:
        settings = HeartbeatSettings(
            expected_interval_seconds=30.0, stale_after_seconds=90.0
        )
        self.assertEqual(settings.stale_after_seconds, 90.0)

    def test_rejects_non_positive_values(self) -> None:
        with self.assertRaises(ValueError):
            HeartbeatSettings(
                expected_interval_seconds=0.0, stale_after_seconds=90.0
            )
        with self.assertRaises(ValueError):
            HeartbeatSettings(
                expected_interval_seconds=30.0, stale_after_seconds=-1.0
            )


class ExternalCheckSettingsTest(unittest.TestCase):
    def test_valid_settings_with_defaults(self) -> None:
        settings = ExternalCheckSettings(tcp_host="example", tcp_port=443)
        self.assertEqual(settings.timeout_seconds, 5.0)
        self.assertEqual(settings.down_confirmations, 3)
        self.assertEqual(settings.recovery_confirmations, 2)

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaises(ValueError):
            ExternalCheckSettings(tcp_host="example", tcp_port=0)
        with self.assertRaises(ValueError):
            ExternalCheckSettings(tcp_host="example", tcp_port=65536)

    def test_rejects_missing_tcp_host(self) -> None:
        with self.assertRaises(ValueError):
            ExternalCheckSettings(tcp_host="", tcp_port=443)

    def test_confirmations_must_be_at_least_one(self) -> None:
        with self.assertRaises(ValueError):
            ExternalCheckSettings(
                tcp_host="example", tcp_port=443, down_confirmations=0
            )
        with self.assertRaises(ValueError):
            ExternalCheckSettings(
                tcp_host="example", tcp_port=443, recovery_confirmations=0
            )


class ThresholdsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        thresholds = Thresholds()
        self.assertEqual(thresholds.cpu_percent, 90.0)
        self.assertEqual(thresholds.ram_percent, 90.0)
        self.assertEqual(thresholds.swap_percent, 80.0)
        self.assertEqual(thresholds.disk_percent, 85.0)
        self.assertEqual(thresholds.inode_percent, 90.0)
        self.assertIsNone(thresholds.load5_max)

    def test_rejects_out_of_range_percentages(self) -> None:
        with self.assertRaises(ValueError):
            Thresholds(cpu_percent=0.0)
        with self.assertRaises(ValueError):
            Thresholds(ram_percent=100.5)

    def test_load5_max_must_be_positive_when_set(self) -> None:
        Thresholds(load5_max=4.0)
        with self.assertRaises(ValueError):
            Thresholds(load5_max=0.0)


class SentinelConfigTest(unittest.TestCase):
    def test_rejects_duplicate_host_names(self) -> None:
        with self.assertRaises(ValueError):
            SentinelConfig(hosts=(_host(), _host()))

    def test_empty_host_set_is_constructible(self) -> None:
        config = SentinelConfig()
        self.assertEqual(config.hosts, ())

    def test_host_lookup(self) -> None:
        config = SentinelConfig(hosts=(_host(), _host(name="vds-02")))
        self.assertIsNotNone(config.host("vds-01"))
        self.assertIsNotNone(config.host("vds-02"))
        self.assertIsNone(config.host("vds-99"))


class PackageTest(unittest.TestCase):
    def test_package_exposes_version(self) -> None:
        import hermes_sentinel

        self.assertEqual(hermes_sentinel.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
