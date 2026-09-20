"""Incident core: transitions to incidents (Stage E1).

A pure, deterministic domain mapper that projects the already-frozen
Stage D ``HostTransition`` contract onto the incident vocabulary:

- a **DOWN event** (any transition into DOWN) becomes an incident of
  kind ``IncidentKind.DOWN``;
- a **RECOVERED event** (any transition out of DOWN) becomes an
  incident of kind ``IncidentKind.RECOVERED``;
- every other valid ``HostTransition`` (ordinary
  ``HEALTHY <-> DEGRADED`` changes, never emitted by the Stage D4
  health engine) maps to ``None`` — deliberately non-exceptional.

Contract points (see docs/ARCHITECTURE.md section 22):

- ``Incident`` stores the original ``HostTransition`` as the single
  canonical source of ``host`` / ``at`` / ``from_state`` /
  ``to_state``; it duplicates none of them and performs none of the
  validation already owned by ``HostTransition``;
- manual construction is fail-closed: the requested ``kind`` must
  agree with the transition event family, otherwise ``ValueError``;
- ``incident_from_transition`` is the pure public mapper returning
  ``Incident | None``;
- the module is pure: no I/O, no networking, no persistence, no
  logging, no wall-clock access, no environment reads, no mutable
  module state.

E1 deliberately performs no deduplication/flap suppression, no
incident persistence, no message formatting and no Telegram
transport: those are later Stage E concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hermes_sentinel.domain import HostState, HostTransition

__all__ = [
    "Incident",
    "IncidentKind",
    "incident_from_transition",
]


class IncidentKind(Enum):
    """The two MVP incident kinds, one per transition event family."""

    DOWN = "down"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class Incident:
    """A confirmed host incident wrapping its ``HostTransition``.

    The transition is the canonical source of every fact the
    incident carries; the read-only convenience properties are direct
    projections of it and invent nothing.
    """

    kind: IncidentKind
    transition: HostTransition

    def __post_init__(self) -> None:
        if self.kind is IncidentKind.DOWN:
            if not self.transition.is_down_event:
                raise ValueError(
                    "IncidentKind.DOWN requires a transition into DOWN"
                )
        elif not self.transition.is_recovery_event:
            raise ValueError(
                "IncidentKind.RECOVERED requires a transition out of DOWN"
            )

    @property
    def host(self) -> str:
        """The monitored host (projected from the transition)."""
        return self.transition.host

    @property
    def at(self) -> datetime:
        """The confirmation moment (projected from the transition)."""
        return self.transition.at

    @property
    def from_state(self) -> HostState:
        """The state left (projected from the transition)."""
        return self.transition.from_state

    @property
    def to_state(self) -> HostState:
        """The state entered (projected from the transition)."""
        return self.transition.to_state


def incident_from_transition(transition: HostTransition) -> Incident | None:
    """Map a confirmed ``HostTransition`` to its incident, if any.

    DOWN events (any transition into DOWN) map to
    ``IncidentKind.DOWN``; RECOVERED events (any transition out of
    DOWN) map to ``IncidentKind.RECOVERED``; every other valid
    transition maps to ``None``.
    """
    if transition.is_down_event:
        return Incident(kind=IncidentKind.DOWN, transition=transition)
    if transition.is_recovery_event:
        return Incident(kind=IncidentKind.RECOVERED, transition=transition)
    return None
