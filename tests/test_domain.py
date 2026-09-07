"""Deterministic tests for the Stage A1 domain contracts."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing the
# package (stdlib unittest has no pythonpath support; pytest gets the
# same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.domain import (  # noqa: E402
    HostState,
    HostTelemetry,
    HostTransition,
    LoadAverage,
    ResourceUsage,
)


def _telemetry(**overrides: object) -> HostTelemetry:
    """Build a valid HostTelemetry snapshot with optional overrides."""
    defaults: dict[str, object] = {
        "host": "vds-01",
        "timestamp": datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC),
        "uptime_seconds": 3600.0,
        "load": LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        "cpu_percent": 12.5,
        "ram": ResourceUsage(used=1.0, total=2.0, percent=50.0),
        "swap": ResourceUsage(used=0.0, total=0.0, percent=0.0),
        "root_filesystem": ResourceUsage(used=10.0, total=40.0, percent=25.0),
        "root_inodes": ResourceUsage(used=100.0, total=1000.0, percent=10.0),
    }
    defaults.update(overrides)
    return HostTelemetry(**defaults)  # type: ignore[arg-type]


class HostStateTest(unittest.TestCase):
    def test_states_are_exactly_the_host_state_semantics(self) -> None:
        self.assertEqual(
            {state.value for state in HostState},
            {"healthy", "degraded", "down"},
        )


class ResourceUsageTest(unittest.TestCase):
    def test_valid_usage(self) -> None:
        usage = ResourceUsage(used=1.0, total=4.0, percent=25.0)
        self.assertEqual(usage.percent, 25.0)

    def test_absent_resource_requires_zero_usage(self) -> None:
        usage = ResourceUsage(used=0.0, total=0.0, percent=0.0)
        self.assertEqual(usage.used, 0.0)

    def test_absent_resource_rejects_nonzero_used(self) -> None:
        with self.assertRaises(ValueError):
            ResourceUsage(used=1.0, total=0.0, percent=0.0)

    def test_absent_resource_rejects_nonzero_percent(self) -> None:
        """Regression: total=0 / used=0 / percent!=0 must be rejected."""
        with self.assertRaises(ValueError):
            ResourceUsage(used=0.0, total=0.0, percent=50.0)

    def test_used_may_not_exceed_total(self) -> None:
        with self.assertRaises(ValueError):
            ResourceUsage(used=5.0, total=4.0, percent=125.0)

    def test_percent_must_be_within_bounds(self) -> None:
        with self.assertRaises(ValueError):
            ResourceUsage(used=1.0, total=4.0, percent=101.0)
        with self.assertRaises(ValueError):
            ResourceUsage(used=-1.0, total=4.0, percent=-1.0)

    def test_rejects_nan_and_infinity(self) -> None:
        with self.assertRaises(ValueError):
            ResourceUsage(used=float("nan"), total=4.0, percent=25.0)
        with self.assertRaises(ValueError):
            ResourceUsage(used=1.0, total=4.0, percent=float("inf"))


class LoadAverageTest(unittest.TestCase):
    def test_valid_load(self) -> None:
        load = LoadAverage(one=0.0, five=1.5, fifteen=2.0)
        self.assertEqual(load.five, 1.5)

    def test_rejects_negative_load(self) -> None:
        with self.assertRaises(ValueError):
            LoadAverage(one=-0.1, five=0.0, fifteen=0.0)


class HostTelemetryTest(unittest.TestCase):
    def test_valid_telemetry_contains_all_mandatory_fields(self) -> None:
        telemetry = _telemetry()
        self.assertEqual(telemetry.host, "vds-01")
        self.assertEqual(telemetry.uptime_seconds, 3600.0)
        self.assertEqual(telemetry.cpu_percent, 12.5)
        self.assertEqual(telemetry.root_filesystem.percent, 25.0)
        self.assertEqual(telemetry.root_inodes.percent, 10.0)

    def test_requires_timezone_aware_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            _telemetry(timestamp=datetime(2026, 9, 7, 5, 0, 0))

    def test_rejects_empty_host(self) -> None:
        with self.assertRaises(ValueError):
            _telemetry(host="")

    def test_rejects_negative_uptime(self) -> None:
        with self.assertRaises(ValueError):
            _telemetry(uptime_seconds=-1.0)

    def test_rejects_out_of_range_cpu_percent(self) -> None:
        with self.assertRaises(ValueError):
            _telemetry(cpu_percent=100.5)


class HostTransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.at = datetime(2026, 9, 7, 5, 0, 0, tzinfo=UTC)

    def _transition(
        self, from_state: HostState, to_state: HostState
    ) -> HostTransition:
        return HostTransition(
            host="vds-01",
            from_state=from_state,
            to_state=to_state,
            at=self.at,
        )

    def test_down_event(self) -> None:
        transition = self._transition(HostState.DEGRADED, HostState.DOWN)
        self.assertTrue(transition.is_down_event)
        self.assertFalse(transition.is_recovery_event)

    def test_recovered_event(self) -> None:
        transition = self._transition(HostState.DOWN, HostState.HEALTHY)
        self.assertFalse(transition.is_down_event)
        self.assertTrue(transition.is_recovery_event)

    def test_degraded_to_healthy_is_neither_down_nor_recovery(self) -> None:
        transition = self._transition(HostState.DEGRADED, HostState.HEALTHY)
        self.assertFalse(transition.is_down_event)
        self.assertFalse(transition.is_recovery_event)

    def test_transition_must_change_state(self) -> None:
        with self.assertRaises(ValueError):
            self._transition(HostState.HEALTHY, HostState.HEALTHY)

    def test_requires_timezone_aware_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            HostTransition(
                host="vds-01",
                from_state=HostState.HEALTHY,
                to_state=HostState.DOWN,
                at=datetime(2026, 9, 7, 5, 0, 0),
            )

    def test_rejects_empty_host(self) -> None:
        with self.assertRaises(ValueError):
            HostTransition(
                host="",
                from_state=HostState.HEALTHY,
                to_state=HostState.DOWN,
                at=self.at,
            )


if __name__ == "__main__":
    unittest.main()
