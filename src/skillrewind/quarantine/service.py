"""Quarantine service: apply/list quarantine, independent of the barrier."""

from __future__ import annotations

from ..domain.enums import LifecycleStatus
from ..workspace import Workspace, timestamp


def quarantine_artifact(workspace: Workspace, artifact_id: str, *, revocation_event_id: str, reason: str) -> None:
    now = timestamp()
    workspace.revocations.add_quarantine(artifact_id, revocation_event_id, reason, now)
    workspace.artifacts.set_status(artifact_id, LifecycleStatus.QUARANTINED)
    workspace.audit.append(
        "artifact.quarantined",
        workspace.config.actor,
        {"artifact_id": artifact_id, "revocation_event_id": revocation_event_id, "reason": reason},
    )


def release_quarantine(workspace: Workspace, artifact_id: str, *, actor: str, reason: str) -> None:
    """Release quarantine only via an explicit, audited action (waiver or clean successor)."""

    workspace.revocations.remove_quarantine(artifact_id)
    workspace.artifacts.set_status(artifact_id, LifecycleStatus.ACTIVE)
    workspace.audit.append(
        "artifact.quarantine-released", actor, {"artifact_id": artifact_id, "reason": reason}
    )


def list_quarantine(workspace: Workspace) -> list[dict]:
    return workspace.revocations.list_quarantine()
