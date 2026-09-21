"""One-shot monitoring cycle (Stage E4).

The minimal synchronous runtime composition that connects the accepted
Stage D4 ``HealthEngine`` to the accepted Stage E3
``NotificationCoordinator`` for every configured Sentinel host:

    SentinelConfig.hosts (configured tuple order)
                |
    MonitoringCycle.run()
                |  HealthEngine.evaluate_host(host.name)   (D4, once)
                |  NotificationCoordinator.notify_transition(  (E3, once)
                |      evaluation.transition)
                |
    tuple[HostMonitoringResult, ...] (host order preserved)

E4 owns exactly this composition and nothing else. It re-opens none of
the frozen lower contracts: the engine remains the sole evaluation,
state and transition authority, the coordinator (with the E1 mapper
and the injected sender) remains the sole notification authority, and
no freshness, breach, probe, hysteresis, incident or delivery logic is
duplicated here.

Contract points (see docs/ARCHITECTURE.md section 25):

- ``MonitoringCycle`` takes the already-constructed collaborators by
  injection — ``SentinelConfig``, ``HealthEngine`` and
  ``NotificationCoordinator`` — and owns no configuration loading, no
  sender construction and no environment access of its own;
- one ``run`` call is ONE complete pass: the configured hosts are
  iterated in exactly the ``SentinelConfig.hosts`` tuple order, and
  each host is evaluated exactly once through
  ``HealthEngine.evaluate_host(host.name)``;
- the exact ``evaluation.transition`` object is handed to
  ``NotificationCoordinator.notify_transition`` exactly once per host
  — a ``None`` transition stays ``None`` (E4 invents no incidents and
  re-decides no incident-worthiness), and an incident-worthy
  transition produces exactly the ``Incident | None`` the coordinator
  returns;
- the returned result is an ordered immutable tuple of
  ``HostMonitoringResult`` values in the configured host order, each
  preserving the exact ``HostHealthEvaluation`` object and the exact
  coordinator return value (object identity, never a copy);
- an empty host configuration is a valid no-op: zero engine calls,
  zero coordinator calls, an empty tuple back;
- failures are deliberately simple and deterministic: no retry, no
  backoff, no exception translation, no error swallowing. An
  exception raised by the engine or the coordinator propagates
  unchanged, and no later host of that run is processed. E4 defines
  no production-hardening or failure-isolation policy (Stage F
  concerns);
- the cycle holds no state beyond the injected collaborators: it is
  safely reusable across runs, keeps no per-host registry of its own
  (the engine already owns the per-host D3 memory) and performs no
  persistence of any kind.

Out of scope for E4: polling/scheduling, asyncio,
threads, daemon/service lifecycle, the central Sentinel systemd unit,
environment/config-file/Telegram settings loading, retries/backoff,
queueing, dedupe/flap suppression, new persistence, service
monitoring, Hermes integration and remote remediation (later Stage
E/F roadmap units).
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_sentinel.config import SentinelConfig
from hermes_sentinel.health_engine import HealthEngine, HostHealthEvaluation
from hermes_sentinel.incidents import Incident
from hermes_sentinel.notifications import NotificationCoordinator

__all__ = [
    "HostMonitoringResult",
    "MonitoringCycle",
]


@dataclass(frozen=True, slots=True)
class HostMonitoringResult:
    """One host's outcome within a single monitoring cycle.

    Immutable pairing of the exact D4 ``HostHealthEvaluation`` produced
    for this host with the exact ``Incident | None`` the E3 coordinator
    returned for that evaluation's transition. Object identity is
    preserved end to end — neither value is copied, revalidated or
    reconstructed here.
    """

    evaluation: HostHealthEvaluation
    incident: Incident | None


class MonitoringCycle:
    """Synchronous one-shot composition: D4 evaluation -> E3 notification.

    One public operation, ``run``: iterate the configured hosts in
    ``SentinelConfig.hosts`` tuple order, evaluate each exactly once
    through the injected ``HealthEngine``, pass each exact
    ``evaluation.transition`` to the injected ``NotificationCoordinator``
    exactly once, and return the per-host outcomes in the same order.
    """

    __slots__ = ("_config", "_engine", "_coordinator")

    def __init__(
        self,
        config: SentinelConfig,
        engine: HealthEngine,
        coordinator: NotificationCoordinator,
    ) -> None:
        self._config = config
        self._engine = engine
        self._coordinator = coordinator

    def run(self) -> tuple[HostMonitoringResult, ...]:
        """Perform exactly one synchronous pass over all configured hosts.

        For each host, in configured tuple order: one
        ``HealthEngine.evaluate_host(host.name)`` call, then one
        ``NotificationCoordinator.notify_transition(evaluation.transition)``
        call with that exact transition object, then the immutable
        per-host result pairing. An empty configuration returns an
        empty tuple with no collaborator activity.

        This method has no ``try``/``except`` at all: an exception from
        the engine or the coordinator escapes as the original exception
        object, the partially collected results of the failed run are
        simply not returned, and no later host of that run is
        processed. There is no retry, no backoff and no second
        evaluation/notification path.
        """
        results: list[HostMonitoringResult] = []
        for host_config in self._config.hosts:
            evaluation = self._engine.evaluate_host(host_config.name)
            incident = self._coordinator.notify_transition(
                evaluation.transition
            )
            results.append(
                HostMonitoringResult(
                    evaluation=evaluation, incident=incident
                )
            )
        return tuple(results)
