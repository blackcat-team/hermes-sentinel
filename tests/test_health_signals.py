"""Deterministic tests for the Stage D1 health signals core.

Covers heartbeat freshness (MISSING/FRESH/STALE on the central
received_at axis), resource threshold breach semantics, result
immutability/invariants and the D1 architectural boundaries (no host
state resolution, no transitions, no networking, no repository
access). All tests exercise the real production functions with
explicit time — no wall clock, no network, no database.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import health  # noqa: E402
from hermes_sentinel.config import HeartbeatSettings, Thresholds  # noqa: E402
from hermes_sentinel.domain import (  # noqa: E402
    HostTelemetry,
    LoadAverage,
    ResourceUsage,
)
from hermes_sentinel.health import (  # noqa: E402
    HeartbeatFreshness,
    HeartbeatFreshnessResult,
    ResourceAssessment,
    ResourceMetric,
    evaluate_heartbeat_freshness,
    evaluate_resource_thresholds,
)

# Fixed explicit evaluation clock: tests never depend on real time.
_NOW = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)


def _settings(
    expected_interval_seconds: float = 60.0,
    stale_after_seconds: float = 180.0,
) -> HeartbeatSettings:
    return HeartbeatSettings(
        expected_interval_seconds=expected_interval_seconds,
        stale_after_seconds=stale_after_seconds,
    )


def _thresholds(**overrides: object) -> Thresholds:
    values: dict[str, object] = {
        "cpu_percent": 90.0,
        "ram_percent": 90.0,
        "swap_percent": 80.0,
        "disk_percent": 85.0,
        "inode_percent": 90.0,
        "load5_max": 4.0,
    }
    values.update(overrides)
    return Thresholds(**values)  # type: ignore[arg-type]


def _telemetry(**overrides: object) -> HostTelemetry:
    """Valid telemetry snapshot with every metric below threshold."""
    defaults: dict[str, object] = {
        "host": "vds-01",
        "timestamp": datetime(2026, 9, 15, 9, 59, 30, tzinfo=UTC),
        "uptime_seconds": 3600.0,
        "load": LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        "cpu_percent": 12.5,
        "ram": ResourceUsage(used=2.0, total=8.0, percent=25.0),
        "swap": ResourceUsage(used=0.0, total=4.0, percent=0.0),
        "root_filesystem": ResourceUsage(
            used=20.0, total=100.0, percent=20.0
        ),
        "root_inodes": ResourceUsage(
            used=1_000.0, total=10_000.0, percent=10.0
        ),
    }
    defaults.update(overrides)
    return HostTelemetry(**defaults)  # type: ignore[arg-type]


class _NullOffsetTZ(tzinfo):
    """tzinfo whose ``utcoffset()`` is None: effectively naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return "null-offset"


class EnumContractTest(unittest.TestCase):
    def test_freshness_values(self) -> None:
        self.assertEqual(
            {status.value for status in HeartbeatFreshness},
            {"missing", "fresh", "stale"},
        )

    def test_resource_metric_values_and_canonical_order(self) -> None:
        self.assertEqual(
            {metric.value for metric in ResourceMetric},
            {"cpu", "ram", "swap", "disk", "inodes", "load5"},
        )
        self.assertEqual(
            tuple(metric.name for metric in ResourceMetric),
            ("CPU", "RAM", "SWAP", "DISK", "INODES", "LOAD5"),
        )


class HeartbeatFreshnessEvaluationTest(unittest.TestCase):
    def test_missing_heartbeat_is_missing_with_none_age(self) -> None:
        result = evaluate_heartbeat_freshness(
            now=_NOW, latest_received_at=None, settings=_settings()
        )
        self.assertIs(result.status, HeartbeatFreshness.MISSING)
        self.assertIsNone(result.age_seconds)

    def test_zero_age_is_fresh(self) -> None:
        result = evaluate_heartbeat_freshness(
            now=_NOW, latest_received_at=_NOW, settings=_settings()
        )
        self.assertIs(result.status, HeartbeatFreshness.FRESH)
        self.assertEqual(result.age_seconds, 0.0)

    def test_age_below_threshold_is_fresh(self) -> None:
        now = _NOW + timedelta(seconds=179.999)
        result = evaluate_heartbeat_freshness(
            now=now, latest_received_at=_NOW, settings=_settings()
        )
        self.assertIs(result.status, HeartbeatFreshness.FRESH)
        self.assertAlmostEqual(result.age_seconds, 179.999)

    def test_exact_stale_threshold_equality_is_fresh(self) -> None:
        now = _NOW + timedelta(seconds=180.0)
        result = evaluate_heartbeat_freshness(
            now=now, latest_received_at=_NOW, settings=_settings()
        )
        self.assertIs(result.status, HeartbeatFreshness.FRESH)
        self.assertEqual(result.age_seconds, 180.0)

    def test_just_above_threshold_is_stale(self) -> None:
        now = _NOW + timedelta(seconds=180.001)
        result = evaluate_heartbeat_freshness(
            now=now, latest_received_at=_NOW, settings=_settings()
        )
        self.assertIs(result.status, HeartbeatFreshness.STALE)
        self.assertAlmostEqual(result.age_seconds, 180.001)

    def test_far_above_threshold_is_stale(self) -> None:
        now = _NOW + timedelta(seconds=3600.0)
        result = evaluate_heartbeat_freshness(
            now=now, latest_received_at=_NOW, settings=_settings()
        )
        self.assertIs(result.status, HeartbeatFreshness.STALE)
        self.assertEqual(result.age_seconds, 3600.0)

    def test_age_above_expected_interval_but_below_stale_is_fresh(
        self,
    ) -> None:
        # expected_interval_seconds=60, stale_after_seconds=180,
        # age=120: no LATE/WARNING/DEGRADED may be invented.
        now = _NOW + timedelta(seconds=120.0)
        result = evaluate_heartbeat_freshness(
            now=now,
            latest_received_at=_NOW,
            settings=_settings(
                expected_interval_seconds=60.0, stale_after_seconds=180.0
            ),
        )
        self.assertIs(result.status, HeartbeatFreshness.FRESH)
        self.assertEqual(result.age_seconds, 120.0)

    def test_reporter_telemetry_timestamp_does_not_participate(
        self,
    ) -> None:
        # Freshness runs on the central received_at axis only: the
        # evaluation contract accepts no telemetry at all (there is
        # no parameter through which a reported_at could enter).
        params = inspect.signature(
            evaluate_heartbeat_freshness
        ).parameters
        self.assertEqual(
            set(params), {"now", "latest_received_at", "settings"}
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in params.values()
            )
        )

    def test_timezone_offset_equivalence(self) -> None:
        latest_utc = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        now_utc = datetime(2026, 9, 15, 10, 2, 0, tzinfo=UTC)
        result_utc = evaluate_heartbeat_freshness(
            now=now_utc,
            latest_received_at=latest_utc,
            settings=_settings(),
        )
        # Same instants expressed with different UTC offsets.
        latest_shifted = latest_utc.astimezone(
            timezone(timedelta(hours=2))
        )
        now_shifted = now_utc.astimezone(timezone(timedelta(hours=-5)))
        result_shifted = evaluate_heartbeat_freshness(
            now=now_shifted,
            latest_received_at=latest_shifted,
            settings=_settings(),
        )
        self.assertIs(result_utc.status, HeartbeatFreshness.FRESH)
        self.assertEqual(result_utc, result_shifted)
        self.assertIs(result_shifted.status, HeartbeatFreshness.FRESH)
        self.assertEqual(result_shifted.age_seconds, 120.0)

    def test_naive_now_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_heartbeat_freshness(
                now=datetime(2026, 9, 15, 10, 0, 0),
                latest_received_at=_NOW,
                settings=_settings(),
            )

    def test_naive_latest_received_at_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_heartbeat_freshness(
                now=_NOW,
                latest_received_at=datetime(2026, 9, 15, 9, 59, 0),
                settings=_settings(),
            )

    def test_effectively_naive_now_rejected(self) -> None:
        broken = datetime(2026, 9, 15, 12, 0, 0, tzinfo=_NullOffsetTZ())
        with self.assertRaises(ValueError):
            evaluate_heartbeat_freshness(
                now=broken,
                latest_received_at=_NOW,
                settings=_settings(),
            )

    def test_effectively_naive_latest_received_at_rejected(self) -> None:
        broken = datetime(2026, 9, 15, 9, 59, 0, tzinfo=_NullOffsetTZ())
        with self.assertRaises(ValueError):
            evaluate_heartbeat_freshness(
                now=_NOW,
                latest_received_at=broken,
                settings=_settings(),
            )

    def test_future_latest_received_at_rejected(self) -> None:
        future = _NOW + timedelta(seconds=0.001)
        with self.assertRaises(ValueError):
            evaluate_heartbeat_freshness(
                now=_NOW,
                latest_received_at=future,
                settings=_settings(),
            )

    def test_far_future_latest_received_at_rejected(self) -> None:
        far_future = _NOW + timedelta(hours=1)
        with self.assertRaises(ValueError):
            evaluate_heartbeat_freshness(
                now=_NOW,
                latest_received_at=far_future,
                settings=_settings(),
            )

    def test_keyword_only_call_required(self) -> None:
        with self.assertRaises(TypeError):
            evaluate_heartbeat_freshness(  # type: ignore[misc]
                _NOW, _NOW, _settings()
            )

    def test_deterministic_same_inputs_same_result(self) -> None:
        result_a = evaluate_heartbeat_freshness(
            now=_NOW + timedelta(seconds=30),
            latest_received_at=_NOW,
            settings=_settings(),
        )
        result_b = evaluate_heartbeat_freshness(
            now=_NOW + timedelta(seconds=30),
            latest_received_at=_NOW,
            settings=_settings(),
        )
        self.assertEqual(result_a, result_b)

    def test_settings_not_mutated(self) -> None:
        settings = _settings()
        evaluate_heartbeat_freshness(
            now=_NOW, latest_received_at=_NOW, settings=settings
        )
        self.assertEqual(settings, _settings())


class HeartbeatFreshnessResultContractTest(unittest.TestCase):
    def test_result_is_immutable(self) -> None:
        result = evaluate_heartbeat_freshness(
            now=_NOW, latest_received_at=_NOW, settings=_settings()
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.status = HeartbeatFreshness.STALE  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.age_seconds = 5.0  # type: ignore[misc]

    def test_missing_result_age_must_be_none(self) -> None:
        with self.assertRaises(ValueError):
            HeartbeatFreshnessResult(
                status=HeartbeatFreshness.MISSING, age_seconds=1.0
            )

    def test_present_result_age_must_not_be_none(self) -> None:
        for status in (HeartbeatFreshness.FRESH, HeartbeatFreshness.STALE):
            with self.subTest(status=status.value):
                with self.assertRaises(ValueError):
                    HeartbeatFreshnessResult(
                        status=status, age_seconds=None
                    )

    def test_present_result_age_must_be_nonnegative(self) -> None:
        for status in (HeartbeatFreshness.FRESH, HeartbeatFreshness.STALE):
            with self.subTest(status=status.value):
                with self.assertRaises(ValueError):
                    HeartbeatFreshnessResult(
                        status=status, age_seconds=-0.001
                    )

    def test_present_result_age_must_be_finite(self) -> None:
        for bad in (float("inf"), float("-inf"), float("nan")):
            for status in (
                HeartbeatFreshness.FRESH,
                HeartbeatFreshness.STALE,
            ):
                with self.subTest(status=status.value, age=bad):
                    with self.assertRaises(ValueError):
                        HeartbeatFreshnessResult(
                            status=status, age_seconds=bad
                        )


class ResourceThresholdEvaluationTest(unittest.TestCase):
    def test_all_below_threshold_no_breaches(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(), thresholds=_thresholds()
        )
        self.assertEqual(assessment.breaches, ())
        self.assertFalse(assessment.is_breached)

    def test_cpu_equality_is_breach(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(cpu_percent=90.0),
            thresholds=_thresholds(),
        )
        self.assertEqual(assessment.breaches, (ResourceMetric.CPU,))

    def test_ram_equality_is_breach(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(
                ram=ResourceUsage(used=7.2, total=8.0, percent=90.0)
            ),
            thresholds=_thresholds(),
        )
        self.assertEqual(assessment.breaches, (ResourceMetric.RAM,))

    def test_swap_equality_is_breach(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(
                swap=ResourceUsage(used=3.2, total=4.0, percent=80.0)
            ),
            thresholds=_thresholds(),
        )
        self.assertEqual(assessment.breaches, (ResourceMetric.SWAP,))

    def test_disk_equality_is_breach(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(
                root_filesystem=ResourceUsage(
                    used=85.0, total=100.0, percent=85.0
                )
            ),
            thresholds=_thresholds(),
        )
        self.assertEqual(assessment.breaches, (ResourceMetric.DISK,))

    def test_inode_equality_is_breach(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(
                root_inodes=ResourceUsage(
                    used=9_000.0, total=10_000.0, percent=90.0
                )
            ),
            thresholds=_thresholds(),
        )
        self.assertEqual(assessment.breaches, (ResourceMetric.INODES,))

    def test_load5_equality_is_breach_when_enabled(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(
                load=LoadAverage(one=0.1, five=4.0, fifteen=0.3)
            ),
            thresholds=_thresholds(),
        )
        self.assertEqual(assessment.breaches, (ResourceMetric.LOAD5,))

    def test_each_just_below_threshold_not_breached(self) -> None:
        cases = [
            ("cpu", _telemetry(cpu_percent=89.999)),
            (
                "ram",
                _telemetry(
                    ram=ResourceUsage(
                        used=7.19, total=8.0, percent=89.875
                    )
                ),
            ),
            (
                "swap",
                _telemetry(
                    swap=ResourceUsage(
                        used=3.19, total=4.0, percent=79.75
                    )
                ),
            ),
            (
                "disk",
                _telemetry(
                    root_filesystem=ResourceUsage(
                        used=84.9, total=100.0, percent=84.9
                    )
                ),
            ),
            (
                "inodes",
                _telemetry(
                    root_inodes=ResourceUsage(
                        used=8_999.0, total=10_000.0, percent=89.99
                    )
                ),
            ),
            (
                "load5",
                _telemetry(
                    load=LoadAverage(one=0.1, five=3.999, fifteen=0.3)
                ),
            ),
        ]
        for name, telemetry in cases:
            with self.subTest(metric=name):
                assessment = evaluate_resource_thresholds(
                    telemetry=telemetry, thresholds=_thresholds()
                )
                self.assertEqual(assessment.breaches, ())
                self.assertFalse(assessment.is_breached)

    def test_each_above_threshold_breached(self) -> None:
        cases = [
            (_telemetry(cpu_percent=90.001), ResourceMetric.CPU),
            (
                _telemetry(
                    ram=ResourceUsage(used=7.21, total=8.0, percent=90.125)
                ),
                ResourceMetric.RAM,
            ),
            (
                _telemetry(
                    swap=ResourceUsage(
                        used=3.21, total=4.0, percent=80.25
                    )
                ),
                ResourceMetric.SWAP,
            ),
            (
                _telemetry(
                    root_filesystem=ResourceUsage(
                        used=85.1, total=100.0, percent=85.1
                    )
                ),
                ResourceMetric.DISK,
            ),
            (
                _telemetry(
                    root_inodes=ResourceUsage(
                        used=9_001.0, total=10_000.0, percent=90.01
                    )
                ),
                ResourceMetric.INODES,
            ),
            (
                _telemetry(
                    load=LoadAverage(one=0.1, five=4.001, fifteen=0.3)
                ),
                ResourceMetric.LOAD5,
            ),
        ]
        for telemetry, metric in cases:
            with self.subTest(metric=metric.value):
                assessment = evaluate_resource_thresholds(
                    telemetry=telemetry, thresholds=_thresholds()
                )
                self.assertEqual(assessment.breaches, (metric,))
                self.assertTrue(assessment.is_breached)

    def test_load5_none_disables_check(self) -> None:
        telemetry = _telemetry(
            load=LoadAverage(one=500.0, five=500.0, fifteen=500.0)
        )
        assessment = evaluate_resource_thresholds(
            telemetry=telemetry, thresholds=_thresholds(load5_max=None)
        )
        self.assertEqual(assessment.breaches, ())
        self.assertFalse(assessment.is_breached)

    def test_absent_swap_not_breached_under_normal_threshold(self) -> None:
        telemetry = _telemetry(
            swap=ResourceUsage(used=0.0, total=0.0, percent=0.0)
        )
        assessment = evaluate_resource_thresholds(
            telemetry=telemetry, thresholds=_thresholds()
        )
        self.assertNotIn(ResourceMetric.SWAP, assessment.breaches)
        self.assertEqual(assessment.breaches, ())

    def test_multiple_simultaneous_breaches(self) -> None:
        telemetry = _telemetry(
            cpu_percent=95.0,
            ram=ResourceUsage(used=7.9, total=8.0, percent=98.75),
            load=LoadAverage(one=0.1, five=9.5, fifteen=0.3),
        )
        assessment = evaluate_resource_thresholds(
            telemetry=telemetry, thresholds=_thresholds()
        )
        self.assertEqual(
            assessment.breaches,
            (ResourceMetric.CPU, ResourceMetric.RAM, ResourceMetric.LOAD5),
        )

    def test_canonical_breach_order_all_six(self) -> None:
        telemetry = _telemetry(
            cpu_percent=100.0,
            ram=ResourceUsage(used=8.0, total=8.0, percent=100.0),
            swap=ResourceUsage(used=4.0, total=4.0, percent=100.0),
            root_filesystem=ResourceUsage(
                used=100.0, total=100.0, percent=100.0
            ),
            root_inodes=ResourceUsage(
                used=10_000.0, total=10_000.0, percent=100.0
            ),
            load=LoadAverage(one=0.0, five=42.0, fifteen=0.0),
        )
        assessment = evaluate_resource_thresholds(
            telemetry=telemetry, thresholds=_thresholds()
        )
        self.assertEqual(
            assessment.breaches,
            (
                ResourceMetric.CPU,
                ResourceMetric.RAM,
                ResourceMetric.SWAP,
                ResourceMetric.DISK,
                ResourceMetric.INODES,
                ResourceMetric.LOAD5,
            ),
        )

    def test_load_one_and_fifteen_do_not_affect_load5(self) -> None:
        # one/fifteen far above load5_max, five below: no breach.
        telemetry = _telemetry(
            load=LoadAverage(one=500.0, five=1.0, fifteen=500.0)
        )
        assessment = evaluate_resource_thresholds(
            telemetry=telemetry, thresholds=_thresholds()
        )
        self.assertNotIn(ResourceMetric.LOAD5, assessment.breaches)
        # one/fifteen minimal, five above: breach.
        telemetry_b = _telemetry(
            load=LoadAverage(one=0.0, five=5.0, fifteen=0.0)
        )
        assessment_b = evaluate_resource_thresholds(
            telemetry=telemetry_b, thresholds=_thresholds()
        )
        self.assertIn(ResourceMetric.LOAD5, assessment_b.breaches)

    def test_reported_timestamp_does_not_affect_breaches(self) -> None:
        old = _telemetry(timestamp=datetime(2000, 1, 1, tzinfo=UTC))
        new = _telemetry(timestamp=datetime(2099, 1, 1, tzinfo=UTC))
        thresholds = _thresholds()
        self.assertEqual(
            evaluate_resource_thresholds(
                telemetry=old, thresholds=thresholds
            ),
            evaluate_resource_thresholds(
                telemetry=new, thresholds=thresholds
            ),
        )

    def test_telemetry_not_mutated(self) -> None:
        telemetry = _telemetry(cpu_percent=95.0)
        snapshot = _telemetry(cpu_percent=95.0)
        evaluate_resource_thresholds(
            telemetry=telemetry, thresholds=_thresholds()
        )
        self.assertEqual(telemetry, snapshot)

    def test_thresholds_not_mutated(self) -> None:
        thresholds = _thresholds()
        snapshot = _thresholds()
        evaluate_resource_thresholds(
            telemetry=_telemetry(), thresholds=thresholds
        )
        self.assertEqual(thresholds, snapshot)


class ResourceAssessmentContractTest(unittest.TestCase):
    def test_assessment_is_immutable(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(), thresholds=_thresholds()
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            assessment.breaches = ()  # type: ignore[misc]

    def test_breaches_is_a_tuple(self) -> None:
        assessment = evaluate_resource_thresholds(
            telemetry=_telemetry(), thresholds=_thresholds()
        )
        self.assertIsInstance(assessment.breaches, tuple)

    def test_is_breached_false_when_empty(self) -> None:
        self.assertFalse(ResourceAssessment(breaches=()).is_breached)

    def test_is_breached_true_when_non_empty(self) -> None:
        self.assertTrue(
            ResourceAssessment(
                breaches=(ResourceMetric.CPU,)
            ).is_breached
        )

    def test_mutable_breaches_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResourceAssessment(breaches=[ResourceMetric.CPU])  # type: ignore[arg-type]

    def test_non_canonical_order_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResourceAssessment(
                breaches=(ResourceMetric.RAM, ResourceMetric.CPU)
            )

    def test_duplicate_breaches_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResourceAssessment(
                breaches=(ResourceMetric.CPU, ResourceMetric.CPU)
            )

    def test_unknown_metric_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResourceAssessment(breaches=("cpu",))  # type: ignore[arg-type]


class ArchitectureBoundaryTest(unittest.TestCase):
    """D1 stays a pure signal layer: no state, transitions, net, I/O."""

    def setUp(self) -> None:
        self.source = inspect.getsource(health)

    def test_no_host_state_resolution_in_d1(self) -> None:
        self.assertNotIn("HostState", self.source)

    def test_no_host_transition_in_d1(self) -> None:
        self.assertNotIn("HostTransition", self.source)

    def test_no_network_dependency_in_d1(self) -> None:
        for token in ("socket", "httpx", "requests", "asyncio", "urllib"):
            self.assertNotIn(token, self.source)

    def test_no_storage_dependency_in_d1(self) -> None:
        for token in (
            "sqlite",
            "HeartbeatRepository",
            "hermes_sentinel.persistence",
        ):
            self.assertNotIn(token, self.source)

    def test_no_wall_clock_or_sleeping_in_d1(self) -> None:
        for token in (
            "datetime.now",
            "utcnow",
            "monotonic",
            "perf_counter",
            "sleep(",
        ):
            self.assertNotIn(token, self.source)

    def test_d1_imports_only_config_and_domain(self) -> None:
        imported = {
            line.strip().split()[1]
            for line in self.source.splitlines()
            if line.strip().startswith("from hermes_sentinel")
        }
        self.assertEqual(
            imported, {"hermes_sentinel.config", "hermes_sentinel.domain"}
        )


if __name__ == "__main__":
    unittest.main()
