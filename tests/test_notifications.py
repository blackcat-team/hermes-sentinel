"""Deterministic tests for the Stage E3 notification coordinator.

Covers the authoritative E3 contract: the minimal structural
``IncidentSender`` protocol (exactly one ``send(Incident)`` member,
no transport-specific surface), the synchronous stateless
``NotificationCoordinator.notify_transition`` composition with its
exact ordering (None short-circuit, one E1 mapping call, None-mapping
short-circuit, exactly one sender call, same-``Incident`` return on
success), unchanged exception propagation with no retry and no second
mapping/send path, deliberate absence of deduplication, the stateless
purity boundaries (no clock, network, repository, environment or
global mutable state) and the structural satisfaction of the protocol
by the real Stage E2 ``TelegramSender``. All tests use synthetic
frozen HostTransition values and hand-rolled doubles — no network, no
sleeps, no filesystem dependency, no real clock.
"""

from __future__ import annotations

import inspect
import sys
import typing
import unittest
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import incidents, notifications  # noqa: E402
from hermes_sentinel.domain import HostState, HostTransition  # noqa: E402
from hermes_sentinel.incidents import Incident, IncidentKind  # noqa: E402
from hermes_sentinel.notifications import (  # noqa: E402
    IncidentSender,
    NotificationCoordinator,
)
from hermes_sentinel.telegram import (  # noqa: E402
    ResponseLike,
    TelegramDeliveryError,
    TelegramSender,
    TelegramSettings,
)

_AT = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _transition(from_state: HostState, to_state: HostState) -> HostTransition:
    return HostTransition(
        host="vds-01.example.net",
        from_state=from_state,
        to_state=to_state,
        at=_AT,
    )


class _MappingProbe:
    """Count coordinator mapper calls and delegate to the real E1 mapper."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, transition: HostTransition) -> Incident | None:
        self.calls += 1
        return incidents.incident_from_transition(transition)


class _RecordingSender:
    """Deterministic IncidentSender double.

    Records every ``send(incident)`` call and, after recording,
    re-raises the exact pre-built failure object when one is armed —
    so exception identity can be observed externally.
    """

    def __init__(self, failure: BaseException | None = None) -> None:
        self.sent: list[Incident] = []
        self.failure = failure

    def send(self, incident: Incident) -> None:
        self.sent.append(incident)
        if self.failure is not None:
            raise self.failure


class _RefusingOpener:
    """TelegramSender transport double that never performs I/O.

    Satisfies the E2 ``OpenerLike`` seam structurally; any open call
    would be a test defect (network access is forbidden here).
    """

    def open(
        self, request: urllib.request.Request, timeout: float
    ) -> ResponseLike:
        raise AssertionError("IncidentSender tests must not open requests")


class MapperProbeTestCase(unittest.TestCase):
    """Shared harness: instrument the coordinator's mapper reference.

    The coordinator resolves ``incident_from_transition`` from its own
    module namespace at call time, so swapping that one module
    attribute lets every test observe the exact mapping call count
    while still delegating to the real production mapper.
    """

    def setUp(self) -> None:
        super().setUp()
        self._probe = _MappingProbe()
        self._original_mapper = notifications.incident_from_transition
        notifications.incident_from_transition = self._probe

    def tearDown(self) -> None:
        notifications.incident_from_transition = self._original_mapper
        super().tearDown()


class SenderProtocolContractTest(unittest.TestCase):
    """The protocol is only the minimal send(Incident) contract."""

    def test_is_a_typing_protocol(self) -> None:
        self.assertTrue(issubclass(IncidentSender, typing.Protocol))

    def test_declares_exactly_one_member_send(self) -> None:
        self.assertEqual(
            [name for name in dir(IncidentSender) if not name.startswith("_")],
            ["send"],
        )
        self.assertEqual(
            sorted(notifications.__all__),
            ["IncidentSender", "NotificationCoordinator"],
        )

    def test_send_takes_exactly_one_incident_and_returns_none(self) -> None:
        self.assertEqual(
            list(inspect.signature(IncidentSender.send).parameters),
            ["self", "incident"],
        )
        self.assertEqual(
            get_type_hints(IncidentSender.send),
            {"incident": Incident, "return": type(None)},
        )

    def test_protocol_carries_no_transport_specific_surface(self) -> None:
        source = inspect.getsource(notifications)
        for forbidden in ("TelegramSettings", "TelegramDeliveryError", "token"):
            self.assertNotIn(forbidden, source)


class CoordinatorApiTest(unittest.TestCase):
    """Constructor and single public operation shape."""

    def test_constructor_takes_exactly_one_sender(self) -> None:
        self.assertEqual(
            list(
                inspect.signature(NotificationCoordinator.__init__).parameters
            ),
            ["self", "sender"],
        )

    def test_single_public_operation_notify_transition(self) -> None:
        self.assertEqual(
            list(
                inspect.signature(
                    NotificationCoordinator.notify_transition
                ).parameters
            ),
            ["self", "transition"],
        )
        self.assertEqual(
            [
                name
                for name in dir(NotificationCoordinator)
                if not name.startswith("_")
            ],
            ["notify_transition"],
        )


class NoneTransitionSemanticsTest(MapperProbeTestCase):
    """transition=None returns None with mapper and sender untouched."""

    def test_none_transition_short_circuits_completely(self) -> None:
        sender = _RecordingSender()
        coordinator = NotificationCoordinator(sender)
        self.assertIsNone(coordinator.notify_transition(None))
        self.assertEqual(self._probe.calls, 0)
        self.assertEqual(sender.sent, [])


class IncidentWorthyDeliveryTest(MapperProbeTestCase):
    """DOWN / RECOVERED transitions map through E1 and deliver once."""

    def test_healthy_to_down_maps_and_sends_exactly_once(self) -> None:
        sender = _RecordingSender()
        coordinator = NotificationCoordinator(sender)
        result = coordinator.notify_transition(
            _transition(HostState.HEALTHY, HostState.DOWN)
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIs(result.kind, IncidentKind.DOWN)
        self.assertEqual(self._probe.calls, 1)
        self.assertEqual(len(sender.sent), 1)

    def test_degraded_to_down_maps_and_sends_exactly_once(self) -> None:
        sender = _RecordingSender()
        result = NotificationCoordinator(sender).notify_transition(
            _transition(HostState.DEGRADED, HostState.DOWN)
        )
        assert result is not None
        self.assertIs(result.kind, IncidentKind.DOWN)

    def test_down_to_healthy_recovers(self) -> None:
        sender = _RecordingSender()
        result = NotificationCoordinator(sender).notify_transition(
            _transition(HostState.DOWN, HostState.HEALTHY)
        )
        assert result is not None
        self.assertIs(result.kind, IncidentKind.RECOVERED)
        self.assertEqual(self._probe.calls, 1)
        self.assertEqual(len(sender.sent), 1)

    def test_down_to_degraded_recovers(self) -> None:
        sender = _RecordingSender()
        result = NotificationCoordinator(sender).notify_transition(
            _transition(HostState.DOWN, HostState.DEGRADED)
        )
        assert result is not None
        self.assertIs(result.kind, IncidentKind.RECOVERED)
        self.assertEqual(self._probe.calls, 1)
        self.assertEqual(len(sender.sent), 1)


class NonIncidentTransitionTest(MapperProbeTestCase):
    """E1 None mappings never reach the sender."""

    def test_healthy_to_degraded_maps_none_and_never_sends(self) -> None:
        sender = _RecordingSender()
        result = NotificationCoordinator(sender).notify_transition(
            _transition(HostState.HEALTHY, HostState.DEGRADED)
        )
        self.assertIsNone(result)
        self.assertEqual(self._probe.calls, 1)
        self.assertEqual(sender.sent, [])

    def test_degraded_to_healthy_maps_none_and_never_sends(self) -> None:
        sender = _RecordingSender()
        result = NotificationCoordinator(sender).notify_transition(
            _transition(HostState.DEGRADED, HostState.HEALTHY)
        )
        self.assertIsNone(result)
        self.assertEqual(self._probe.calls, 1)
        self.assertEqual(sender.sent, [])


class ObjectIdentityTest(MapperProbeTestCase):
    """The exact transition and Incident objects flow through unmodified."""

    def test_incident_retains_the_exact_original_transition(self) -> None:
        transition = _transition(HostState.HEALTHY, HostState.DOWN)
        result = NotificationCoordinator(
            _RecordingSender()
        ).notify_transition(transition)
        assert result is not None
        self.assertIs(result.transition, transition)

    def test_returned_incident_is_the_exact_object_sent(self) -> None:
        sender = _RecordingSender()
        result = NotificationCoordinator(sender).notify_transition(
            _transition(HostState.DOWN, HostState.HEALTHY)
        )
        self.assertEqual(len(sender.sent), 1)
        assert result is not None
        self.assertIs(result, sender.sent[0])

    def test_send_is_called_exactly_once_per_incident_worthy_transition(
        self,
    ) -> None:
        sender = _RecordingSender()
        coordinator = NotificationCoordinator(sender)
        for _ in range(5):
            coordinator.notify_transition(
                _transition(HostState.HEALTHY, HostState.DOWN)
            )
        self.assertEqual(len(sender.sent), 5)
        self.assertEqual(self._probe.calls, 5)


class DeliveryFailureTest(MapperProbeTestCase):
    """Sender failures propagate unchanged with no retry and no reroute."""

    def test_arbitrary_sender_failure_propagates_as_the_same_object(self) -> None:
        failure = RuntimeError("transport exploded")
        sender = _RecordingSender(failure=failure)
        with self.assertRaises(RuntimeError) as ctx:
            NotificationCoordinator(sender).notify_transition(
                _transition(HostState.HEALTHY, HostState.DOWN)
            )
        self.assertIs(ctx.exception, failure)

    def test_telegram_delivery_error_is_not_special_cased(self) -> None:
        failure = TelegramDeliveryError("Telegram delivery failed: timed out")
        sender = _RecordingSender(failure=failure)
        with self.assertRaises(TelegramDeliveryError) as ctx:
            NotificationCoordinator(sender).notify_transition(
                _transition(HostState.DEGRADED, HostState.DOWN)
            )
        self.assertIs(ctx.exception, failure)

    def test_failure_causes_no_retry(self) -> None:
        sender = _RecordingSender(failure=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            NotificationCoordinator(sender).notify_transition(
                _transition(HostState.HEALTHY, HostState.DOWN)
            )
        self.assertEqual(len(sender.sent), 1)

    def test_failure_causes_no_second_mapping_or_sending_path(self) -> None:
        sender = _RecordingSender(failure=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            NotificationCoordinator(sender).notify_transition(
                _transition(HostState.HEALTHY, HostState.DOWN)
            )
        self.assertEqual(self._probe.calls, 1)
        self.assertEqual(len(sender.sent), 1)

    def test_no_pending_state_survives_a_failure(self) -> None:
        failing = _RecordingSender(failure=RuntimeError("boom"))
        coordinator = NotificationCoordinator(failing)
        with self.assertRaises(RuntimeError):
            coordinator.notify_transition(
                _transition(HostState.HEALTHY, HostState.DOWN)
            )
        failing.failure = None
        result = coordinator.notify_transition(
            _transition(HostState.DOWN, HostState.HEALTHY)
        )
        assert result is not None
        self.assertIs(result.kind, IncidentKind.RECOVERED)
        self.assertEqual(len(failing.sent), 2)


class NoDedupeTest(MapperProbeTestCase):
    """Repeating an incident-worthy transition sends twice by design."""

    def test_same_transition_twice_sends_two_independent_incidents(self) -> None:
        sender = _RecordingSender()
        coordinator = NotificationCoordinator(sender)
        transition = _transition(HostState.HEALTHY, HostState.DOWN)
        first = coordinator.notify_transition(transition)
        second = coordinator.notify_transition(transition)
        assert first is not None
        assert second is not None
        self.assertEqual(len(sender.sent), 2)
        self.assertEqual(self._probe.calls, 2)
        self.assertIsNot(first, second)
        self.assertIs(first.transition, transition)
        self.assertIs(second.transition, transition)


class StatelessnessBoundariesTest(unittest.TestCase):
    """No clock, network, repository, environment or global state."""

    _ALLOWED_MODULE_NAMES = {
        "__future__",
        "builtins",
        "hermes_sentinel.domain",
        "hermes_sentinel.incidents",
        "typing",
    }

    def test_module_imports_no_io_transport_engine_or_config(self) -> None:
        imported = {
            module.__name__
            for _, module in inspect.getmembers(notifications, inspect.ismodule)
        }
        self.assertTrue(imported <= self._ALLOWED_MODULE_NAMES, imported)
        for forbidden in (
            "sqlite",
            "urllib",
            "socket",
            "environ",
            "HealthEngine",
            "evaluate_host",
            "asyncio",
        ):
            self.assertNotIn(forbidden, notifications.__dict__)

    def test_no_wall_clock_or_sleep_or_loop_in_source(self) -> None:
        source = inspect.getsource(notifications)
        for forbidden in ("utcnow", "now(", "time(", "sleep", "while True"):
            self.assertNotIn(forbidden, source)

    def test_no_global_mutable_state(self) -> None:
        for name, value in vars(notifications).items():
            if name.startswith("__") or inspect.ismodule(value):
                continue
            if name in ("IncidentSender", "NotificationCoordinator"):
                continue
            self.assertFalse(
                isinstance(value, (dict, list, set)),
                f"unexpected mutable module attribute: {name}",
            )

    def test_coordinator_holds_only_the_injected_sender(self) -> None:
        self.assertEqual(NotificationCoordinator.__slots__, ("_sender",))
        sender = _RecordingSender()
        coordinator = NotificationCoordinator(sender)
        self.assertIs(coordinator._sender, sender)
        self.assertFalse(hasattr(coordinator, "__dict__"))
        for transition in (
            _transition(HostState.HEALTHY, HostState.DOWN),
            _transition(HostState.DOWN, HostState.HEALTHY),
        ):
            coordinator.notify_transition(transition)
        self.assertIs(coordinator._sender, sender)
        self.assertFalse(hasattr(coordinator, "__dict__"))


class TelegramStructuralCompatTest(unittest.TestCase):
    """The real E2 TelegramSender satisfies the protocol as-is."""

    def test_telegram_sender_is_an_incident_sender(self) -> None:
        sender = TelegramSender(
            settings=TelegramSettings(
                bot_token="123456:ABC-DEF_gh",
                chat_id=-1001234567890,
            ),
            opener=_RefusingOpener(),
        )
        self.assertIsInstance(sender, IncidentSender)


if __name__ == "__main__":
    unittest.main()
