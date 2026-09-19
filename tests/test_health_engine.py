"""Deterministic tests for the Stage D4 health engine.

Covers the public result contract (immutability, shape), the exact
evaluation order (host check, one clock call, clock validation, one
repository read, D1 freshness/resources, one D2 probe, D3 resolution,
transition, commit), the missing-heartbeat neutral-resource rule,
DOWN/recovery transition semantics, per-host state isolation, the
process-local resolver memory (restart reset), all-or-nothing state
committal on failures, the read-only repository boundary and the D4
architectural boundaries (no persistence writes, no direct networking,
no notification coupling, no lower-layer reimplementation).

All dependencies are faked/stubbed at the accepted seams: the
injected clock, the injected repository and the accepted D2 probe
function (patched at the module boundary, the same idiom as the D2
suite). No sleeps, no real network, no wall time, no real clocks.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import sqlite3
import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import health_engine, ingestion  # noqa: E402
from hermes_sentinel.config import (  # noqa: E402
    ExternalCheckSettings,
    HeartbeatSettings,
    HostConfig,
    SentinelConfig,
    Thresholds,
)
from hermes_sentinel.domain import (  # noqa: E402
    HostState,
    HostTelemetry,
    HostTransition,
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
from hermes_sentinel.health_engine import (  # noqa: E402
    HealthEngine,
    HealthEngineError,
    HostHealthEvaluation,
    InvalidClockResultError,
    UnknownHostError,
)
from hermes_sentinel.persistence import (  # noqa: E402
    HeartbeatRecord,
    HeartbeatRepository,
)
from hermes_sentinel.reachability import TcpReachability  # noqa: E402
from hermes_sentinel.state_resolver import (  # noqa: E402
    HostStateResolution,
    resolve_host_state,
)

# Fixed central moments: the single engine evaluation time axis.
_T1 = datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
_T2 = _T1 + timedelta(seconds=15)
_T3 = _T1 + timedelta(seconds=30)
_T4 = _T1 + timedelta(seconds=45)
_T5 = _T1 + timedelta(seconds=60)
_T6 = _T1 + timedelta(seconds=75)
_REPORTED_AT = datetime(2026, 9, 7, 5, 59, 30, tzinfo=UTC)
# 30 seconds before _T1: FRESH under the 90-second stale threshold.
_RECEIVED_AT = datetime(2026, 9, 7, 5, 59, 30, tzinfo=UTC)
# In the future relative to every evaluation moment: D1 fails closed.
_RECEIVED_AT_FUTURE = datetime(2026, 9, 7, 6, 5, 0, tzinfo=UTC)
_OFFSET_TZ = timezone(timedelta(hours=5))

_NEUTRAL = ResourceAssessment(breaches=())


class _NullOffsetTZ(tzinfo):
    """Exotic tzinfo: non-None, but utcoffset() returns None.

    A datetime built with it is effectively naive per authoritative
    Python semantics, even though ``tzinfo is not None``.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return "NullOffset"

    def __repr__(self) -> str:  # deterministic test output
        return "NullOffsetTZ()"


class _ScriptedClock:
    """Injectable engine clock returning scripted values in order.

    The final scripted value repeats for subsequent calls; values may
    be arbitrary objects for the invalid-clock-result tests.
    ``script()`` swaps the queue so a broken clock can be repaired
    mid-test without rebuilding the engine.
    """

    def __init__(self, *values: object) -> None:
        self._values = list(values)
        self.calls = 0

    def script(self, *values: object) -> None:
        self._values = list(values)

    def __call__(self) -> object:
        self.calls += 1
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class _ScriptedProbe:
    """Deterministic D2 stand-in patched over the accepted probe.

    Records every call's settings; the last scripted reachability
    result repeats. Setting ``error`` makes the next call raise it
    (cleared automatically after raising once).
    """

    def __init__(self, *results: TcpReachability) -> None:
        self._results = list(results)
        self.calls: list[ExternalCheckSettings] = []
        self.error: BaseException | None = None

    def script(self, *results: TcpReachability) -> None:
        self._results = list(results)

    def __call__(self, *, settings: ExternalCheckSettings) -> TcpReachability:
        self.calls.append(settings)
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        if not self._results:
            raise AssertionError("probe script exhausted")
        result = self._results.pop(0)
        self._results.append(result)
        return result


class _FakeRepository:
    """Repository stub at the accepted read seam.

    ``latest`` is the scripted record (or None); any other repository
    method records the attempt and fails loudly — D4 must never call
    anything but ``latest_heartbeat``.
    """

    def __init__(self, latest: HeartbeatRecord | None = None) -> None:
        self.latest = latest
        self.latest_calls: list[str] = []
        self.other_calls: list[str] = []
        self.error: BaseException | None = None

    def latest_heartbeat(self, node: str) -> HeartbeatRecord | None:
        self.latest_calls.append(node)
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        return self.latest

    def insert_heartbeat(self, record: HeartbeatRecord) -> int:
        self.other_calls.append("insert_heartbeat")
        raise AssertionError("D4 must not write heartbeats")

    def latest_received_at(self, node: str) -> datetime | None:
        self.other_calls.append("latest_received_at")
        raise AssertionError("D4 reads only latest_heartbeat")

    def count_observations(self) -> int:
        self.other_calls.append("count_observations")
        raise AssertionError("D4 performs no repository scans")


@contextlib.contextmanager
def _patched_probe(script: _ScriptedProbe):
    """Patch the accepted D2 probe at the health_engine boundary."""
    with mock.patch.object(
        health_engine, "probe_tcp_reachability", new=script
    ):
        yield


def _heartbeat() -> HeartbeatSettings:
    return HeartbeatSettings(
        expected_interval_seconds=30.0, stale_after_seconds=90.0
    )


def _external(
    *,
    down_confirmations: int = 3,
    recovery_confirmations: int = 2,
) -> ExternalCheckSettings:
    return ExternalCheckSettings(
        tcp_host="203.0.113.10",
        tcp_port=22,
        timeout_seconds=2.5,
        down_confirmations=down_confirmations,
        recovery_confirmations=recovery_confirmations,
    )


def _thresholds() -> Thresholds:
    return Thresholds(
        cpu_percent=90.0,
        ram_percent=90.0,
        swap_percent=80.0,
        disk_percent=85.0,
        inode_percent=90.0,
        load5_max=None,
    )


def _host_config(
    name: str = "vds-01",
    *,
    thresholds: Thresholds | None = None,
    external: ExternalCheckSettings | None = None,
) -> HostConfig:
    return HostConfig(
        name=name,
        heartbeat=_heartbeat(),
        external=external if external is not None else _external(),
        thresholds=thresholds if thresholds is not None else _thresholds(),
    )


def _config(*hosts: HostConfig) -> SentinelConfig:
    return SentinelConfig(hosts=hosts)


def _record(
    *,
    host: str = "vds-01",
    received_at: datetime = _RECEIVED_AT,
    cpu_percent: float = 12.5,
) -> HeartbeatRecord:
    """One accepted heartbeat observation with clear resources."""
    telemetry = HostTelemetry(
        host=host,
        timestamp=_REPORTED_AT,
        uptime_seconds=3600.5,
        load=LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        cpu_percent=cpu_percent,
        ram=ResourceUsage(used=1.0, total=2.0, percent=50.0),
        swap=ResourceUsage(used=0.0, total=0.0, percent=0.0),
        root_filesystem=ResourceUsage(used=10.0, total=40.0, percent=25.0),
        root_inodes=ResourceUsage(used=100.0, total=1000.0, percent=10.0),
    )
    return HeartbeatRecord(telemetry=telemetry, received_at=received_at)


def _evaluate(
    engine: HealthEngine,
    host: str = "vds-01",
) -> HostHealthEvaluation:
    return engine.evaluate_host(host)


class PublicContractTest(unittest.TestCase):
    """Public result and error shapes of the D4 surface."""

    def test_evaluation_is_frozen_slotted_dataclass(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(HostHealthEvaluation))
        self.assertTrue(HostHealthEvaluation.__dataclass_params__.frozen)
        instance = HostHealthEvaluation(
            host="vds-01",
            evaluated_at=_T1,
            freshness=HeartbeatFreshnessResult(
                status=HeartbeatFreshness.MISSING, age_seconds=None
            ),
            resources=None,
            reachability=TcpReachability.REACHABLE,
            resolution=HostStateResolution(state=HostState.DEGRADED),
            transition=None,
        )
        self.assertNotIn("__dict__", dir(instance))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            instance.host = "other"  # type: ignore[misc]

    def test_evaluation_field_order_matches_contract(self) -> None:
        names = [
            field.name
            for field in dataclasses.fields(HostHealthEvaluation)
        ]
        self.assertEqual(
            names,
            [
                "host",
                "evaluated_at",
                "freshness",
                "resources",
                "reachability",
                "resolution",
                "transition",
            ],
        )

    def test_evaluation_structural_validation(self) -> None:
        kwargs: dict[str, object] = {
            "host": "vds-01",
            "evaluated_at": _T1,
            "freshness": HeartbeatFreshnessResult(
                status=HeartbeatFreshness.MISSING, age_seconds=None
            ),
            "resources": None,
            "reachability": TcpReachability.REACHABLE,
            "resolution": HostStateResolution(state=HostState.DEGRADED),
            "transition": None,
        }
        for field, bad in (
            ("host", ""),
            ("evaluated_at", _T1.replace(tzinfo=None)),
            ("evaluated_at", _T1.replace(tzinfo=_NullOffsetTZ())),
        ):
            with self.subTest(field=field, bad=bad):
                mutated = dict(kwargs)
                mutated[field] = bad
                with self.assertRaises(ValueError):
                    HostHealthEvaluation(**mutated)  # type: ignore[arg-type]

    def test_error_hierarchy_is_bounded(self) -> None:
        self.assertTrue(issubclass(HealthEngineError, Exception))
        self.assertTrue(issubclass(UnknownHostError, HealthEngineError))
        self.assertTrue(
            issubclass(InvalidClockResultError, HealthEngineError)
        )
        # Distinct from the accepted B2 ingestion errors: no shared
        # hierarchy with lower layers.
        self.assertFalse(issubclass(UnknownHostError, ingestion.UnknownNodeError))
        self.assertFalse(
            issubclass(
                InvalidClockResultError, ingestion.InvalidClockResultError
            )
        )

    def test_default_clock_is_the_accepted_utc_now(self) -> None:
        parameter = inspect.signature(HealthEngine.__init__).parameters[
            "clock"
        ]
        self.assertIs(parameter.default, ingestion.utc_now)

    def test_public_exports(self) -> None:
        self.assertEqual(
            set(health_engine.__all__),
            {
                "HostHealthEvaluation",
                "HealthEngineError",
                "UnknownHostError",
                "InvalidClockResultError",
                "HealthEngine",
            },
        )


class EvaluationOrderTest(unittest.TestCase):
    """The exact evaluation order and once-only dependencies."""

    def setUp(self) -> None:
        self.clock = _ScriptedClock(_T1)
        self.repo = _FakeRepository(_record())
        self.probe = _ScriptedProbe(TcpReachability.REACHABLE)
        self.config = _config(_host_config())

    def _engine(self) -> HealthEngine:
        return HealthEngine(self.config, self.repo, self.clock)

    def test_unknown_host_fails_before_all_activity(self) -> None:
        with _patched_probe(self.probe):
            engine = self._engine()
            with self.assertRaises(UnknownHostError) as cm:
                engine.evaluate_host("vds-02")
        self.assertIn("vds-02", str(cm.exception))
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(self.repo.latest_calls, [])
        self.assertEqual(self.probe.calls, [])

    def test_unknown_host_lookup_is_verbatim_case_sensitive(self) -> None:
        with _patched_probe(self.probe):
            engine = self._engine()
            with self.assertRaises(UnknownHostError):
                engine.evaluate_host("VDS-01")
        self.assertEqual(self.repo.latest_calls, [])

    def test_clock_called_exactly_once_per_evaluation(self) -> None:
        with _patched_probe(self.probe):
            engine = self._engine()
            _evaluate(engine)
            self.assertEqual(self.clock.calls, 1)
            _evaluate(engine)
            self.assertEqual(self.clock.calls, 2)

    def test_naive_clock_rejected_before_repository_and_probe(self) -> None:
        self.clock = _ScriptedClock(_T1.replace(tzinfo=None))
        with _patched_probe(self.probe):
            engine = self._engine()
            with self.assertRaises(InvalidClockResultError):
                _evaluate(engine)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.repo.latest_calls, [])
        self.assertEqual(self.probe.calls, [])

    def test_effectively_naive_clock_rejected(self) -> None:
        self.clock = _ScriptedClock(_T1.replace(tzinfo=_NullOffsetTZ()))
        with _patched_probe(self.probe):
            engine = self._engine()
            with self.assertRaises(InvalidClockResultError):
                _evaluate(engine)
        self.assertEqual(self.repo.latest_calls, [])

    def test_non_datetime_clock_results_rejected(self) -> None:
        for bad in ("2026-09-07T06:00:00+00:00", 1757234400, None):
            with self.subTest(bad=bad):
                clock = _ScriptedClock(bad)
                repo = _FakeRepository(_record())
                probe = _ScriptedProbe(TcpReachability.REACHABLE)
                with _patched_probe(probe):
                    engine = HealthEngine(self.config, repo, clock)
                    with self.assertRaises(InvalidClockResultError):
                        engine.evaluate_host("vds-01")
                self.assertEqual(repo.latest_calls, [])
                self.assertEqual(probe.calls, [])

    def test_aware_non_utc_clock_moment_is_valid(self) -> None:
        offset_moment = _T1.astimezone(_OFFSET_TZ)
        clock = _ScriptedClock(offset_moment)
        with _patched_probe(self.probe):
            engine = HealthEngine(self.config, self.repo, clock)
            evaluation = _evaluate(engine)
        self.assertIs(evaluation.evaluated_at, offset_moment)
        self.assertIs(evaluation.freshness.status, HeartbeatFreshness.FRESH)

    def test_latest_heartbeat_called_exactly_once_per_evaluation(self) -> None:
        with _patched_probe(self.probe):
            engine = self._engine()
            _evaluate(engine)
            _evaluate(engine)
        self.assertEqual(self.repo.latest_calls, ["vds-01", "vds-01"])

    def test_exactly_one_probe_per_evaluation_with_host_settings(self) -> None:
        external = _external()
        config = _config(_host_config(external=external))
        with _patched_probe(self.probe):
            engine = HealthEngine(config, self.repo, self.clock)
            _evaluate(engine)
            self.assertEqual(len(self.probe.calls), 1)
            self.assertIs(self.probe.calls[0], external)
            _evaluate(engine)
            self.assertEqual(len(self.probe.calls), 2)
            self.assertIs(self.probe.calls[1], external)


class HeartbeatPresentTest(unittest.TestCase):
    """Heartbeat-present evaluations propagate the D1/D2/D3 truth."""

    def setUp(self) -> None:
        self.host_config = _host_config()
        self.config = _config(self.host_config)

    def _run(
        self,
        record: HeartbeatRecord | None,
        reachability: TcpReachability,
        now: datetime = _T1,
    ) -> HostHealthEvaluation:
        clock = _ScriptedClock(now)
        repo = _FakeRepository(record)
        probe = _ScriptedProbe(reachability)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            return _evaluate(engine)

    def test_healthy_evaluation_propagates_each_layer_result(self) -> None:
        record = _record()
        evaluation = self._run(record, TcpReachability.REACHABLE)
        expected_freshness = evaluate_heartbeat_freshness(
            now=_T1,
            latest_received_at=record.received_at,
            settings=self.host_config.heartbeat,
        )
        expected_resources = evaluate_resource_thresholds(
            telemetry=record.telemetry,
            thresholds=self.host_config.thresholds,
        )
        expected_resolution = resolve_host_state(
            previous=None,
            freshness=expected_freshness.status,
            resources=expected_resources,
            reachability=TcpReachability.REACHABLE,
            settings=self.host_config.external,
        )
        self.assertEqual(evaluation.host, "vds-01")
        self.assertIs(evaluation.evaluated_at, _T1)
        self.assertEqual(evaluation.freshness, expected_freshness)
        self.assertEqual(evaluation.resources, expected_resources)
        self.assertIs(evaluation.reachability, TcpReachability.REACHABLE)
        self.assertEqual(evaluation.resolution, expected_resolution)
        self.assertIs(evaluation.resolution.state, HostState.HEALTHY)
        self.assertIsNone(evaluation.transition)

    def test_stale_freshness_is_propagated(self) -> None:
        stale_record = _record(received_at=_T1 - timedelta(seconds=180))
        evaluation = self._run(stale_record, TcpReachability.REACHABLE)
        self.assertIs(evaluation.freshness.status, HeartbeatFreshness.STALE)
        self.assertEqual(evaluation.freshness.age_seconds, 180.0)
        self.assertIs(evaluation.resolution.state, HostState.DEGRADED)
        self.assertIsNone(evaluation.transition)

    def test_resource_breach_is_propagated(self) -> None:
        evaluation = self._run(
            _record(cpu_percent=95.0), TcpReachability.REACHABLE
        )
        self.assertEqual(
            evaluation.resources,
            ResourceAssessment(breaches=(ResourceMetric.CPU,)),
        )
        self.assertIs(evaluation.resolution.state, HostState.DEGRADED)
        self.assertIsNone(evaluation.transition)

    def test_unreachable_evidence_is_propagated(self) -> None:
        # STALE heartbeat + UNREACHABLE TCP is a qualifying DOWN
        # observation: first evaluation shows the pending debounce
        # streak, and no transition exists to emit from.
        evaluation = self._run(
            _record(received_at=_T1 - timedelta(seconds=180)),
            TcpReachability.UNREACHABLE,
        )
        self.assertIs(evaluation.reachability, TcpReachability.UNREACHABLE)
        self.assertIs(evaluation.freshness.status, HeartbeatFreshness.STALE)
        self.assertIs(evaluation.resolution.state, HostState.DEGRADED)
        self.assertEqual(evaluation.resolution.down_failures, 1)
        self.assertIsNone(evaluation.transition)


class HeartbeatMissingTest(unittest.TestCase):
    """Missing-heartbeat evaluations: public None + neutral internals."""

    def setUp(self) -> None:
        self.host_config = _host_config()
        self.config = _config(self.host_config)

    def _run(
        self, reachability: TcpReachability
    ) -> HostHealthEvaluation:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(reachability)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            return _evaluate(engine)

    def test_missing_heartbeat_public_resources_none(self) -> None:
        evaluation = self._run(TcpReachability.REACHABLE)
        self.assertIsNone(evaluation.resources)
        self.assertIs(evaluation.freshness.status, HeartbeatFreshness.MISSING)
        self.assertIsNone(evaluation.freshness.age_seconds)
        # MISSING + REACHABLE resolves DEGRADED, never DOWN — the
        # resolver saw the neutral non-degrading resource input.
        self.assertEqual(
            evaluation.resolution,
            resolve_host_state(
                previous=None,
                freshness=HeartbeatFreshness.MISSING,
                resources=_NEUTRAL,
                reachability=TcpReachability.REACHABLE,
                settings=self.host_config.external,
            ),
        )
        self.assertIs(evaluation.resolution.state, HostState.DEGRADED)
        self.assertIsNone(evaluation.transition)

    def test_neutral_resource_input_never_invents_escalation(self) -> None:
        # With the heartbeat missing, resources cannot participate:
        # repeated REACHABLE evaluations stay DEGRADED forever — the
        # neutral internal input fabricates neither HEALTHY, nor a
        # DOWN qualification, nor streak drift.
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.REACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            for _ in range(5):
                evaluation = _evaluate(engine)
                self.assertIsNone(evaluation.resources)
                self.assertIs(
                    evaluation.resolution.state, HostState.DEGRADED
                )
                self.assertEqual(evaluation.resolution.down_failures, 0)
                self.assertEqual(
                    evaluation.resolution.recovery_successes, 0
                )
                self.assertIsNone(evaluation.transition)

    def test_missing_plus_unreachable_qualifies_for_down(self) -> None:
        # Absent resources never block DOWN qualification either: the
        # D3 predicate is freshness + reachability only.
        self.config = _config(
            _host_config(external=_external(down_confirmations=1))
        )
        evaluation = self._run(TcpReachability.UNREACHABLE)
        self.assertIsNone(evaluation.resources)
        self.assertIs(evaluation.resolution.state, HostState.DOWN)
        # First evaluation: DOWN without a transition.
        self.assertIsNone(evaluation.transition)


class TransitionTest(unittest.TestCase):
    """Transitions only on entering/leaving DOWN."""

    def setUp(self) -> None:
        self.config = _config(_host_config())

    def test_entering_and_leaving_down_emit_transitions(self) -> None:
        clock = _ScriptedClock(_T1, _T2, _T3, _T4, _T5, _T6)
        repo = _FakeRepository(_record())
        probe = _ScriptedProbe(TcpReachability.REACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            healthy = _evaluate(engine)
            self.assertIs(healthy.resolution.state, HostState.HEALTHY)
            self.assertIsNone(healthy.transition)

            # Heartbeat lost and TCP failing: the down debounce runs.
            repo.latest = None
            probe.script(TcpReachability.UNREACHABLE)
            first = _evaluate(engine)
            second = _evaluate(engine)
            self.assertIs(first.resolution.state, HostState.DEGRADED)
            self.assertEqual(first.resolution.down_failures, 1)
            self.assertIsNone(first.transition)
            self.assertIs(second.resolution.state, HostState.DEGRADED)
            self.assertEqual(second.resolution.down_failures, 2)
            self.assertIsNone(second.transition)

            # Third consecutive qualifying observation confirms DOWN.
            confirmed = _evaluate(engine)
            self.assertIs(confirmed.resolution.state, HostState.DOWN)
            transition = confirmed.transition
            self.assertIsNotNone(transition)
            self.assertEqual(
                transition,
                HostTransition(
                    host="vds-01",
                    from_state=HostState.DEGRADED,
                    to_state=HostState.DOWN,
                    at=confirmed.evaluated_at,
                ),
            )
            self.assertTrue(transition.is_down_event)
            self.assertIs(transition.at, confirmed.evaluated_at)

            # Recovery debounce: one REACHABLE probe holds DOWN.
            probe.script(TcpReachability.REACHABLE)
            held = _evaluate(engine)
            self.assertIs(held.resolution.state, HostState.DOWN)
            self.assertEqual(held.resolution.recovery_successes, 1)
            self.assertIsNone(held.transition)

            # Second consecutive REACHABLE probe leaves DOWN.
            recovered = _evaluate(engine)
            self.assertIs(recovered.resolution.state, HostState.DEGRADED)
            recovery = recovered.transition
            self.assertIsNotNone(recovery)
            self.assertEqual(
                recovery,
                HostTransition(
                    host="vds-01",
                    from_state=HostState.DOWN,
                    to_state=HostState.DEGRADED,
                    at=recovered.evaluated_at,
                ),
            )
            self.assertTrue(recovery.is_recovery_event)
            self.assertIs(recovery.at, recovered.evaluated_at)

    def test_healthy_degraded_flips_never_emit_transitions(self) -> None:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(_record(cpu_percent=12.5))
        probe = _ScriptedProbe(TcpReachability.REACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            healthy = _evaluate(engine)
            self.assertIs(healthy.resolution.state, HostState.HEALTHY)

            repo.latest = _record(cpu_percent=95.0)
            degraded = _evaluate(engine)
            self.assertIs(degraded.resolution.state, HostState.DEGRADED)
            self.assertIsNone(degraded.transition)

            repo.latest = _record(cpu_percent=12.5)
            healthy_again = _evaluate(engine)
            self.assertIs(healthy_again.resolution.state, HostState.HEALTHY)
            self.assertIsNone(healthy_again.transition)


class StateMemoryTest(unittest.TestCase):
    """Per-host retention, isolation, advance and restart reset."""

    def test_down_streak_resets_on_predicate_break(self) -> None:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        config = _config(_host_config())
        with _patched_probe(probe):
            engine = HealthEngine(config, repo, clock)
            first = _evaluate(engine)
            self.assertEqual(first.resolution.down_failures, 1)

            probe.script(TcpReachability.REACHABLE)
            reachable = _evaluate(engine)
            self.assertIs(reachable.resolution.state, HostState.DEGRADED)
            self.assertEqual(reachable.resolution.down_failures, 0)

            probe.script(TcpReachability.UNREACHABLE)
            again = _evaluate(engine)
            self.assertEqual(again.resolution.down_failures, 1)

    def test_per_host_state_isolation(self) -> None:
        config = _config(
            _host_config(
                "vds-01", external=_external(down_confirmations=2)
            ),
            _host_config(
                "vds-02", external=_external(down_confirmations=2)
            ),
        )
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(config, repo, clock)

            # Independent first evaluations: each host carries its own
            # pending streak (a shared memory would show 2 here).
            first_host = engine.evaluate_host("vds-01")
            self.assertIs(first_host.resolution.state, HostState.DEGRADED)
            self.assertEqual(first_host.resolution.down_failures, 1)
            second_host = engine.evaluate_host("vds-02")
            self.assertIs(second_host.resolution.state, HostState.DEGRADED)
            self.assertEqual(second_host.resolution.down_failures, 1)

            # vds-01 reaches DOWN; vds-02's later evaluations are not
            # dragged into the DOWN hold by vds-01's memory.
            vds_01_down = engine.evaluate_host("vds-01")
            self.assertIs(vds_01_down.resolution.state, HostState.DOWN)
            self.assertIsNotNone(vds_01_down.transition)

            probe.script(TcpReachability.REACHABLE)
            vds_02_reachable = engine.evaluate_host("vds-02")
            self.assertIs(vds_02_reachable.resolution.state, HostState.DEGRADED)
            self.assertEqual(vds_02_reachable.resolution.down_failures, 0)
            self.assertIsNone(vds_02_reachable.transition)

            # vds-02 restarts its own debounce from zero (not from
            # vds-01's confirmed DOWN or its own earlier streak).
            probe.script(TcpReachability.UNREACHABLE)
            vds_02_again = engine.evaluate_host("vds-02")
            self.assertIs(vds_02_again.resolution.state, HostState.DEGRADED)
            self.assertEqual(vds_02_again.resolution.down_failures, 1)
            self.assertIsNone(vds_02_again.transition)

    def test_new_engine_instance_resets_in_memory_history(self) -> None:
        config = _config(
            _host_config(
                external=_external(
                    down_confirmations=1, recovery_confirmations=2
                )
            )
        )
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        with _patched_probe(probe):
            engine_one = HealthEngine(config, repo, clock)
            down = _evaluate(engine_one)
            self.assertIs(down.resolution.state, HostState.DOWN)
            self.assertIsNone(down.transition)

            probe.script(TcpReachability.REACHABLE)
            holding = _evaluate(engine_one)
            self.assertIs(holding.resolution.state, HostState.DOWN)
            self.assertEqual(holding.resolution.recovery_successes, 1)

        # A fresh engine starts from previous=None: the very same
        # evidence yields a first-evaluation result with no memory of
        # the old DOWN/recovery streak (a persisted memory would exit
        # DOWN here and emit a recovery transition).
        fresh_probe = _ScriptedProbe(TcpReachability.REACHABLE)
        with _patched_probe(fresh_probe):
            engine_two = HealthEngine(config, _FakeRepository(None), clock)
            first = _evaluate(engine_two)
        self.assertIs(first.resolution.state, HostState.DEGRADED)
        self.assertEqual(first.resolution.recovery_successes, 0)
        self.assertEqual(first.resolution.down_failures, 0)
        self.assertIsNone(first.transition)


class AtomicityTest(unittest.TestCase):
    """Failures before completion leave remembered state intact."""

    def setUp(self) -> None:
        self.config = _config(_host_config())

    def test_probe_failure_leaves_state_unchanged(self) -> None:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            first = _evaluate(engine)
            self.assertEqual(first.resolution.down_failures, 1)

            error = RuntimeError("probe defect")
            probe.error = error
            with self.assertRaises(RuntimeError) as cm:
                _evaluate(engine)
            # Propagates unchanged — no exception wrapping.
            self.assertIs(cm.exception, error)

            third = _evaluate(engine)
        # The failed evaluation advanced nothing: the streak continues
        # from the last committed value (2, not a confirmed DOWN 3).
        self.assertIs(third.resolution.state, HostState.DEGRADED)
        self.assertEqual(third.resolution.down_failures, 2)
        self.assertIsNone(third.transition)

    def test_repository_failure_leaves_state_unchanged(self) -> None:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            _evaluate(engine)  # streak 1 committed

            repo.error = RuntimeError("repository read failed")
            with self.assertRaises(RuntimeError):
                _evaluate(engine)
            self.assertEqual(len(repo.latest_calls), 2)

            third = _evaluate(engine)
        self.assertIs(third.resolution.state, HostState.DEGRADED)
        self.assertEqual(third.resolution.down_failures, 2)
        self.assertIsNone(third.transition)

    def test_invalid_clock_failure_leaves_state_unchanged(self) -> None:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            _evaluate(engine)  # streak 1 committed

            clock.script(_T1.replace(tzinfo=None))
            with self.assertRaises(InvalidClockResultError):
                _evaluate(engine)
            # The failure hit before the repository read step.
            self.assertEqual(len(repo.latest_calls), 1)

            clock.script(_T1)
            third = _evaluate(engine)
            self.assertEqual(len(repo.latest_calls), 2)
        self.assertIs(third.resolution.state, HostState.DEGRADED)
        self.assertEqual(third.resolution.down_failures, 2)
        self.assertIsNone(third.transition)

    def test_freshness_failure_leaves_state_unchanged(self) -> None:
        clock = _ScriptedClock(_T1)
        repo = _FakeRepository(None)
        probe = _ScriptedProbe(TcpReachability.UNREACHABLE)
        with _patched_probe(probe):
            engine = HealthEngine(self.config, repo, clock)
            _evaluate(engine)  # streak 1 committed, 1 probe call

            # A latest_received_at in the future of the clock moment
            # is inconsistent clock evidence: D1 fails closed with its
            # own ValueError, which must propagate unwrapped.
            repo.latest = _record(received_at=_RECEIVED_AT_FUTURE)
            with self.assertRaises(ValueError) as cm:
                _evaluate(engine)
            self.assertNotIsInstance(cm.exception, HealthEngineError)
            # The failure hit before the D2 step.
            self.assertEqual(len(probe.calls), 1)

            repo.latest = None
            third = _evaluate(engine)
        self.assertIs(third.resolution.state, HostState.DEGRADED)
        self.assertEqual(third.resolution.down_failures, 2)
        self.assertIsNone(third.transition)


class PersistenceBoundaryTest(unittest.TestCase):
    """D4 is read-only with respect to repository persistence."""

    def test_real_repository_row_count_is_untouched(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = HeartbeatRepository(connection)
        repository.insert_heartbeat(_record())
        probe = _ScriptedProbe(TcpReachability.REACHABLE)
        config = _config(_host_config())
        with _patched_probe(probe):
            engine = HealthEngine(config, repository, _ScriptedClock(_T1))
            first = _evaluate(engine)
            _evaluate(engine)
        # The accepted real read path fed D1, and nothing was written.
        self.assertIs(first.freshness.status, HeartbeatFreshness.FRESH)
        self.assertEqual(repository.count_observations(), 1)

    def test_fake_repository_write_spies_never_called(self) -> None:
        repo = _FakeRepository(_record())
        probe = _ScriptedProbe(TcpReachability.REACHABLE)
        config = _config(_host_config())
        with _patched_probe(probe):
            engine = HealthEngine(config, repo, _ScriptedClock(_T1))
            _evaluate(engine)
            _evaluate(engine)
        self.assertEqual(repo.other_calls, [])


class ArchitectureBoundaryTest(unittest.TestCase):
    """D4 stays a bounded orchestrator over the accepted layers."""

    def setUp(self) -> None:
        self.source = inspect.getsource(health_engine)

    def test_imports_only_authoritative_contracts(self) -> None:
        imported = {
            line.strip().split()[1]
            for line in self.source.splitlines()
            if line.strip().startswith("from hermes_sentinel")
        }
        self.assertEqual(
            imported,
            {
                "hermes_sentinel.config",
                "hermes_sentinel.domain",
                "hermes_sentinel.health",
                "hermes_sentinel.ingestion",
                "hermes_sentinel.persistence",
                "hermes_sentinel.reachability",
                "hermes_sentinel.state_resolver",
            },
        )

    def test_no_direct_persistence_writes(self) -> None:
        for token in (
            "sqlite3",
            "initialize_schema",
            "insert_heartbeat",
            "repository.latest_received_at",
            ".execute(",
            "executemany",
            "CREATE TABLE",
            "commit(",
        ):
            self.assertNotIn(token, self.source)

    def test_no_direct_network_dependency(self) -> None:
        # D2 is consumed only through its accepted function.
        for token in (
            "socket",
            "create_connection",
            "urllib",
            "requests",
            "httpx",
            "getaddrinfo",
            "gethostbyname",
        ):
            self.assertNotIn(token, self.source)

    def test_no_remediation_or_process_execution(self) -> None:
        for token in ("subprocess", "os.system", "systemctl", "SSH", "reboot"):
            self.assertNotIn(token, self.source)

    def test_no_notification_coupling(self) -> None:
        for token in ("telegram", "incident"):
            self.assertNotIn(token, self.source)

    def test_no_concurrency_or_scheduling_loop(self) -> None:
        for token in (
            "threading",
            "Thread",
            "asyncio",
            "sleep(",
            "Timer(",
            "while True",
        ):
            self.assertNotIn(token, self.source)

    def test_no_lower_layer_reimplementation(self) -> None:
        # Freshness/breach/hysteresis/probe-target logic and every
        # lower-layer settings field stay in their owning stages; the
        # engine passes the frozen settings objects through verbatim.
        for token in (
            "stale_after_seconds",
            "expected_interval_seconds",
            "cpu_percent",
            "ram_percent",
            "swap_percent",
            "disk_percent",
            "inode_percent",
            "load5_max",
            "tcp_host",
            "tcp_port",
            "timeout_seconds",
            "down_confirmations",
            "recovery_confirmations",
            "_base_state",
            "_is_down_observation",
            "down_failures + 1",
            "recovery_successes + 1",
        ):
            self.assertNotIn(token, self.source)

    def test_no_mutable_module_state(self) -> None:
        # The per-host resolver memory is instance state only; the
        # module surface is the public API plus the imported accepted
        # contracts, and none of it is a mutable container.
        allowed = set(health_engine.__all__) | {
            "annotations",
            "dataclass",
            "datetime",
            "SentinelConfig",
            "HostState",
            "HostTransition",
            "HeartbeatFreshnessResult",
            "ResourceAssessment",
            "evaluate_heartbeat_freshness",
            "evaluate_resource_thresholds",
            "Clock",
            "utc_now",
            "HeartbeatRepository",
            "TcpReachability",
            "probe_tcp_reachability",
            "HostStateResolution",
            "resolve_host_state",
        }
        for name, value in vars(health_engine).items():
            if name.startswith("_"):
                continue
            self.assertIn(name, allowed)
            self.assertNotIsInstance(value, (dict, list, set))


if __name__ == "__main__":
    unittest.main()
