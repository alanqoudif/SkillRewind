"""Reference platform-adapter interfaces (Phase C2.4 section 10).

Six small, `typing.Protocol`-based (structural, `runtime_checkable`)
interfaces a platform integration can implement in whatever language/style
it wants -- these Python Protocols exist to *validate* that the public
integration contract (`docs/integration-contract-v1.md`) is not secretly
coupled to any internal SkillRewind class. Nothing in this module imports
from `skillrewind.domain`, `skillrewind.persistence`, or any other internal
package; every method signature uses only plain `str`/`dict`/`list` types
that could be expressed identically over the public HTTP API.

Not every adapter implements every protocol -- `LEVEL_PROTOCOL_REQUIREMENTS`
maps the minimum protocol combination required for each Integration Level
(mirrors `skillrewind.conformance.levels.LEVEL_REQUIREMENTS`, which encodes
the same fact in terms of HTTP endpoints instead of Python interfaces).

This module deliberately does NOT implement any adapter logic itself,
except the one deterministic in-memory reference adapter in
`skillrewind.adapters.reference` used by this repository's own tests.
Mibyan-specific and LangGraph-specific adapters are out of scope for this
milestone.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArtifactProvider(Protocol):
    """Reports persistent learned artifacts (Level 1+)."""

    def report_artifact(
        self, *, kind: str, logical_name: str, content: bytes, metadata: dict[str, Any]
    ) -> str:
        """Returns the platform-observed artifact_id (or an
        adapter-assigned correlation id if the platform has not yet called
        SkillRewind's `POST /api/v1/artifacts`)."""
        ...

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        ...


@runtime_checkable
class DerivationProvider(Protocol):
    """Reports which inputs influenced the creation of a persistent
    artifact (Level 1+)."""

    def report_derivation(
        self, *, target_artifact_id: str, recipe: str, recorded_input_ids: list[str], task_snapshot: dict[str, Any]
    ) -> str:
        """Returns a derivation_id."""
        ...


@runtime_checkable
class ResolutionEnforcer(Protocol):
    """Enforces the resolution-gate decision before serving or executing a
    persistent learned artifact (Level 2+)."""

    def enforce_resolution(self, artifact_id: str, resolution: str, *, successor_artifact_id: str | None) -> bool:
        """Given a resolution value from `GET /api/v1/artifacts/{id}/resolve`
        (one of "active"/"revoked"/"quarantined"/"superseded"/
        "allowed-by-waiver"/"unavailable"), returns whether the platform
        will actually serve/execute the artifact. A conformant enforcer
        returns False for "revoked" and "quarantined" and never overrides
        that with local logic."""
        ...


@runtime_checkable
class ReplayProvider(Protocol):
    """Exposes present/withheld/control replay conditions for a candidate
    ancestor (Level 3)."""

    def run_replay(
        self, *, derivation_id: str, candidate_ancestor_id: str, intervention: str, task_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """`intervention` is one of "present"/"withheld"/"control". Returns
        the platform's observed output for that condition, in whatever
        structured form the configured verification suite expects."""
        ...


@runtime_checkable
class RebuildProvider(Protocol):
    """Exposes a clean rebuild mechanism (Level 3)."""

    def rebuild(self, *, target_artifact_id: str, clean_support_ids: list[str], recipe: str) -> bytes:
        """Returns the rebuilt artifact's raw bytes, produced using only
        `clean_support_ids` -- never any excluded/quarantined/revoked
        input."""
        ...


@runtime_checkable
class EventConsumer(Protocol):
    """Consumes lifecycle events (Level 3, optional at any level)."""

    def on_event(self, event: dict[str, Any]) -> None:
        """`event` is a canonical event envelope -- see
        `docs/event-contract-v1.md`."""
        ...


LEVEL_PROTOCOL_REQUIREMENTS: dict[int, tuple[str, ...]] = {
    1: ("ArtifactProvider", "DerivationProvider"),
    2: ("ArtifactProvider", "DerivationProvider", "ResolutionEnforcer"),
    3: ("ArtifactProvider", "DerivationProvider", "ResolutionEnforcer", "ReplayProvider", "RebuildProvider", "EventConsumer"),
}
