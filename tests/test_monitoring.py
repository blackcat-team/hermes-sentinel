"""Deterministic tests for the Stage E4 one-shot monitoring cycle.

Covers the authoritative E4 contract: the injected-composition
constructor shape (exactly SentinelConfig / HealthEngine /
NotificationCoordinator), the single public ``run`` operation, the
immutable ``HostMonitoringResult`` pairing, empty-configuration
no-op semantics, configured host order preservation, exactly one
engine evaluation and one coordinator notification per host, exact
``evaluation.transition`` pass-through (``None`` transitions stay
``None``, no invented incidents), object-identity preservation of the
evaluations and coordinator return values, unchanged failure
propagation from either collaborator with the remaining hosts of the
run unprocessed and no retries, the module purity boundaries (no
clock, network, persistence, environment or global mutable state),
and one end-to-end composition through the REAL D4 HealthEngine and
the REAL E3 NotificationCoordinator (patched D2 probe, fake B1
repository, fixed clock, recording sender, instrumented E1 mapper).
All values are synthetic and frozen — no network, no sleeps, no
filesystem dependency, no real clock.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_type_hints
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import health_engine, monitoring, notifications  # noqa: E402
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
)
from hermes_sentinel.health_engine import (  # noqa: E402
    HealthEngine,
    HostHealthEvaluation,
)
from hermes_sentinel.incidents import (  # noqa: E402
    Incident,
    IncidentKind,
    incident_from_transition,
)
from hermes_sentinel.monitoring import (  # noqa: E402
    HostMonitoringResult,
    MonitoringCycle,
)
from hermes_sentinel.notifications import NotificationCoordinator  # noqa: E402
from hermes_sentinel.persistence import HeartbeatRecord  # noqa: E402
from hermes_sentinel.reachability import TcpReachability  # noqa: E402
from hermes_sentinel.state_resolver import HostStateResolution  # noqa: E402

_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _transition(
    host: str,
    from_state: HostState = HostState.HEALTHY,
    to_state: HostState = HostState.DOWN,
) -> HostTransition:
    return HostTransition(
        host=host, from_state=from_state, to_state=to_state, at=_NOW
    )


def _evaluation(
    host: str,
    transition: HostTransition | None = None,
    *,
    state: HostState = HostState.HEALTHY,
) -> HostHealthEvaluation:
    """One valid D4 evaluation snapshot with fully synthetic evidence."""
    return HostHealthEvaluation(
        host=host,
        evaluated_at=_NOW,
        freshness=HeartbeatFreshnessResult(
            status=HeartbeatFreshness.FRESH, age_seconds=10.0
        ),
        resources=ResourceAssessment(breaches=()),
        reachability=TcpReachability.REACHABLE,
        resolution=HostStateResolution(state=state),
        transition=transition,
    )


def _host_config(name: str) -> HostConfig:
    return HostConfig(
        name=name,
        heartbeat=HeartbeatSettings(
            expected_interval_seconds=30.0, stale_after_seconds=90.0
        ),
        external=ExternalCheckSettings(
            tcp_host="203.0.113.10", tcp_port=22, timeout_seconds=2.5
        ),
        thresholds=Thresholds(),
    )


def _config(*names: str) -> SentinelConfig:
    return SentinelConfig(hosts=tuple(_host_config(name) for name in names))


class _ScriptedEngine:
    """HealthEngine double at the accepted evaluation seam.

    One scripted outcome per host name — an evaluation object to
    return or a ``BaseException`` to raise unchanged. Every call is
    recorded; an unscripted host is a test defect.
    """

    def __init__(
        self,
        outcomes: dict[str, HostHealthEvaluation | BaseException],
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def evaluate_host(self, host: str) -> HostHealthEvaluation:
        self.calls.append(host)
        outcome = self.outcomes.get(host)
        if outcome is None:
            raise AssertionError(f"unexpected evaluate_host for {host!r}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _DelegatingCoordinator:
    """NotificationCoordinator double that records and delegates.

    Records every received transition (including ``None``) and returns
    exactly what the real E1 mapper verdict implies, remembering each
    return value so object identity stays observable.
    """

    def __init__(self) -> None:
        self.transitions: list[HostTransition | None] = []
        self.returns: list[Incident | None] = []

    def notify_transition(
        self, transition: HostTransition | None
    ) -> Incident | None:
        self.transitions.append(transition)
        result = (
            None if transition is None
            else incident_from_transition(transition)
        )
        self.returns.append(result)
        return result


class _FailingCoordinator:
    """NotificationCoordinator double that fails on every call.

    The exact pre-built failure object is raised after recording, so
    exception identity and stop semantics stay observable.
    """

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.transitions: list[HostTransition | None] = []

    def notify_transition(
        self, transition: HostTransition | None
    ) -> Incident | None:
        self.transitions.append(transition)
        raise self.failure


class CycleApiShapeTest(unittest.TestCase):
    """Constructor and single public operation shape."""

    def test_constructor_takes_exactly_config_engine_coordinator(self) -> None:
        self.assertEqual(
            list(inspect.signature(MonitoringCycle.__init__).parameters),
            ["self", "config", "engine", "coordinator"],
        )

    def test_single_public_operation_run(self) -> None:
        self.assertEqual(
            list(inspect.signature(MonitoringCycle.run).parameters),
            ["self"],
        )
        self.assertEqual(
            get_type_hints(MonitoringCycle.run),
            {"return": tuple[HostMonitoringResult, ...]},
        )
        self.assertEqual(
            [
                name
                for name in dir(MonitoringCycle)
                if not name.startswith("_")
            ],
            ["run"],
        )
        self.assertEqual(
            sorted(monitoring.__all__),
            ["HostMonitoringResult", "MonitoringCycle"],
        )

    def test_cycle_holds_only_the_injected_collaborators(self) -> None:
        self.assertEqual(
            MonitoringCycle.__slots__,
            ("_config", "_engine", "_coordinator"),
        )
        config = _config("vds-01")
        engine = _ScriptedEngine({"vds-01": _evaluation("vds-01")})
        coordinator = _DelegatingCoordinator()
        cycle = MonitoringCycle(config, engine, coordinator)
        self.assertIs(cycle._config, config)
        self.assertIs(cycle._engine, engine)
        self.assertIs(cycle._coordinator, coordinator)
        self.assertFalse(hasattr(cycle, "__dict__"))
        cycle.run()
        self.assertIs(cycle._config, config)
        self.assertIs(cycle._engine, engine)
        self.assertIs(cycle._coordinator, coordinator)
        self.assertFalse(hasattr(cycle, "__dict__"))


class ResultShapeTest(unittest.TestCase):
    """The per-host result is the minimal immutable pairing."""

    def test_fields_are_exactly_evaluation_and_incident(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(HostMonitoringResult)],
            ["evaluation", "incident"],
        )
        self.assertEqual(
            get_type_hints(HostMonitoringResult),
            {
                "evaluation": HostHealthEvaluation,
                "incident": Incident | None,
            },
        )

    def test_result_is_frozen_and_slotted(self) -> None:
        self.assertEqual(
            HostMonitoringResult.__slots__, ("evaluation", "incident")
        )
        result = HostMonitoringResult(
            evaluation=_evaluation("vds-01"), incident=None
        )
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.incident = None  # type: ignore[misc]


class EmptyConfigurationTest(unittest.TestCase):
    """Zero configured hosts is a valid no-op."""

    def test_empty_hosts_produce_zero_calls_and_empty_result(self) -> None:
        engine = _ScriptedEngine({})
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            SentinelConfig(hosts=()), engine, coordinator
        ).run()
        self.assertEqual(results, ())
        self.assertEqual(engine.calls, [])
        self.assertEqual(coordinator.transitions, [])
        self.assertEqual(coordinator.returns, [])


class HostOrderTest(unittest.TestCase):
    """Configured tuple order is preserved end to end."""

    def test_hosts_are_evaluated_in_configured_order(self) -> None:
        outcomes = {
            name: _evaluation(name) for name in ("beta", "alpha", "gamma")
        }
        engine = _ScriptedEngine(outcomes)
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            _config("beta", "alpha", "gamma"), engine, coordinator
        ).run()
        self.assertEqual(engine.calls, ["beta", "alpha", "gamma"])
        self.assertEqual(
            [result.evaluation.host for result in results],
            ["beta", "alpha", "gamma"],
        )

    def test_exactly_one_evaluation_per_host_no_extras(self) -> None:
        names = ("beta", "alpha", "gamma")
        engine = _ScriptedEngine({n: _evaluation(n) for n in names})
        coordinator = _DelegatingCoordinator()
        MonitoringCycle(_config(*names), engine, coordinator).run()
        self.assertEqual(len(engine.calls), len(names))
        self.assertEqual(sorted(engine.calls), sorted(names))


class TransitionPassThroughTest(unittest.TestCase):
    """Each exact evaluation.transition reaches the coordinator once."""

    def test_exact_transition_objects_passed_once_in_order(self) -> None:
        down = _transition("beta")
        degraded_change = _transition(
            "gamma", HostState.HEALTHY, HostState.DEGRADED
        )
        engine = _ScriptedEngine(
            {
                "beta": _evaluation(
                    "beta", down, state=HostState.DOWN
                ),
                "alpha": _evaluation("alpha", None),
                "gamma": _evaluation("gamma", degraded_change),
            }
        )
        coordinator = _DelegatingCoordinator()
        MonitoringCycle(
            _config("beta", "alpha", "gamma"), engine, coordinator
        ).run()
        self.assertEqual(len(coordinator.transitions), 3)
        self.assertIs(coordinator.transitions[0], down)
        self.assertIs(coordinator.transitions[1], None)
        self.assertIs(coordinator.transitions[2], degraded_change)

    def test_none_transition_stays_none_and_invents_no_incident(
        self,
    ) -> None:
        engine = _ScriptedEngine({"alpha": _evaluation("alpha", None)})
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            _config("alpha"), engine, coordinator
        ).run()
        self.assertIsNone(results[0].evaluation.transition)
        self.assertIsNone(results[0].incident)

    def test_non_incident_transition_yields_none_incident(self) -> None:
        change = _transition("gamma", HostState.HEALTHY, HostState.DEGRADED)
        engine = _ScriptedEngine({"gamma": _evaluation("gamma", change)})
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            _config("gamma"), engine, coordinator
        ).run()
        self.assertIs(results[0].evaluation.transition, change)
        self.assertIsNone(results[0].incident)

    def test_incident_worthy_transition_yields_the_mapped_incident(
        self,
    ) -> None:
        down = _transition("beta")
        engine = _ScriptedEngine(
            {"beta": _evaluation("beta", down, state=HostState.DOWN)}
        )
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            _config("beta"), engine, coordinator
        ).run()
        assert results[0].incident is not None
        self.assertIs(results[0].incident.kind, IncidentKind.DOWN)
        self.assertIs(results[0].incident.transition, down)


class IdentityPreservationTest(unittest.TestCase):
    """The exact engine and coordinator objects survive unmodified."""

    def test_evaluation_objects_are_returned_verbatim(self) -> None:
        outcomes = {
            name: _evaluation(name) for name in ("beta", "alpha", "gamma")
        }
        engine = _ScriptedEngine(outcomes)
        results = MonitoringCycle(
            _config("beta", "alpha", "gamma"),
            engine,
            _DelegatingCoordinator(),
        ).run()
        for result in results:
            self.assertIs(result.evaluation, outcomes[result.evaluation.host])

    def test_incident_objects_are_the_exact_coordinator_returns(self) -> None:
        down = _transition("beta")
        recovered = _transition("gamma", HostState.DOWN, HostState.HEALTHY)
        engine = _ScriptedEngine(
            {
                "beta": _evaluation("beta", down, state=HostState.DOWN),
                "gamma": _evaluation("gamma", recovered),
            }
        )
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            _config("beta", "gamma"), engine, coordinator
        ).run()
        self.assertIs(results[0].incident, coordinator.returns[0])
        self.assertIs(results[1].incident, coordinator.returns[1])
        assert results[1].incident is not None
        self.assertIs(results[1].incident.kind, IncidentKind.RECOVERED)
        self.assertIs(results[1].incident.transition, recovered)


class RunCountsTest(unittest.TestCase):
    """One run performs no duplicate work and stays repeatable."""

    def test_single_run_counts_one_evaluation_and_notification_each(
        self,
    ) -> None:
        names = ("beta", "alpha", "gamma")
        engine = _ScriptedEngine({n: _evaluation(n) for n in names})
        coordinator = _DelegatingCoordinator()
        results = MonitoringCycle(
            _config(*names), engine, coordinator
        ).run()
        self.assertEqual(len(results), 3)
        self.assertEqual(len(engine.calls), 3)
        self.assertEqual(len(coordinator.transitions), 3)

    def test_second_run_repeats_the_full_pass_with_no_dedupe(self) -> None:
        names = ("beta", "alpha")
        engine = _ScriptedEngine({n: _evaluation(n) for n in names})
        coordinator = _DelegatingCoordinator()
        cycle = MonitoringCycle(_config(*names), engine, coordinator)
        first = cycle.run()
        second = cycle.run()
        self.assertEqual(
            [r.evaluation.host for r in first], ["beta", "alpha"]
        )
        self.assertEqual(
            [r.evaluation.host for r in second], ["beta", "alpha"]
        )
        self.assertEqual(len(engine.calls), 4)
        self.assertEqual(len(coordinator.transitions), 4)


class EngineFailureStopsRunTest(unittest.TestCase):
    """An engine failure propagates unchanged and ends the run."""

    def test_engine_failure_propagates_as_the_same_object(self) -> None:
        failure = RuntimeError("repository exploded")
        engine = _ScriptedEngine(
            {
                "alpha": _evaluation("alpha"),
                "beta": failure,
                "gamma": _evaluation("gamma"),
            }
        )
        with self.assertRaises(RuntimeError) as ctx:
            MonitoringCycle(
                _config("alpha", "beta", "gamma"),
                engine,
                _DelegatingCoordinator(),
            ).run()
        self.assertIs(ctx.exception, failure)

    def test_engine_failure_stops_remaining_hosts_with_no_retry(self) -> None:
        engine = _ScriptedEngine(
            {
                "alpha": _evaluation("alpha"),
                "beta": RuntimeError("boom"),
                "gamma": _evaluation("gamma"),
            }
        )
        coordinator = _DelegatingCoordinator()
        with self.assertRaises(RuntimeError):
            MonitoringCycle(
                _config("alpha", "beta", "gamma"), engine, coordinator
            ).run()
        # alpha succeeded fully; beta failed at evaluation (no
        # notification for it); gamma was never processed.
        self.assertEqual(engine.calls, ["alpha", "beta"])
        self.assertEqual(len(coordinator.transitions), 1)
        self.assertIsNone(coordinator.transitions[0])


class CoordinatorFailureStopsRunTest(unittest.TestCase):
    """A notification failure propagates unchanged and ends the run."""

    def test_coordinator_failure_propagates_as_the_same_object(self) -> None:
        failure = RuntimeError("delivery exploded")
        engine = _ScriptedEngine({"alpha": _evaluation("alpha")})
        with self.assertRaises(RuntimeError) as ctx:
            MonitoringCycle(
                _config("alpha", "beta"),
                engine,
                _FailingCoordinator(failure),
            ).run()
        self.assertIs(ctx.exception, failure)

    def test_coordinator_failure_stops_remaining_hosts_with_no_retry(
        self,
    ) -> None:
        names = ("alpha", "beta", "gamma")
        engine = _ScriptedEngine({n: _evaluation(n) for n in names})
        coordinator = _FailingCoordinator(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            MonitoringCycle(_config(*names), engine, coordinator).run()
        # alpha was evaluated and notified once (the failing call);
        # beta and gamma were never processed, nothing was retried.
        self.assertEqual(engine.calls, ["alpha"])
        self.assertEqual(len(coordinator.transitions), 1)


class ModuleBoundaryTest(unittest.TestCase):
    """No clock, network, persistence, environment or global state."""

    _ALLOWED_MODULE_NAMES = {
        "__future__",
        "builtins",
        "dataclasses",
        "hermes_sentinel.config",
        "hermes_sentinel.health_engine",
        "hermes_sentinel.incidents",
        "hermes_sentinel.notifications",
    }

    def test_module_imports_only_the_accepted_contracts(self) -> None:
        imported = {
            module.__name__
            for _, module in inspect.getmembers(monitoring, inspect.ismodule)
        }
        self.assertTrue(imported <= self._ALLOWED_MODULE_NAMES, imported)
        for forbidden in (
            "sqlite",
            "urllib",
            "socket",
            "environ",
            "asyncio",
            "threading",
            "HealthEngineError",
            "TelegramSender",
        ):
            self.assertNotIn(forbidden, monitoring.__dict__)

    def test_no_wall_clock_sleep_loop_or_retry_in_source(self) -> None:
        source = inspect.getsource(monitoring)
        for forbidden in (
            "utcnow",
            "now(",
            "time(",
            "sleep",
            "while True",
            "for _ in range",
            "try:",
        ):
            self.assertNotIn(forbidden, source)

    def test_no_global_mutable_state(self) -> None:
        for name, value in vars(monitoring).items():
            if name.startswith("__") or inspect.ismodule(value):
                continue
            if name in ("HostMonitoringResult", "MonitoringCycle"):
                continue
            self.assertFalse(
                isinstance(value, (dict, list, set)),
                f"unexpected mutable module attribute: {name}",
            )


class _IntegrationProbe:
    """D2 probe double at the health_engine seam, keyed by tcp_host."""

    def __init__(
        self, script: dict[str, list[TcpReachability]]
    ) -> None:
        self.script = script
        self.calls: list[str] = []

    def __call__(self, *, settings: ExternalCheckSettings) -> TcpReachability:
        self.calls.append(settings.tcp_host)
        results = self.script[settings.tcp_host]
        return results.pop(0)


class _IntegrationRepository:
    """B1 repository read seam double, keyed by node."""

    def __init__(self) -> None:
        self.latest_by_node: dict[str, HeartbeatRecord | None] = {}
        self.calls: list[str] = []

    def latest_heartbeat(self, node: str) -> HeartbeatRecord | None:
        self.calls.append(node)
        return self.latest_by_node.get(node)


class _RecordingSender:
    """IncidentSender double: records every delivered incident."""

    def __init__(self) -> None:
        self.sent: list[Incident] = []

    def send(self, incident: Incident) -> None:
        self.sent.append(incident)


class _MappingProbe:
    """Count coordinator mapper calls and delegate to the real E1 mapper."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, transition: HostTransition) -> Incident | None:
        self.calls += 1
        return incident_from_transition(transition)


def _integration_host(name: str, tcp_host: str) -> HostConfig:
    return HostConfig(
        name=name,
        heartbeat=HeartbeatSettings(
            expected_interval_seconds=30.0, stale_after_seconds=90.0
        ),
        external=ExternalCheckSettings(
            tcp_host=tcp_host,
            tcp_port=22,
            timeout_seconds=2.5,
            down_confirmations=1,
            recovery_confirmations=2,
        ),
    )


def _record(host: str, *, received_at: datetime) -> HeartbeatRecord:
    """One accepted heartbeat observation with clear resources."""
    telemetry = HostTelemetry(
        host=host,
        timestamp=_NOW - timedelta(seconds=15),
        uptime_seconds=3600.5,
        load=LoadAverage(one=0.1, five=0.2, fifteen=0.3),
        cpu_percent=12.5,
        ram=ResourceUsage(used=1.0, total=2.0, percent=50.0),
        swap=ResourceUsage(used=0.0, total=0.0, percent=0.0),
        root_filesystem=ResourceUsage(
            used=10.0, total=40.0, percent=25.0
        ),
        root_inodes=ResourceUsage(used=100.0, total=1000.0, percent=10.0),
    )
    return HeartbeatRecord(telemetry=telemetry, received_at=received_at)


class RealCompositionTest(unittest.TestCase):
    """End to end through the REAL D4 engine and E3 coordinator.

    Two hosts run twice through one ``MonitoringCycle``: the first run
    is the first evaluation of every host (no transitions by D4
    contract), the second run takes ``vds-01`` into confirmed DOWN
    (missing heartbeat + unreachable TCP + down_confirmations=1) whose
    exact ``evaluation.transition`` flows through the real coordinator
    and E1 mapper into the recording sender exactly once, while
    ``vds-02`` stays healthy with a ``None`` transition.
    """

    def test_two_runs_compose_real_engine_and_coordinator(self) -> None:
        config = SentinelConfig(
            hosts=(
                _integration_host("vds-01", "203.0.113.10"),
                _integration_host("vds-02", "203.0.113.20"),
            )
        )
        repository = _IntegrationRepository()
        repository.latest_by_node["vds-01"] = _record(
            "vds-01", received_at=_NOW - timedelta(seconds=10)
        )
        repository.latest_by_node["vds-02"] = _record(
            "vds-02", received_at=_NOW - timedelta(seconds=10)
        )
        probe = _IntegrationProbe(
            {
                "203.0.113.10": [
                    TcpReachability.REACHABLE,
                    TcpReachability.UNREACHABLE,
                ],
                "203.0.113.20": [
                    TcpReachability.REACHABLE,
                    TcpReachability.REACHABLE,
                ],
            }
        )
        sender = _RecordingSender()
        mapper_probe = _MappingProbe()
        original_mapper = notifications.incident_from_transition
        notifications.incident_from_transition = mapper_probe
        try:
            with mock.patch.object(
                health_engine, "probe_tcp_reachability", new=probe
            ):
                cycle = MonitoringCycle(
                    config,
                    HealthEngine(config, repository, clock=lambda: _NOW),
                    NotificationCoordinator(sender),
                )
                first = cycle.run()
                # First evaluation of every host: no transitions, no
                # incidents, nothing mapped or delivered.
                self.assertEqual(
                    [r.evaluation.host for r in first], ["vds-01", "vds-02"]
                )
                self.assertTrue(
                    all(r.evaluation.transition is None for r in first)
                )
                self.assertTrue(all(r.incident is None for r in first))
                self.assertEqual(sender.sent, [])
                self.assertEqual(mapper_probe.calls, 0)
                # Second run: vds-01 loses its heartbeat AND its TCP
                # evidence; vds-02 keeps both.
                repository.latest_by_node["vds-01"] = None
                second = cycle.run()
        finally:
            notifications.incident_from_transition = original_mapper

        # Second run: only vds-01 transitioned into DOWN.
        self.assertEqual(
            [r.evaluation.host for r in second], ["vds-01", "vds-02"]
        )
        assert second[0].evaluation.transition is not None
        self.assertIs(
            second[0].evaluation.transition.from_state, HostState.HEALTHY
        )
        self.assertIs(
            second[0].evaluation.transition.to_state, HostState.DOWN
        )
        assert second[0].incident is not None
        self.assertIs(second[0].incident.kind, IncidentKind.DOWN)
        self.assertIs(
            second[0].incident.transition,
            second[0].evaluation.transition,
        )
        self.assertIsNone(second[1].evaluation.transition)
        self.assertIsNone(second[1].incident)

        # Exactly one mapping and one delivery for the whole cycle.
        self.assertEqual(mapper_probe.calls, 1)
        self.assertEqual(len(sender.sent), 1)
        self.assertIs(sender.sent[0], second[0].incident)


if __name__ == "__main__":
    unittest.main()
