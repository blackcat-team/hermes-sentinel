"""Notification coordinator (Stage E3).

A deliberately thin, bounded composition layer: take one confirmed
Stage D4 ``HostTransition``, project it through the accepted Stage E1
mapper ``incident_from_transition`` and — only when the mapper judges
the transition incident-worthy — hand the resulting ``Incident`` to an
injected sender exactly once. E3 owns nothing else: no network
transport, no health evaluation, no runtime loop, no configuration
loading and no persistence.

Contract points (see docs/ARCHITECTURE.md section 24):

- the minimal structural ``IncidentSender`` protocol carries exactly
  one member, ``send(incident)``; it is transport-agnostic and the
  Stage E2 ``TelegramSender`` satisfies it structurally without any
  modification;
- E1 remains the sole incident-semantics authority: the coordinator
  never re-decides which transitions are incident-worthy, it only
  obeys the mapper's ``Incident | None`` verdict;
- a ``None`` transition returns ``None`` without calling the mapper
  or the sender;
- a non-incident transition (mapper returns ``None``) returns
  ``None`` without calling the sender;
- an incident-worthy transition causes exactly one
  ``sender.send(incident)`` invocation, and only a successful send
  returns that exact same ``Incident`` object — never a copy, never a
  second mapping call;
- a sender failure propagates unchanged: the same exception object
  escapes unwrapped and unreplaced, with no retry, no second send, no
  pending state and no ``Incident`` returned;
- the coordinator is synchronous and stateless: no dedupe registry,
  no flap suppression, no delivery receipts, no mutable global state.
  Calling ``notify_transition`` twice with the same incident-worthy
  transition performs two independent sender calls by design;
- the coordinator never calls ``HealthEngine.evaluate_host`` and
  never creates transitions itself: a later runtime unit passes
  ``evaluation.transition`` in.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hermes_sentinel.domain import HostTransition
from hermes_sentinel.incidents import Incident, incident_from_transition

__all__ = [
    "IncidentSender",
    "NotificationCoordinator",
]


@runtime_checkable
class IncidentSender(Protocol):
    """The minimal structural sender contract: deliver one incident.

    Exactly one member, ``send(incident)``, with no return value:
    quiet return means delivered, any raised exception means the
    delivery failed. There are deliberately no Telegram-specific
    fields, methods or error types — the Stage E2 ``TelegramSender``
    satisfies this protocol structurally as-is, and so can any other
    future transport. ``runtime_checkable`` exists only so tests can
    assert structural satisfaction; it adds no behaviour.
    """

    def send(self, incident: Incident) -> None: ...


class NotificationCoordinator:
    """Bounded bridge: HostTransition -> Incident -> injected sender.

    One public operation, ``notify_transition``: ``None`` in yields
    ``None`` out; a transition the E1 mapper maps to ``None`` yields
    ``None`` out with the sender untouched; an incident-worthy
    transition goes to ``sender.send`` exactly once and, only when
    that call returns quietly, the very same ``Incident`` object is
    returned. A sender exception escapes unchanged. The coordinator
    holds no state beyond the injected sender.
    """

    __slots__ = ("_sender",)

    def __init__(self, sender: IncidentSender) -> None:
        self._sender = sender

    def notify_transition(
        self, transition: HostTransition | None
    ) -> Incident | None:
        """Compose one transition through E1 into one delivery attempt.

        The order is exact: ``None`` short-circuits before any
        mapping; ``incident_from_transition`` is called exactly once;
        a ``None`` mapping short-circuits before the sender;
        otherwise the incident is sent exactly once and returned only
        after a successful send. A sender failure propagates as the
        original exception — this method has no ``try``/``except`` at
        all, so there is nothing to wrap, replace or retry with.
        """
        if transition is None:
            return None
        incident = incident_from_transition(transition)
        if incident is None:
            return None
        self._sender.send(incident)
        return incident
