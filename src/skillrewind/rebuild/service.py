"""Clean-room rebuild execution: produces a new, never-mutates-the-old artifact."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..canonical.json import canonical_bytes
from ..domain.enums import EvidenceClass, LifecycleStatus, RelationType
from ..domain.models import Artifact, Derivation, InfluenceEdge
from ..workspace import Workspace, timestamp
from .clean_room import execute_clean_room_recipe
from .planner import RebuildPlan, plan_rebuild


@dataclass(frozen=True, slots=True)
class RebuildResult:
    plan: RebuildPlan
    new_artifact: Artifact
    fixture_output: dict[str, Any]


def rebuild_artifact(
    workspace: Workspace, artifact_id: str, *, revocation_event_id: Optional[str] = None, seed: Optional[int] = None
) -> RebuildResult:
    plan = plan_rebuild(workspace, artifact_id)
    original = workspace.artifacts.get(artifact_id)
    output = execute_clean_room_recipe(plan, seed=seed)

    content = canonical_bytes(
        {
            "rebuilt_from": artifact_id,
            "plan_digest": plan.plan_digest,
            "fixture_output": output,
        }
    )
    now = timestamp()
    behavior_keys = output.get("_behavior_keys", [])
    probes = {k: output[k] for k in behavior_keys if k in output}
    new_artifact = workspace.ingest_artifact(
        content,
        kind=original.kind,
        logical_name=original.logical_name,
        mime_type=original.mime_type,
        creator=workspace.config.actor,
        metadata={
            **{k: v for k, v in original.metadata.items() if k != "agent_skills_manifest"},
            "agent_skills_manifest": original.metadata.get("agent_skills_manifest"),
            "canary_activated": probes.get(behavior_keys[0]) if behavior_keys else None,
            "probes": probes,
            "rebuild": {
                "rebuilt_from": artifact_id,
                "plan": plan.to_dict(),
                "revocation_event_id": revocation_event_id,
            },
        },
    )
    # new artifact starts quarantined pending verification -- never auto-active
    workspace.revocations.add_quarantine(
        new_artifact.artifact_id, revocation_event_id or "manual-rebuild", "pending verification", now
    )
    workspace.artifacts.set_status(new_artifact.artifact_id, LifecycleStatus.QUARANTINED)

    workspace.derivations.upsert(
        Derivation(
            derivation_id=f"rebuild-{new_artifact.artifact_id.split(':')[-1][:16]}",
            recipe=plan.recipe,
            recipe_version="rebuild",
            target_artifact_id=new_artifact.artifact_id,
            recorded_inputs=list(plan.clean_support),
            candidate_context_pool=list(plan.clean_support),
            task_snapshot=plan.task_snapshot,
            started_at=now,
            ended_at=now,
            seed=seed,
        )
    )

    for edge_relation, source, target in [
        (RelationType.REBUILT_FROM, new_artifact.artifact_id, artifact_id),
        (RelationType.REPLACES, new_artifact.artifact_id, artifact_id),
        *[(RelationType.CLEAN_SUPPORT, s, new_artifact.artifact_id) for s in plan.clean_support],
    ]:
        workspace.edges.upsert(
            InfluenceEdge(
                source=source,
                target=target,
                relation=edge_relation,
                evidence_class=EvidenceClass.RECORDED,
                created_at=now,
                updated_at=now,
            )
        )

    workspace.audit.append(
        "artifact.rebuilt",
        workspace.config.actor,
        {
            "original_artifact_id": artifact_id,
            "new_artifact_id": new_artifact.artifact_id,
            "plan_digest": plan.plan_digest,
            "revocation_event_id": revocation_event_id,
        },
    )
    return RebuildResult(plan=plan, new_artifact=new_artifact, fixture_output=output)


def publish_successor(workspace: Workspace, original_artifact_id: str, new_artifact_id: str) -> None:
    """Atomically promote a verified successor: release its quarantine, mark
    the original superseded, and move any alias across."""

    original = workspace.artifacts.get(original_artifact_id)
    workspace.revocations.remove_quarantine(new_artifact_id)
    workspace.artifacts.set_status(new_artifact_id, LifecycleStatus.ACTIVE)
    if original.alias:
        workspace.artifacts.clear_alias(original.alias)
        workspace.artifacts.set_alias(new_artifact_id, original.alias)
    workspace.artifacts.set_superseded_by(original_artifact_id, new_artifact_id)
    workspace.audit.append(
        "artifact.successor-published",
        workspace.config.actor,
        {"original_artifact_id": original_artifact_id, "new_artifact_id": new_artifact_id},
    )
