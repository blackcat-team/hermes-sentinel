"""Deterministic tests for the Stage D3 host state resolver.

Covers the HostStateResolution contract (immutability, structural
invariants), the base instantaneous state matrix, DOWN debounce, the
confirmed DOWN hold, recovery hysteresis, counter canonicalization,
statelessness/purity, end-to-end pure sequences and the D3
architectural boundaries (no transitions, no networking, no D1
re-evaluation, no persistence, no wall clock, no per-host registry).
All tests exercise the real production resolver with synthetic D1/D2
evidence — no network, no database, no real clock, no sleeps.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import state_resolver  # noqa: E402
from hermes_sentinel.config import ExternalCheckSettings  # noqa: E402
from hermes_sentinel.domain import HostState  # noqa: E402
from hermes_sentinel.health import (  # noqa: E402
    HeartbeatFreshness,
    ResourceAssessment,
    ResourceMetric,
)
from hermes_sentinel.reachability import TcpReachability  # noqa: E402
from hermes_sentinel.state_resolver import (  # noqa: E402
    HostStateResolution,
    resolve_host_state,
)

_CLEAR = ResourceAssessment(breaches=())
_BREACHED = ResourceAssessment(breaches=(ResourceMetric.CPU,))


def _settings(
    down_confirmations: int = 3,
    recovery_confirmations: int = 2,
) -> ExternalCheckSettings:
    return ExternalCheckSettings(
        tcp_host="vds-01.example.net",
        tcp_port=443,
        timeout_seconds=2.5,
        down_confirmations=down_confirmations,
        recovery_confirmations=recovery_confirmations,
    )


def _resolve(
    previous: HostStateResolution | None,
    freshness: HeartbeatFreshness,
    reachability: TcpReachability,
    *,
    resources: ResourceAssessment = _CLEAR,
    settings: ExternalCheckSettings | None = None,
) -> HostStateResolution:
    """Thin convenience wrapper: all tests go through the real API."""
    return resolve_host_state(
        previous=previous,
        freshness=freshness,
        resources=resources,
        reachability=reachability,
        settings=settings if settings is not None else _settings(),
    )


def _down_resolution() -> HostStateResolution:
    """A confirmed DOWN resolution with no pending recovery streak."""
    return HostStateResolution(state=HostState.DOWN)


def _run_sequence(
    start: HostStateResolution | None,
    observations: tuple[tuple[HeartbeatFreshness, TcpReachability], ...],
    *,
    settings: ExternalCheckSettings | None = None,
    resources: ResourceAssessment = _CLEAR,
) -> list[HostStateResolution]:
    """Thread the explicit immutable memory through pure evaluations."""
    current = start
    results: list[HostStateResolution] = []
    for freshness, reachability in observations:
        current = _resolve(
            current,
            freshness,
            reachability,
            resources=resources,
            settings=settings,
        )
        results.append(current)
    return results


class ResolutionContractTest(unittest.TestCase):
    """Public API shape and HostStateResolution invariants."""

    def test_is_frozen_dataclass(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(HostStateResolution))
        self.assertTrue(HostStateResolution.__dataclass_params__.frozen)

    def test_uses_slots(self) -> None:
        self.assertNotIn(
            "__dict__",
            dir(HostStateResolution(state=HostState.HEALTHY)),
        )
        resolution = HostStateResolution(state=HostState.HEALTHY)
        with self.assertRaises(AttributeError):
            getattr(resolution, "__dict__")  # noqa: B009

    def test_counters_are_immutable(self) -> None:
        resolution = HostStateResolution(state=HostState.HEALTHY)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            resolution.down_failures = 1  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            resolution.recovery_successes = 1  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            resolution.state = HostState.DOWN  # type: ignore[misc]

    def test_counter_defaults_are_zero(self) -> None:
        resolution = HostStateResolution(state=HostState.HEALTHY)
        self.assertEqual(resolution.down_failures, 0)
        self.assertEqual(resolution.recovery_successes, 0)
        self.assertIsInstance(resolution.down_failures, int)
        self.assertIsInstance(resolution.recovery_successes, int)

    def test_resolver_arguments_are_keyword_only(self) -> None:
        params = inspect.signature(resolve_host_state).parameters
        self.assertEqual(
            set(params),
            {"previous", "freshness", "resources", "reachability", "settings"},
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in params.values()
            )
        )

    def test_positional_call_rejected(self) -> None:
        with self.assertRaises(TypeError):
            resolve_host_state(
                None,  # type: ignore[misc]
                HeartbeatFreshness.FRESH,
                _CLEAR,
                TcpReachability.REACHABLE,
                _settings(),
            )

    def test_previous_none_accepted(self) -> None:
        resolution = _resolve(None, HeartbeatFreshness.FRESH,
                              TcpReachability.REACHABLE)
        self.assertIs(resolution.state, HostState.HEALTHY)

    def test_negative_counters_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HostStateResolution(  # type: ignore[arg-type]
                state=HostState.DEGRADED, down_failures=-1
            )
        with self.assertRaises(ValueError):
            HostStateResolution(  # type: ignore[arg-type]
                state=HostState.DOWN, recovery_successes=-1
            )

    def test_bool_counters_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HostStateResolution(  # type: ignore[arg-type]
                state=HostState.DEGRADED, down_failures=True
            )
        with self.assertRaises(ValueError):
            HostStateResolution(  # type: ignore[arg-type]
                state=HostState.DOWN, recovery_successes=False
            )

    def test_non_integer_counters_rejected(self) -> None:
        for bad in (1.0, "1", 2.5, None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    HostStateResolution(  # type: ignore[arg-type]
                        state=HostState.DEGRADED, down_failures=bad
                    )
                with self.assertRaises(ValueError):
                    HostStateResolution(  # type: ignore[arg-type]
                        state=HostState.DOWN, recovery_successes=bad
                    )

    def test_down_with_nonzero_down_failures_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HostStateResolution(
                state=HostState.DOWN, down_failures=1
            )

    def test_non_down_with_nonzero_recovery_successes_rejected(self) -> None:
        for state in (HostState.HEALTHY, HostState.DEGRADED):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    HostStateResolution(
                        state=state, recovery_successes=1
                    )

    def test_valid_memory_shapes_accepted(self) -> None:
        # DOWN carries only a recovery streak.
        self.assertEqual(
            HostStateResolution(state=HostState.DOWN, recovery_successes=5),
            HostStateResolution(
                state=HostState.DOWN, down_failures=0, recovery_successes=5
            ),
        )
        # Non-DOWN carries only a pending down streak.
        self.assertEqual(
            HostStateResolution(
                state=HostState.DEGRADED, down_failures=4
            ),
            HostStateResolution(
                state=HostState.DEGRADED, down_failures=4, recovery_successes=0
            ),
        )


class BaseStateMatrixTest(unittest.TestCase):
    """Base instantaneous state outside confirmed DOWN hysteresis."""

    def test_fresh_reachable_clear_is_healthy(self) -> None:
        resolution = _resolve(
            None, HeartbeatFreshness.FRESH, TcpReachability.REACHABLE
        )
        self.assertIs(resolution.state, HostState.HEALTHY)
        self.assertEqual(resolution.down_failures, 0)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_fresh_reachable_breached_is_degraded(self) -> None:
        resolution = _resolve(
            None,
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            resources=_BREACHED,
        )
        self.assertIs(resolution.state, HostState.DEGRADED)

    def test_missing_reachable_is_degraded(self) -> None:
        resolution = _resolve(
            None, HeartbeatFreshness.MISSING, TcpReachability.REACHABLE
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.down_failures, 0)

    def test_stale_reachable_is_degraded(self) -> None:
        resolution = _resolve(
            None, HeartbeatFreshness.STALE, TcpReachability.REACHABLE
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.down_failures, 0)

    def test_fresh_unreachable_is_degraded(self) -> None:
        resolution = _resolve(
            None, HeartbeatFreshness.FRESH, TcpReachability.UNREACHABLE
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.down_failures, 0)

    def test_resource_breach_alone_never_produces_down(self) -> None:
        # Even a breached host with a fresh heartbeat that is TCP
        # unreachable never qualifies for DOWN (FRESH breaks the
        # predicate), regardless of the confirmation threshold.
        settings = _settings(down_confirmations=1)
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.FRESH, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.UNREACHABLE),
            ),
            settings=settings,
            resources=_BREACHED,
        )
        for resolution in results:
            self.assertIs(resolution.state, HostState.DEGRADED)
            self.assertEqual(resolution.down_failures, 0)

    def test_matrix_holds_with_pending_down_streak(self) -> None:
        # The base rule applies to non-qualifying evidence even while
        # a down streak is pending on the previous resolution.
        pending = HostStateResolution(
            state=HostState.DEGRADED, down_failures=2
        )
        resolution = _resolve(
            pending, HeartbeatFreshness.MISSING, TcpReachability.REACHABLE
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.down_failures, 0)


class DownDebounceTest(unittest.TestCase):
    """DOWN qualification and the down_confirmations debounce."""

    def test_first_qualifying_observation_increments_streak(self) -> None:
        resolution = _resolve(
            None, HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.down_failures, 1)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_below_threshold_remains_degraded(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.down_failures) for r in results],
            [
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 2),
            ],
        )

    def test_exact_threshold_enters_down(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertIs(results[2].state, HostState.DOWN)
        self.assertEqual(results[2].down_failures, 0)
        self.assertEqual(results[2].recovery_successes, 0)

    def test_threshold_one_enters_immediately(self) -> None:
        resolution = _resolve(
            None,
            HeartbeatFreshness.MISSING,
            TcpReachability.UNREACHABLE,
            settings=_settings(down_confirmations=1),
        )
        self.assertIs(resolution.state, HostState.DOWN)
        self.assertEqual(resolution.down_failures, 0)

    def test_missing_qualifies(self) -> None:
        self.assertEqual(
            _resolve(
                None,
                HeartbeatFreshness.MISSING,
                TcpReachability.UNREACHABLE,
                settings=_settings(down_confirmations=1),
            ).state,
            HostState.DOWN,
        )

    def test_stale_qualifies(self) -> None:
        self.assertEqual(
            _resolve(
                None,
                HeartbeatFreshness.STALE,
                TcpReachability.UNREACHABLE,
                settings=_settings(down_confirmations=1),
            ).state,
            HostState.DOWN,
        )

    def test_missing_stale_alternation_preserves_streak(self) -> None:
        # MISSING and STALE are the same lost-heartbeat qualifying
        # family: alternating them never breaks the streak.
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.down_failures) for r in results],
            [
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 2),
                (HostState.DOWN, 0),
            ],
        )

    def test_fresh_breaks_streak(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.down_failures) for r in results],
            [
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 2),
                (HostState.DEGRADED, 0),
                (HostState.DEGRADED, 1),
            ],
        )

    def test_reachable_breaks_streak(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.REACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertIs(results[0].state, HostState.DEGRADED)
        self.assertEqual(results[0].down_failures, 1)
        self.assertIs(results[1].state, HostState.DEGRADED)
        self.assertEqual(results[1].down_failures, 0)

    def test_resources_do_not_affect_qualification(self) -> None:
        settings = _settings(down_confirmations=2)
        clear = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=settings,
            resources=_CLEAR,
        )
        breached = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=settings,
            resources=_BREACHED,
        )
        self.assertEqual(clear, breached)

    def test_no_off_by_one(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=4),
        )
        self.assertEqual(
            [(r.state, r.down_failures) for r in results],
            [
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 2),
                (HostState.DEGRADED, 3),
                (HostState.DOWN, 0),
            ],
        )


class DownHoldTest(unittest.TestCase):
    """Confirmed DOWN is held until recovery hysteresis confirms."""

    def test_unreachable_remains_down(self) -> None:
        resolution = _resolve(
            _down_resolution(),
            HeartbeatFreshness.MISSING,
            TcpReachability.UNREACHABLE,
        )
        self.assertIs(resolution.state, HostState.DOWN)
        self.assertEqual(resolution.down_failures, 0)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_unreachable_resets_recovery_streak(self) -> None:
        down_with_streak = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            down_with_streak,
            HeartbeatFreshness.MISSING,
            TcpReachability.UNREACHABLE,
        )
        self.assertIs(resolution.state, HostState.DOWN)
        self.assertEqual(resolution.recovery_successes, 0)
        self.assertEqual(resolution.down_failures, 0)

    def test_fresh_heartbeat_alone_does_not_release_down(self) -> None:
        resolution = _resolve(
            _down_resolution(),
            HeartbeatFreshness.FRESH,
            TcpReachability.UNREACHABLE,
        )
        self.assertIs(resolution.state, HostState.DOWN)

    def test_resource_changes_alone_do_not_release_down(self) -> None:
        for resources in (_CLEAR, _BREACHED):
            with self.subTest(breached=resources.is_breached):
                resolution = _resolve(
                    _down_resolution(),
                    HeartbeatFreshness.STALE,
                    TcpReachability.UNREACHABLE,
                    resources=resources,
                )
                self.assertIs(resolution.state, HostState.DOWN)

    def test_down_failures_always_zero_while_down(self) -> None:
        # Every DOWN-hold path must keep down_failures at 0.
        previous = _down_resolution()
        for freshness in (
            HeartbeatFreshness.MISSING,
            HeartbeatFreshness.STALE,
            HeartbeatFreshness.FRESH,
        ):
            for resources in (_CLEAR, _BREACHED):
                resolution = _resolve(
                    previous,
                    freshness,
                    TcpReachability.UNREACHABLE,
                    resources=resources,
                )
                self.assertIs(resolution.state, HostState.DOWN)
                self.assertEqual(resolution.down_failures, 0)
                self.assertEqual(resolution.recovery_successes, 0)

    def test_stale_unreachable_remains_down(self) -> None:
        resolution = _resolve(
            _down_resolution(),
            HeartbeatFreshness.STALE,
            TcpReachability.UNREACHABLE,
        )
        self.assertIs(resolution.state, HostState.DOWN)


class RecoveryHysteresisTest(unittest.TestCase):
    """Recovery from confirmed DOWN via the REACHABLE TCP streak."""

    def test_first_reachable_increments_recovery_streak(self) -> None:
        resolution = _resolve(
            _down_resolution(),
            HeartbeatFreshness.MISSING,
            TcpReachability.REACHABLE,
        )
        self.assertIs(resolution.state, HostState.DOWN)
        self.assertEqual(resolution.recovery_successes, 1)
        self.assertEqual(resolution.down_failures, 0)

    def test_below_threshold_remains_down(self) -> None:
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.recovery_successes) for r in results],
            [
                (HostState.DOWN, 1),
                (HostState.DOWN, 2),
            ],
        )

    def test_exact_threshold_exits_down(self) -> None:
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=2),
        )
        self.assertIs(results[1].state, HostState.HEALTHY)
        self.assertEqual(results[1].down_failures, 0)
        self.assertEqual(results[1].recovery_successes, 0)

    def test_threshold_one_exits_immediately(self) -> None:
        resolution = _resolve(
            _down_resolution(),
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            settings=_settings(recovery_confirmations=1),
        )
        self.assertIs(resolution.state, HostState.HEALTHY)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_unreachable_breaks_recovery_streak(self) -> None:
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=2),
        )
        self.assertEqual(
            [(r.state, r.recovery_successes) for r in results],
            [
                (HostState.DOWN, 1),
                (HostState.DOWN, 0),
                (HostState.DOWN, 1),
                (HostState.HEALTHY, 0),
            ],
        )

    def test_fresh_clear_confirming_success_is_healthy(self) -> None:
        pending = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            resources=_CLEAR,
        )
        self.assertIs(resolution.state, HostState.HEALTHY)

    def test_fresh_breached_confirming_success_is_degraded(self) -> None:
        pending = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            resources=_BREACHED,
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_stale_confirming_success_is_degraded(self) -> None:
        pending = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.STALE,
            TcpReachability.REACHABLE,
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_missing_confirming_success_is_degraded(self) -> None:
        pending = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.MISSING,
            TcpReachability.REACHABLE,
        )
        self.assertIs(resolution.state, HostState.DEGRADED)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_freshness_changes_do_not_break_tcp_streak(self) -> None:
        # Heartbeat evidence changes while TCP stays REACHABLE never
        # reset the pending recovery streak.
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.STALE, TcpReachability.REACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.recovery_successes) for r in results],
            [
                (HostState.DOWN, 1),
                (HostState.DOWN, 2),
                (HostState.HEALTHY, 0),
            ],
        )

    def test_resource_changes_do_not_break_tcp_streak(self) -> None:
        settings = _settings(recovery_confirmations=2)
        first = _resolve(
            _down_resolution(),
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            resources=_BREACHED,
            settings=settings,
        )
        self.assertIs(first.state, HostState.DOWN)
        self.assertEqual(first.recovery_successes, 1)
        second = _resolve(
            first,
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            resources=_CLEAR,
            settings=settings,
        )
        self.assertIs(second.state, HostState.HEALTHY)
        self.assertEqual(second.recovery_successes, 0)

    def test_no_off_by_one(self) -> None:
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.recovery_successes) for r in results],
            [
                (HostState.DOWN, 1),
                (HostState.DOWN, 2),
                (HostState.DEGRADED, 0),
            ],
        )


class CounterCanonicalizationTest(unittest.TestCase):
    """Which counter is carried/reset on which path."""

    def test_pending_down_always_zero_recovery_successes(self) -> None:
        pending = _resolve(
            None, HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE
        )
        self.assertEqual(pending.recovery_successes, 0)
        still_pending = _resolve(
            pending, HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE
        )
        self.assertEqual(still_pending.recovery_successes, 0)

    def test_confirmed_down_resets_down_failures(self) -> None:
        pending = HostStateResolution(
            state=HostState.DEGRADED, down_failures=2
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.MISSING,
            TcpReachability.UNREACHABLE,
            settings=_settings(down_confirmations=3),
        )
        self.assertIs(resolution.state, HostState.DOWN)
        self.assertEqual(resolution.down_failures, 0)
        self.assertEqual(resolution.recovery_successes, 0)

    def test_recovery_pending_keeps_down_failures_zero(self) -> None:
        pending = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.MISSING,
            TcpReachability.REACHABLE,
            settings=_settings(recovery_confirmations=3),
        )
        self.assertIs(resolution.state, HostState.DOWN)
        self.assertEqual(resolution.down_failures, 0)
        self.assertEqual(resolution.recovery_successes, 2)

    def test_confirmed_recovery_resets_both_counters(self) -> None:
        pending = HostStateResolution(
            state=HostState.DOWN, recovery_successes=1
        )
        resolution = _resolve(
            pending,
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            settings=_settings(recovery_confirmations=2),
        )
        self.assertEqual(
            (resolution.down_failures, resolution.recovery_successes),
            (0, 0),
        )

    def test_non_qualifying_evaluation_resets_pending_counters(self) -> None:
        pending = HostStateResolution(
            state=HostState.DEGRADED, down_failures=2
        )
        for freshness, reachability in (
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
            (HeartbeatFreshness.STALE, TcpReachability.REACHABLE),
            (HeartbeatFreshness.FRESH, TcpReachability.UNREACHABLE),
        ):
            with self.subTest(
                freshness=freshness.value, reachability=reachability.value
            ):
                resolution = _resolve(pending, freshness, reachability)
                self.assertEqual(resolution.down_failures, 0)
                self.assertEqual(resolution.recovery_successes, 0)


class PurityStatelessnessTest(unittest.TestCase):
    """Same inputs, same outputs; no mutation, no hidden memory."""

    def test_same_inputs_produce_same_output(self) -> None:
        kwargs = dict(
            previous=HostStateResolution(
                state=HostState.DEGRADED, down_failures=1
            ),
            freshness=HeartbeatFreshness.STALE,
            resources=_CLEAR,
            reachability=TcpReachability.UNREACHABLE,
            settings=_settings(down_confirmations=3),
        )
        self.assertEqual(
            resolve_host_state(**kwargs),  # type: ignore[arg-type]
            resolve_host_state(**kwargs),  # type: ignore[arg-type]
        )

    def test_previous_resolution_not_mutated(self) -> None:
        previous = HostStateResolution(
            state=HostState.DEGRADED, down_failures=1
        )
        snapshot = HostStateResolution(
            state=HostState.DEGRADED, down_failures=1
        )
        _resolve(previous, HeartbeatFreshness.MISSING,
                 TcpReachability.UNREACHABLE)
        self.assertEqual(previous, snapshot)

    def test_resources_not_mutated(self) -> None:
        _resolve(
            None,
            HeartbeatFreshness.FRESH,
            TcpReachability.REACHABLE,
            resources=_BREACHED,
        )
        self.assertEqual(_BREACHED, ResourceAssessment(
            breaches=(ResourceMetric.CPU,)
        ))

    def test_settings_not_mutated(self) -> None:
        settings = _settings(down_confirmations=2, recovery_confirmations=1)
        snapshot = _settings(down_confirmations=2, recovery_confirmations=1)
        _resolve(
            None,
            HeartbeatFreshness.MISSING,
            TcpReachability.UNREACHABLE,
            settings=settings,
        )
        self.assertEqual(settings, snapshot)

    def test_no_hidden_cross_call_memory(self) -> None:
        # Running the identical sequence twice from the same start
        # yields identical results: no call leaves residue behind.
        observations = (
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
        )
        first = _run_sequence(None, observations)
        second = _run_sequence(None, observations)
        self.assertEqual(first, second)
        self.assertTrue(
            all(r.state is HostState.DOWN for r in first[2:])
        )

    def test_independent_host_chains_do_not_interfere(self) -> None:
        # Two hosts with separately retained previous resolutions,
        # evaluated interleaved, behave exactly like two solo runs —
        # per-host memory lives outside D3 and never leaks.
        settings = _settings(down_confirmations=2, recovery_confirmations=2)
        host_a_observations = (
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
        )
        host_b_observations = (
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            (HeartbeatFreshness.STALE, TcpReachability.REACHABLE),
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
        )
        solo_a = _run_sequence(
            None, host_a_observations, settings=settings
        )
        solo_b = _run_sequence(
            None, host_b_observations, settings=settings
        )

        current_a: HostStateResolution | None = None
        current_b: HostStateResolution | None = None
        interleaved_a: list[HostStateResolution] = []
        interleaved_b: list[HostStateResolution] = []
        for obs_a, obs_b in zip(
            host_a_observations, host_b_observations, strict=True
        ):
            current_a = _resolve(
                current_a, *obs_a, settings=settings
            )
            interleaved_a.append(current_a)
            current_b = _resolve(
                current_b, *obs_b, settings=settings
            )
            interleaved_b.append(current_b)
        self.assertEqual(interleaved_a, solo_a)
        self.assertEqual(interleaved_b, solo_b)


class SequenceTest(unittest.TestCase):
    """End-to-end pure sequences with threaded explicit memory."""

    def test_sequence_a_down_confirmation(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.down_failures) for r in results],
            [
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 2),
                (HostState.DOWN, 0),
            ],
        )

    def test_sequence_b_interrupted_down_confirmation(self) -> None:
        results = _run_sequence(
            None,
            (
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.REACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
            ),
            settings=_settings(down_confirmations=3),
        )
        self.assertEqual(
            [(r.state, r.down_failures) for r in results],
            [
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 0),
                (HostState.DEGRADED, 1),
                (HostState.DEGRADED, 2),
                (HostState.DOWN, 0),
            ],
        )

    def test_sequence_c_recovery(self) -> None:
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=2),
        )
        self.assertEqual(
            [
                (r.state, r.down_failures, r.recovery_successes)
                for r in results
            ],
            [
                (HostState.DOWN, 0, 1),
                (HostState.HEALTHY, 0, 0),
            ],
        )

    def test_sequence_d_interrupted_recovery(self) -> None:
        results = _run_sequence(
            _down_resolution(),
            (
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
                (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
                (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            ),
            settings=_settings(recovery_confirmations=2),
        )
        self.assertEqual(
            [
                (r.state, r.down_failures, r.recovery_successes)
                for r in results
            ],
            [
                (HostState.DOWN, 0, 1),
                (HostState.DOWN, 0, 0),
                (HostState.DOWN, 0, 1),
                (HostState.HEALTHY, 0, 0),
            ],
        )

    def test_full_cycle_down_and_back(self) -> None:
        # HEALTHY -> DEGRADED streak -> DOWN -> recovery -> HEALTHY.
        observations = (
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.REACHABLE),
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
        )
        results = _run_sequence(
            None,
            observations,
            settings=_settings(down_confirmations=3, recovery_confirmations=2),
        )
        self.assertEqual(
            [r.state for r in results],
            [
                HostState.HEALTHY,
                HostState.DEGRADED,
                HostState.DEGRADED,
                HostState.DOWN,
                HostState.DOWN,
                HostState.HEALTHY,
            ],
        )


class ArchitectureBoundaryTest(unittest.TestCase):
    """D3 stays a pure resolver: no transitions, no networking, no
    D1 re-evaluation, no persistence, no wall clock, no host
    identity, no per-host mutable registry."""

    def setUp(self) -> None:
        self.source = inspect.getsource(state_resolver)

    def test_no_host_transition_construction_or_import(self) -> None:
        self.assertNotIn("HostTransition(", self.source)
        self.assertFalse(hasattr(state_resolver, "HostTransition"))

    def test_domain_import_is_host_state_only(self) -> None:
        imported: set[str] = set()
        for line in self.source.splitlines():
            stripped = line.strip()
            if stripped.startswith("from hermes_sentinel.domain import"):
                imported.update(
                    name.strip()
                    for name in stripped.split("import", 1)[1].split(",")
                )
        self.assertEqual(imported, {"HostState"})

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
                "hermes_sentinel.reachability",
            },
        )

    def test_no_network_dependency(self) -> None:
        for token in (
            "socket",
            "create_connection",
            "probe_tcp_reachability",
            "urllib",
            "requests",
            "httpx",
            "asyncio",
            "getaddrinfo",
            "gethostbyname",
            "dns",
        ):
            self.assertNotIn(token, self.source)

    def test_no_d1_re_evaluation(self) -> None:
        for token in (
            "evaluate_heartbeat_freshness",
            "evaluate_resource_thresholds",
            "HeartbeatFreshnessResult",
        ):
            self.assertNotIn(token, self.source)

    def test_no_persistence_dependency(self) -> None:
        for token in (
            "sqlite",
            "HeartbeatRepository",
            "hermes_sentinel.persistence",
        ):
            self.assertNotIn(token, self.source)

    def test_no_wall_clock_or_sleeping(self) -> None:
        for token in (
            "datetime",
            "utcnow",
            "monotonic",
            "perf_counter",
            "sleep(",
            "time.time",
            "now(",
        ):
            self.assertNotIn(token, self.source)

    def test_no_environment_or_logging(self) -> None:
        for token in (
            "os.environ",
            "getenv",
            "import logging",
            "logger",
            "print(",
        ):
            self.assertNotIn(token, self.source)

    def test_no_host_config_or_services(self) -> None:
        for token in ("HostConfig", "services"):
            self.assertNotIn(token, self.source)

    def test_no_notification_coupling(self) -> None:
        for token in ("telegram", "incident"):
            self.assertNotIn(token, self.source)

    def test_no_d2_settings_fields_consumed(self) -> None:
        for token in ("tcp_host", "tcp_port", "timeout_seconds"):
            self.assertNotIn(token, self.source)

    def test_no_mutable_module_state_or_registry(self) -> None:
        allowed_public = {
            "ExternalCheckSettings",
            "HostState",
            "HostStateResolution",
            "HeartbeatFreshness",
            "ResourceAssessment",
            "TcpReachability",
            "annotations",  # from __future__ import annotations
            "dataclass",
            "resolve_host_state",
            "__all__",
        }
        for name, value in vars(state_resolver).items():
            # Private helpers (leading underscore) and dunder module
            # bookkeeping are irrelevant here: only the public module
            # surface may exist, and none of it may be a mutable
            # container — no per-host registry, no cache, no
            # module-global resolver state.
            if name.startswith("_"):
                continue
            self.assertIn(name, allowed_public)
            if name == "__all__":
                continue
            self.assertNotIsInstance(value, (dict, list, set))

    def test_tcp_target_settings_do_not_affect_resolution(self) -> None:
        # Behavioural complement of the source-level boundary: only
        # the confirmation fields participate in resolution.
        base = _settings(down_confirmations=2, recovery_confirmations=2)
        variant = ExternalCheckSettings(
            tcp_host="other-host.example.org",
            tcp_port=8443,
            timeout_seconds=0.5,
            down_confirmations=2,
            recovery_confirmations=2,
        )
        for freshness, reachability in (
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
            (HeartbeatFreshness.MISSING, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.STALE, TcpReachability.UNREACHABLE),
            (HeartbeatFreshness.FRESH, TcpReachability.REACHABLE),
        ):
            with self.subTest(
                freshness=freshness.value, reachability=reachability.value
            ):
                previous = HostStateResolution(
                    state=HostState.DEGRADED, down_failures=1
                )
                self.assertEqual(
                    _resolve(
                        previous,
                        freshness,
                        reachability,
                        settings=base,
                    ),
                    _resolve(
                        previous,
                        freshness,
                        reachability,
                        settings=variant,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
