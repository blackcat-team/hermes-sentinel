"""Deterministic tests for the Stage E1 incident core.

Covers the authoritative E1 contract: the exact IncidentKind enum
members and values, the immutable Incident shape wrapping the
canonical HostTransition, the pure incident_from_transition mapping
(DOWN / RECOVERED / None), the fail-closed manual construction
invariants, the direct-projection convenience properties and the E1
purity boundaries (no clock, repository, network or global mutable
state). All tests exercise the real production mapper with synthetic
frozen HostTransition values — no network, no database, no real
clock.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import incidents  # noqa: E402
from hermes_sentinel.domain import HostState, HostTransition  # noqa: E402
from hermes_sentinel.incidents import (  # noqa: E402
    Incident,
    IncidentKind,
    incident_from_transition,
)

_AT = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _transition(from_state: HostState, to_state: HostState) -> HostTransition:
    return HostTransition(
        host="vds-01.example.net",
        from_state=from_state,
        to_state=to_state,
        at=_AT,
    )


def _down_incident() -> Incident:
    return Incident(
        kind=IncidentKind.DOWN,
        transition=_transition(HostState.HEALTHY, HostState.DOWN),
    )


class IncidentKindContractTest(unittest.TestCase):
    """The enum is exactly DOWN / RECOVERED with exact values."""

    def test_members_are_exactly_down_and_recovered(self) -> None:
        self.assertEqual(
            [member.name for member in IncidentKind],
            ["DOWN", "RECOVERED"],
        )

    def test_values_are_exactly_down_and_recovered(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in IncidentKind},
            {"DOWN": "down", "RECOVERED": "recovered"},
        )


class IncidentShapeTest(unittest.TestCase):
    """Immutable shape wrapping the canonical transition."""

    def test_is_frozen_and_slotted(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(Incident))
        incident = _down_incident()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            incident.kind = IncidentKind.RECOVERED  # type: ignore[misc]
        self.assertFalse(hasattr(incident, "__dict__"))

    def test_fields_are_exactly_kind_and_transition(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(Incident)],
            ["kind", "transition"],
        )

    def test_preserves_the_exact_same_transition_object(self) -> None:
        transition = _transition(HostState.DEGRADED, HostState.DOWN)
        incident = Incident(kind=IncidentKind.DOWN, transition=transition)
        self.assertIs(incident.transition, transition)

    def test_convenience_properties_are_direct_projections(self) -> None:
        incident = _down_incident()
        self.assertIs(incident.host, incident.transition.host)
        self.assertIs(incident.at, incident.transition.at)
        self.assertIs(incident.from_state, incident.transition.from_state)
        self.assertIs(incident.to_state, incident.transition.to_state)
        self.assertEqual(incident.host, "vds-01.example.net")
        self.assertEqual(incident.at, _AT)
        self.assertIs(incident.from_state, HostState.HEALTHY)
        self.assertIs(incident.to_state, HostState.DOWN)

    def test_no_summary_or_rendering_api_exists(self) -> None:
        for forbidden in ("summary", "render", "message", "format"):
            self.assertFalse(hasattr(Incident, forbidden))


class MappingSemanticsTest(unittest.TestCase):
    """The pure HostTransition -> Incident | None projection."""

    def test_down_event_maps_to_down(self) -> None:
        for source in (HostState.HEALTHY, HostState.DEGRADED):
            with self.subTest(source=source):
                incident = incident_from_transition(
                    _transition(source, HostState.DOWN)
                )
                assert incident is not None
                self.assertIs(incident.kind, IncidentKind.DOWN)

    def test_down_to_healthy_maps_to_recovered(self) -> None:
        incident = incident_from_transition(
            _transition(HostState.DOWN, HostState.HEALTHY)
        )
        assert incident is not None
        self.assertIs(incident.kind, IncidentKind.RECOVERED)

    def test_down_to_degraded_maps_to_recovered(self) -> None:
        incident = incident_from_transition(
            _transition(HostState.DOWN, HostState.DEGRADED)
        )
        assert incident is not None
        self.assertIs(incident.kind, IncidentKind.RECOVERED)

    def test_healthy_to_degraded_maps_to_none(self) -> None:
        self.assertIsNone(
            incident_from_transition(
                _transition(HostState.HEALTHY, HostState.DEGRADED)
            )
        )

    def test_degraded_to_healthy_maps_to_none(self) -> None:
        self.assertIsNone(
            incident_from_transition(
                _transition(HostState.DEGRADED, HostState.HEALTHY)
            )
        )

    def test_mapper_returns_transition_verbatim(self) -> None:
        transition = _transition(HostState.DOWN, HostState.HEALTHY)
        incident = incident_from_transition(transition)
        assert incident is not None
        self.assertIs(incident.transition, transition)

    def test_mapping_is_deterministic(self) -> None:
        transition = _transition(HostState.HEALTHY, HostState.DOWN)
        self.assertEqual(
            incident_from_transition(transition),
            incident_from_transition(transition),
        )


class ManualInvariantTest(unittest.TestCase):
    """Fail-closed manual kind/transition agreement."""

    def test_manual_down_requires_down_event(self) -> None:
        for from_state, to_state in (
            (HostState.DOWN, HostState.HEALTHY),
            (HostState.DOWN, HostState.DEGRADED),
            (HostState.HEALTHY, HostState.DEGRADED),
            (HostState.DEGRADED, HostState.HEALTHY),
        ):
            with self.subTest(from_state=from_state, to_state=to_state):
                with self.assertRaises(ValueError):
                    Incident(
                        kind=IncidentKind.DOWN,
                        transition=_transition(from_state, to_state),
                    )

    def test_manual_recovered_requires_recovery_event(self) -> None:
        for from_state, to_state in (
            (HostState.HEALTHY, HostState.DOWN),
            (HostState.DEGRADED, HostState.DOWN),
            (HostState.HEALTHY, HostState.DEGRADED),
            (HostState.DEGRADED, HostState.HEALTHY),
        ):
            with self.subTest(from_state=from_state, to_state=to_state):
                with self.assertRaises(ValueError):
                    Incident(
                        kind=IncidentKind.RECOVERED,
                        transition=_transition(from_state, to_state),
                    )

    def test_manual_construction_accepts_agreeing_combinations(self) -> None:
        down = Incident(
            kind=IncidentKind.DOWN,
            transition=_transition(HostState.HEALTHY, HostState.DOWN),
        )
        self.assertIs(down.kind, IncidentKind.DOWN)
        recovered = Incident(
            kind=IncidentKind.RECOVERED,
            transition=_transition(HostState.DOWN, HostState.DEGRADED),
        )
        self.assertIs(recovered.kind, IncidentKind.RECOVERED)


class StageBoundariesTest(unittest.TestCase):
    """E1 stays a pure mapper: no clock, repository, network, state."""

    _ALLOWED_MODULE_NAMES = {
        "__future__",
        "abc",
        "builtins",
        "dataclasses",
        "datetime",
        "enum",
        "hermes_sentinel.domain",
        "types",
        "typing",
    }

    def test_module_imports_no_io_persistence_or_transport(self) -> None:
        imported = {
            module.__name__
            for _, module in inspect.getmembers(incidents, inspect.ismodule)
        }
        self.assertTrue(imported <= self._ALLOWED_MODULE_NAMES, imported)
        for forbidden in ("sqlite", "urllib", "socket", "environ"):
            self.assertNotIn(forbidden, incidents.__dict__)

    def test_no_wall_clock_access(self) -> None:
        source = inspect.getsource(incidents)
        self.assertNotIn("utcnow", source)
        self.assertNotIn("now(", source)
        self.assertNotIn("time(", source)

    def test_no_global_mutable_state(self) -> None:
        for name, value in vars(incidents).items():
            if name.startswith("__") or inspect.ismodule(value):
                continue
            if name in ("Incident", "IncidentKind"):
                continue
            self.assertFalse(
                isinstance(value, (dict, list, set)),
                f"unexpected mutable module attribute: {name}",
            )

    def test_public_surface_is_bounded(self) -> None:
        self.assertEqual(
            sorted(incidents.__all__),
            ["Incident", "IncidentKind", "incident_from_transition"],
        )


if __name__ == "__main__":
    unittest.main()
