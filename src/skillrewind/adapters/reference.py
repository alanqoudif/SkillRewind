"""One deterministic, in-memory reference adapter implementing all six
Phase C2.4 protocols (Level 3: full rewind). Used only by this repository's
own tests, to prove the protocols in `skillrewind.adapters.protocols` are
implementable without any internal SkillRewind class -- not a real platform
integration."""

from __future__ import annotations

import hashlib
import itertools
from typing import Any


class InMemoryReferenceAdapter:
    """A toy "platform": stores artifacts/derivations/events in plain
    dicts, deterministically. Satisfies `ArtifactProvider`,
    `DerivationProvider`, `ResolutionEnforcer`, `ReplayProvider`,
    `RebuildProvider`, and `EventConsumer` structurally (see
    `test_adapter_protocols.py`, which asserts `isinstance` against each
    `runtime_checkable` Protocol)."""

    def __init__(self) -> None:
        self._artifacts: dict[str, dict[str, Any]] = {}
        self._derivations: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._enforced: list[tuple[str, str, bool]] = []
        self._id_counter = itertools.count(1)

    # -- ArtifactProvider ---------------------------------------------------

    def report_artifact(self, *, kind: str, logical_name: str, content: bytes, metadata: dict[str, Any]) -> str:
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"ref://{logical_name}@{digest[:16]}"
        self._artifacts[artifact_id] = {
            "artifact_id": artifact_id, "kind": kind, "logical_name": logical_name,
            "digest": digest, "metadata": metadata,
        }
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return dict(self._artifacts[artifact_id])

    # -- DerivationProvider ---------------------------------------------------

    def report_derivation(
        self, *, target_artifact_id: str, recipe: str, recorded_input_ids: list[str], task_snapshot: dict[str, Any]
    ) -> str:
        derivation_id = f"deriv-{next(self._id_counter)}"
        self._derivations[derivation_id] = {
            "derivation_id": derivation_id, "target_artifact_id": target_artifact_id, "recipe": recipe,
            "recorded_input_ids": list(recorded_input_ids), "task_snapshot": task_snapshot,
        }
        return derivation_id

    # -- ResolutionEnforcer ---------------------------------------------------

    def enforce_resolution(self, artifact_id: str, resolution: str, *, successor_artifact_id: str | None) -> bool:
        allowed = resolution in ("active", "allowed-by-waiver")
        self._enforced.append((artifact_id, resolution, allowed))
        return allowed

    # -- ReplayProvider ---------------------------------------------------

    def run_replay(
        self, *, derivation_id: str, candidate_ancestor_id: str, intervention: str, task_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        derivation = self._derivations.get(derivation_id, {})
        present = intervention in ("present", "control") and candidate_ancestor_id in derivation.get("recorded_input_ids", [])
        return {"intervention": intervention, "candidate_ancestor_id": candidate_ancestor_id, "observed_behavior_present": present}

    # -- RebuildProvider ---------------------------------------------------

    def rebuild(self, *, target_artifact_id: str, clean_support_ids: list[str], recipe: str) -> bytes:
        payload = f"{target_artifact_id}|{recipe}|{','.join(sorted(clean_support_ids))}"
        return payload.encode()

    # -- EventConsumer ---------------------------------------------------

    def on_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)

    @property
    def consumed_events(self) -> list[dict[str, Any]]:
        return list(self._events)

    @property
    def enforcement_log(self) -> list[tuple[str, str, bool]]:
        return list(self._enforced)
