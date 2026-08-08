"""Clean-room rebuild planning: determine a validated clean support set."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import EvidenceClass, LifecycleStatus
from ..domain.errors import NotFoundError
from ..workspace import Workspace


@dataclass(frozen=True, slots=True)
class RebuildPlan:
    artifact_id: str
    derivation_id: str
    recipe: str
    task_snapshot: dict
    clean_support: tuple[str, ...]
    excluded_support: tuple[tuple[str, str], ...]  # (artifact_id, exclusion_reason)
    plan_digest: str

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "derivation_id": self.derivation_id,
            "recipe": self.recipe,
            "task_snapshot": self.task_snapshot,
            "clean_support": list(self.clean_support),
            "excluded_support": [{"artifact_id": a, "reason": r} for a, r in self.excluded_support],
            "plan_digest": self.plan_digest,
        }


def plan_rebuild(workspace: Workspace, artifact_id: str) -> RebuildPlan:
    derivation = workspace.derivations.find_by_target(artifact_id)
    if derivation is None:
        raise NotFoundError(f"no derivation recorded for artifact {artifact_id}; cannot plan a rebuild")

    candidate_support = list(
        dict.fromkeys([*derivation.recorded_inputs, *derivation.candidate_context_pool])
    )

    clean: list[str] = []
    excluded: list[tuple[str, str]] = []
    for support_id in candidate_support:
        try:
            support_artifact = workspace.artifacts.get(support_id)
        except NotFoundError:
            excluded.append((support_id, "support-artifact-not-found"))
            continue

        if support_artifact.status == LifecycleStatus.REVOKED:
            excluded.append((support_id, "revoked-root"))
            continue
        if workspace.revocations.is_quarantined(support_id):
            excluded.append((support_id, "quarantined-support"))
            continue

        incoming_confirmed = [
            e for e in workspace.edges.incoming(support_id) if e.evidence_class == EvidenceClass.REPLAY_CONFIRMED
        ]
        if incoming_confirmed:
            excluded.append((support_id, "replay-confirmed-contaminated-ancestor"))
            continue

        clean.append(support_id)

    from ..canonical.json import sha256_hex

    plan_digest = sha256_hex(
        {
            "artifact_id": artifact_id,
            "derivation_id": derivation.derivation_id,
            "clean_support": sorted(clean),
            "excluded_support": sorted(excluded),
        }
    )

    return RebuildPlan(
        artifact_id=artifact_id,
        derivation_id=derivation.derivation_id,
        recipe=derivation.recipe,
        task_snapshot=derivation.task_snapshot,
        clean_support=tuple(clean),
        excluded_support=tuple(excluded),
        plan_digest=plan_digest,
    )
