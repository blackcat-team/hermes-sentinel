"""Deterministic tests for the Stage E5 cooperative runtime loop.

Covers the authoritative E5 contract: stop requested before start
(zero monitoring calls, zero HTTP handling), the first monitoring
cycle firing immediately and before the first heartbeat accept wait,
completion-anchored rescheduling (next due = post-completion monotonic
reading + interval), no re-run before the due time, exactly one cycle
when due is reached after any delay (no catch-up bursts), continuous
fast heartbeat traffic unable to starve monitoring, at most one
``handle_request`` per cooperative loop iteration, the accept-wait
timeout set before every request being non-negative and never larger
than the poll interval nor the remaining schedule time, unchanged
propagation of monitoring/server/monotonic/stop-predicate exceptions
with no retries, fail-fast interval validation, and the module purity
boundaries (no threads, no event loop, no stdlib serve-loop strategy,
no construction of any collaborator). All time is a scripted fake
monotonic clock — no real sleeps, no wall-clock, no network.
"""

from __future__ import annotations

import inspect
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import runtime  # noqa: E402
from hermes_sentinel.runtime import SentinelRuntime  # noqa: E402


class _FakeMonotonic:
    """Monotonic clock double: the value advances only when scripted."""

    def __init__(self, start: float) -> None:
        self.value = start
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeHeartbeatServer:
    """Server double at the standard one-request serving seam."""

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.observed_timeouts: list[float | None] = []
        self.handle_request_calls = 0
        self.on_handle_request: Callable[[], None] | None = None

    def handle_request(self) -> None:
        self.handle_request_calls += 1
        # Snapshot the accept-wait bound the runtime set immediately
        # before this call — exactly what requirement 9 inspects.
        self.observed_timeouts.append(self.timeout)
        if self.on_handle_request is not None:
            self.on_handle_request()


class _FakeMonitoringCycle:
    """MonitoringCycle double: counts runs, records the clock at each."""

    def __init__(self, clock: _FakeMonotonic) -> None:
        self.clock = clock
        self.run_calls = 0
        self.run_at: list[float] = []
        self.on_run: Callable[[], None] | None = None

    def run(self) -> None:
        self.run_calls += 1
        self.run_at.append(self.clock.value)
        if self.on_run is not None:
            self.on_run()


class _StopSwitch:
    """Stop predicate double that logs every loop-boundary check."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.requested = False
        self.calls = 0
        self.events = events

    def __call__(self) -> bool:
        self.calls += 1
        if self.events is not None:
            self.events.append("stop_check")
        return self.requested


class _Scenario:
    """One fully wired deterministic E5 scenario with fresh fakes."""

    def __init__(
        self,
        *,
        start: float = 1000.0,
        interval: float = 30.0,
        poll: float = 0.5,
    ) -> None:
        self.clock = _FakeMonotonic(start)
        self.server = _FakeHeartbeatServer()
        self.cycle = _FakeMonitoringCycle(self.clock)
        self.switch = _StopSwitch()
        self.events: list[str] = []
        self.runtime = SentinelRuntime(
            self.server,
            self.cycle,
            interval,
            monotonic=self.clock,
            poll_interval_seconds=poll,
        )


class ConstructorTest(unittest.TestCase):
    """Fail-fast interval validation and the public surface shape."""

    def test_constructor_signature_shape(self) -> None:
        parameters = inspect.signature(SentinelRuntime.__init__).parameters
        self.assertEqual(
            [
                name
                for name, parameter in parameters.items()
                if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            ],
            [
                "self",
                "heartbeat_server",
                "monitoring_cycle",
                "monitor_interval_seconds",
            ],
        )
        self.assertEqual(
            [
                name
                for name, parameter in parameters.items()
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY
            ],
            ["monotonic", "poll_interval_seconds"],
        )
        self.assertIs(parameters["monotonic"].default, time.monotonic)
        self.assertEqual(parameters["poll_interval_seconds"].default, 0.5)

    def test_run_forever_signature_shape(self) -> None:
        parameters = inspect.signature(SentinelRuntime.run_forever).parameters
        self.assertEqual(list(parameters), ["self", "should_stop"])
        self.assertIsNone(parameters["should_stop"].default)

    def test_invalid_monitor_interval_fails_fast(self) -> None:
        scenario = _Scenario()
        for bad in (
            0.0,
            -1.0,
            -30.0,
            float("inf"),
            float("-inf"),
            float("nan"),
        ):
            with self.assertRaises(ValueError) as context:
                SentinelRuntime(
                    scenario.server,
                    scenario.cycle,
                    bad,
                    monotonic=scenario.clock,
                    poll_interval_seconds=0.5,
                )
            self.assertIn("monitor_interval_seconds", str(context.exception))

    def test_invalid_poll_interval_fails_fast(self) -> None:
        scenario = _Scenario()
        for bad in (
            0.0,
            -1.0,
            -30.0,
            float("inf"),
            float("-inf"),
            float("nan"),
        ):
            with self.assertRaises(ValueError) as context:
                SentinelRuntime(
                    scenario.server,
                    scenario.cycle,
                    30.0,
                    monotonic=scenario.clock,
                    poll_interval_seconds=bad,
                )
            self.assertIn("poll_interval_seconds", str(context.exception))

    def test_validation_touches_no_collaborator(self) -> None:
        scenario = _Scenario()
        with self.assertRaises(ValueError):
            SentinelRuntime(
                scenario.server,
                scenario.cycle,
                0.0,
                monotonic=scenario.clock,
                poll_interval_seconds=0.0,
            )
        self.assertEqual(scenario.cycle.run_calls, 0)
        self.assertEqual(scenario.server.handle_request_calls, 0)
        self.assertEqual(scenario.clock.calls, 0)

    def test_small_positive_intervals_are_valid(self) -> None:
        scenario = _Scenario()
        SentinelRuntime(
            scenario.server,
            scenario.cycle,
            0.001,
            monotonic=scenario.clock,
            poll_interval_seconds=0.001,
        )
        self.assertEqual(scenario.cycle.run_calls, 0)


class StopContractTest(unittest.TestCase):
    """Stop-before-start, default predicate, boundary cadence."""

    def test_stop_requested_before_start_does_no_work(self) -> None:
        # Finding 1 regression: with stop already requested, the
        # initial stop boundary must precede ANY schedule access —
        # the injected monotonic clock must not be read at all.
        server = _FakeHeartbeatServer()
        cycle = _FakeMonitoringCycle(_FakeMonotonic(0.0))
        switch = _StopSwitch()
        switch.requested = True

        def exploding_monotonic() -> float:
            raise AssertionError(
                "monotonic clock must not be read before the "
                "initial stop boundary"
            )

        subject = SentinelRuntime(
            server, cycle, 30.0, monotonic=exploding_monotonic
        )
        subject.run_forever(should_stop=switch)  # returns normally
        self.assertEqual(cycle.run_calls, 0)
        self.assertEqual(server.handle_request_calls, 0)
        self.assertEqual(switch.calls, 1)

    def test_default_predicate_never_stops_the_loop(self) -> None:
        # Without should_stop only an exception ends the loop; three
        # full iterations prove the default predicate never stops it.
        scenario = _Scenario(start=0.0, interval=30.0)
        failure = OSError("listener died")

        def on_run() -> None:
            scenario.events.append("monitor")

        def on_handle_request() -> None:
            scenario.events.append("handle")
            scenario.clock.advance(1.0)
            if scenario.server.handle_request_calls == 3:
                raise failure

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        with self.assertRaises(OSError) as context:
            scenario.runtime.run_forever()
        self.assertIs(context.exception, failure)
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.server.handle_request_calls, 3)
        self.assertEqual(
            scenario.events,
            ["monitor", "handle", "handle", "handle"],
        )

    def test_stop_checked_at_every_bounded_loop_boundary(self) -> None:
        scenario = _Scenario(start=0.0, interval=30.0)
        logging_switch = _StopSwitch(scenario.events)

        def on_run() -> None:
            scenario.events.append("monitor")

        def on_handle_request() -> None:
            scenario.events.append("handle")
            scenario.clock.advance(5.0)
            if scenario.server.handle_request_calls == 2:
                logging_switch.requested = True

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=logging_switch)
        self.assertEqual(
            scenario.events,
            [
                "stop_check",
                "monitor",
                "handle",
                "stop_check",
                "handle",
                "stop_check",
            ],
        )


class ScheduleTest(unittest.TestCase):
    """Immediate first cycle, completion anchoring, no catch-up."""

    def test_first_cycle_immediate_and_before_first_http_wait(self) -> None:
        scenario = _Scenario(start=1000.0, interval=30.0)
        logging_switch = _StopSwitch(scenario.events)

        def on_run() -> None:
            scenario.events.append("monitor")

        def on_handle_request() -> None:
            scenario.events.append("handle")
            logging_switch.requested = True

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=logging_switch)
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.cycle.run_at, [1000.0])
        self.assertEqual(scenario.server.handle_request_calls, 1)
        self.assertEqual(
            scenario.events,
            ["stop_check", "monitor", "handle", "stop_check"],
        )

    def test_next_due_anchored_to_cycle_completion_time(self) -> None:
        # The cycle consumes 10s: started at 1000, completed at 1010,
        # so the second cycle is due at 1040 — NOT at the start-based
        # 1030. Requests consume 6s each: 1016, 1022, 1028, 1034, and
        # then exactly 1040.
        scenario = _Scenario(start=1000.0, interval=30.0, poll=7.0)

        def on_run() -> None:
            scenario.clock.advance(10.0)
            if scenario.cycle.run_calls == 2:
                scenario.switch.requested = True

        def on_handle_request() -> None:
            scenario.clock.advance(6.0)

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.cycle.run_at, [1000.0, 1040.0])
        self.assertEqual(scenario.cycle.run_calls, 2)
        self.assertEqual(scenario.server.handle_request_calls, 6)

    def test_monitoring_not_called_again_before_due_time(self) -> None:
        scenario = _Scenario(start=1000.0, interval=30.0)

        def on_handle_request() -> None:
            scenario.clock.advance(9.5)
            if scenario.server.handle_request_calls == 3:
                scenario.switch.requested = True

        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.cycle.run_at, [1000.0])
        self.assertEqual(scenario.server.handle_request_calls, 3)
        # The clock stopped at 1028.5 — still before the 1030 due point.
        self.assertEqual(scenario.clock.value, 1028.5)

    def test_long_delay_runs_exactly_one_cycle_no_catch_up(self) -> None:
        # One request jumps the clock 1000s past the 1030 due point:
        # exactly ONE further cycle runs (completion-anchored), never
        # a burst of back-to-back catch-up cycles.
        scenario = _Scenario(start=1000.0, interval=30.0)

        def on_run() -> None:
            if scenario.cycle.run_calls == 2:
                scenario.switch.requested = True

        def on_handle_request() -> None:
            scenario.clock.advance(1000.0)

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.cycle.run_at, [1000.0, 2000.0])
        self.assertEqual(scenario.cycle.run_calls, 2)
        self.assertEqual(scenario.server.handle_request_calls, 2)


class SubUlpDeadlineTest(unittest.TestCase):
    """Finding 2 regression: sub-ULP intervals at large clock values.

    At 2**53 the float ULP is 2.0, so the valid positive interval
    1.0 satisfies ``now + interval == now`` under ordinary float
    addition — a collapsed deadline must never re-mark the schedule
    immediately due.
    """

    LARGE = 2.0**53
    INTERVAL = 1.0

    def test_scenario_values_genuinely_collapse_ordinary_addition(self) -> None:
        # Precondition guard: these constants really do exercise the
        # collapse (the remediation scenario is not vacuous).
        self.assertEqual(self.LARGE + self.INTERVAL, self.LARGE)

    def test_sub_ulp_interval_does_not_duplicate_monitoring(self) -> None:
        # Frozen clock, continuous traffic: after the immediate first
        # cycle the runtime must NOT re-run monitoring at the
        # identical clock value, no matter how many requests arrive.
        scenario = _Scenario(
            start=self.LARGE, interval=self.INTERVAL, poll=0.5
        )

        def on_handle_request() -> None:
            # (At this clock magnitude even a 1.0-second advance is
            # unrepresentable, so the traffic genuinely freezes time.)
            if scenario.server.handle_request_calls == 5:
                scenario.switch.requested = True

        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.cycle.run_at, [self.LARGE])
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.server.handle_request_calls, 5)
        self.assertEqual(scenario.server.observed_timeouts, [0.5] * 5)

    def test_sub_ulp_deadline_is_representably_later_than_completion(
        self,
    ) -> None:
        # poll (8.0) exceeds the one-ULP remaining gap (2.0), so the
        # observed accept-wait bound IS the remaining schedule time:
        # a collapsed deadline would expose 0.0; the guarded deadline
        # exposes the strictly positive ULP gap.
        scenario = _Scenario(
            start=self.LARGE, interval=self.INTERVAL, poll=8.0
        )

        def on_handle_request() -> None:
            scenario.switch.requested = True

        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.cycle.run_at, [self.LARGE])
        self.assertEqual(scenario.server.observed_timeouts, [2.0])
        self.assertGreater(scenario.server.observed_timeouts[0], 0.0)

    def test_clock_reaching_the_guarded_deadline_runs_exactly_one_cycle(
        self,
    ) -> None:
        scenario = _Scenario(
            start=self.LARGE, interval=self.INTERVAL, poll=8.0
        )

        def on_run() -> None:
            if scenario.cycle.run_calls == 2:
                scenario.switch.requested = True

        def on_handle_request() -> None:
            if scenario.server.handle_request_calls == 1:
                # A representable leap past the guarded deadline
                # (LARGE + 2): monitoring becomes due exactly once.
                scenario.clock.advance(6.0)

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(
            scenario.cycle.run_at, [self.LARGE, self.LARGE + 6.0]
        )
        self.assertEqual(scenario.cycle.run_calls, 2)
        self.assertEqual(scenario.server.handle_request_calls, 2)
        self.assertEqual(scenario.server.observed_timeouts, [2.0, 2.0])


class CooperationTest(unittest.TestCase):
    """Traffic cannot starve monitoring; one request per iteration."""

    def test_continuous_fast_traffic_cannot_starve_monitoring(self) -> None:
        # An endless stream of instant requests, one every 1/64 of a
        # second (a binary-exact step: 1920 requests sum to exactly
        # 30.0): the second cycle still fires exactly at 30 seconds,
        # after nearly two thousand serviced requests.
        scenario = _Scenario(start=0.0, interval=30.0)

        def on_run() -> None:
            if scenario.cycle.run_calls == 2:
                scenario.switch.requested = True

        def on_handle_request() -> None:
            scenario.clock.advance(1 / 64)

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.cycle.run_at, [0.0, 30.0])
        self.assertEqual(scenario.cycle.run_calls, 2)
        self.assertEqual(scenario.server.handle_request_calls, 1921)

    def test_handle_request_at_most_once_per_loop_iteration(self) -> None:
        # Every serviced request is immediately followed by a logged
        # loop-boundary check before any further request: two handle
        # events are never adjacent, and the boundary-pass count is
        # exactly the request count plus the single exiting check.
        scenario = _Scenario(start=0.0, interval=30.0)
        logging_switch = _StopSwitch(scenario.events)

        def on_run() -> None:
            if scenario.cycle.run_calls == 2:
                logging_switch.requested = True

        def on_handle_request() -> None:
            scenario.events.append("handle")
            scenario.clock.advance(0.01)

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=logging_switch)
        self.assertEqual(scenario.events[0], "stop_check")
        self.assertEqual(scenario.events[-1], "stop_check")
        for index, event in enumerate(scenario.events):
            if event == "handle":
                self.assertEqual(
                    scenario.events[index + 1],
                    "stop_check",
                    f"request at index {index} not followed by a "
                    "loop-boundary check",
                )
        handle_count = scenario.events.count("handle")
        self.assertEqual(handle_count, scenario.server.handle_request_calls)
        self.assertEqual(logging_switch.calls - 1, handle_count)


class AcceptWaitTimeoutTest(unittest.TestCase):
    """The accept-wait bound set before every handle_request."""

    def test_timeout_sequence_bounded_by_poll_and_remaining(self) -> None:
        # Same timeline as the completion-anchor test: the six waits
        # see remaining 30, 24, 18, 12, 6, 30 against poll 7 — the
        # fifth wait is clamped to the smaller remaining time.
        scenario = _Scenario(start=1000.0, interval=30.0, poll=7.0)

        def on_run() -> None:
            scenario.clock.advance(10.0)
            if scenario.cycle.run_calls == 2:
                scenario.switch.requested = True

        def on_handle_request() -> None:
            scenario.clock.advance(6.0)

        scenario.cycle.on_run = on_run
        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(
            scenario.server.observed_timeouts,
            [7.0, 7.0, 7.0, 7.0, 6.0, 7.0],
        )
        for value in scenario.server.observed_timeouts:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 7.0)

    def test_first_wait_timeout_limited_by_remaining_interval(self) -> None:
        # Interval 2 < poll 5: the very first accept wait (right
        # after the immediate first cycle) is clamped to the 2s of
        # remaining schedule time, not the 5s poll interval.
        scenario = _Scenario(start=0.0, interval=2.0, poll=5.0)

        def on_handle_request() -> None:
            scenario.switch.requested = True

        scenario.server.on_handle_request = on_handle_request
        scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(scenario.server.observed_timeouts, [2.0])


class FailureSemanticsTest(unittest.TestCase):
    """Exceptions propagate unchanged; nothing is retried."""

    def test_monitoring_cycle_exception_propagates_unchanged(self) -> None:
        scenario = _Scenario(start=1000.0, interval=30.0)
        failure = RuntimeError("cycle exploded")

        def on_run() -> None:
            raise failure

        scenario.cycle.on_run = on_run
        with self.assertRaises(RuntimeError) as context:
            scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertIs(context.exception, failure)
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.server.handle_request_calls, 0)

    def test_handle_request_exception_propagates_unchanged(self) -> None:
        scenario = _Scenario(start=1000.0, interval=30.0)
        failure = OSError("accept exploded")

        def on_handle_request() -> None:
            raise failure

        scenario.server.on_handle_request = on_handle_request
        with self.assertRaises(OSError) as context:
            scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertIs(context.exception, failure)
        self.assertEqual(scenario.cycle.run_calls, 1)
        self.assertEqual(scenario.server.handle_request_calls, 1)

    def test_no_retry_after_either_failure(self) -> None:
        # After the monitoring failure above the loop is gone: a second
        # run_forever call with a fresh schedule performs exactly one
        # more failing cycle — the runtime keeps no retry machinery.
        scenario = _Scenario(start=0.0, interval=30.0)
        attempts: list[int] = []

        def on_run() -> None:
            attempts.append(scenario.cycle.run_calls)
            raise RuntimeError(f"attempt {scenario.cycle.run_calls}")

        scenario.cycle.on_run = on_run
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                scenario.runtime.run_forever(should_stop=scenario.switch)
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(scenario.server.handle_request_calls, 0)

    def test_monotonic_exception_propagates_unchanged(self) -> None:
        server = _FakeHeartbeatServer()
        cycle = _FakeMonitoringCycle(_FakeMonotonic(0.0))
        failure = ValueError("clock broken")

        def broken_monotonic() -> float:
            raise failure

        subject = SentinelRuntime(
            server, cycle, 30.0, monotonic=broken_monotonic
        )
        with self.assertRaises(ValueError) as context:
            subject.run_forever(should_stop=_StopSwitch())
        self.assertIs(context.exception, failure)
        self.assertEqual(cycle.run_calls, 0)
        self.assertEqual(server.handle_request_calls, 0)

    def test_stop_predicate_exception_propagates_unchanged(self) -> None:
        scenario = _Scenario()
        failure = RuntimeError("predicate exploded")

        def broken_stop() -> bool:
            raise failure

        with self.assertRaises(RuntimeError) as context:
            scenario.runtime.run_forever(should_stop=broken_stop)
        self.assertIs(context.exception, failure)
        self.assertEqual(scenario.cycle.run_calls, 0)
        self.assertEqual(scenario.server.handle_request_calls, 0)


class ModuleBoundaryTest(unittest.TestCase):
    """E5 production code adds no background execution machinery."""

    _ALLOWED_MODULE_NAMES = {
        "__future__",
        "builtins",
        "math",
        "time",
        "typing",
        "hermes_sentinel.monitoring",
    }

    def test_module_imports_only_the_accepted_contracts(self) -> None:
        imported = {
            module.__name__
            for _, module in inspect.getmembers(runtime, inspect.ismodule)
        }
        self.assertTrue(imported <= self._ALLOWED_MODULE_NAMES, imported)
        for forbidden in (
            "threading",
            "asyncio",
            "multiprocessing",
            "concurrent",
            "socket",
            "select",
            "signal",
            "subprocess",
            "os",
            "environ",
            "http_server",
            "HTTPServer",
            "ThreadingHTTPServer",
            "serve_forever",
            "server_close",
            "create_heartbeat_http_server",
        ):
            self.assertNotIn(forbidden, runtime.__dict__)

    def test_no_concurrency_or_serve_loop_strategy_in_source(self) -> None:
        # NOTE: "time(" is deliberately NOT a token here — the class
        # name "SentinelRuntime(" contains it; the wall-clock markers
        # below are exact instead.
        source = inspect.getsource(runtime)
        for forbidden in (
            "threading",
            "ThreadingHTTPServer",
            "asyncio",
            "multiprocessing",
            "concurrent",
            "serve_forever",
            "sleep",
            "utcnow",
            "time.time",
            "datetime",
            "perf_counter",
            "while True",
            "try:",
            "for _ in range",
        ):
            self.assertNotIn(forbidden, source)

    def test_no_global_mutable_state(self) -> None:
        for name, value in vars(runtime).items():
            if name.startswith("__") or inspect.ismodule(value):
                continue
            if name in ("HeartbeatServer", "SentinelRuntime"):
                continue
            self.assertFalse(
                isinstance(value, (dict, list, set)),
                f"unexpected mutable module attribute: {name}",
            )


if __name__ == "__main__":
    unittest.main()
