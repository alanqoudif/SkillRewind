"""Quarantine service: apply/list quarantine, independent of the barrier."""

from __future__ import annotations

from ..domain.enums import LifecycleStatus
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike


def quarantine_artifact(workspace: WorkspaceLike, artifact_id: str, *, revocation_event_id: str, reason: str) -> None:
    now = timestamp()
    workspace.revocations.add_quarantine(artifact_id, revocation_event_id, reason, now)
    workspace.artifacts.set_status(artifact_id, LifecycleStatus.QUARANTINED)
    workspace.audit.append(
        "artifact.quarantined",
        workspace.config.actor,
        {"artifact_id": artifact_id, "revocation_event_id": revocation_event_id, "reason": reason},
    )


def release_quarantine(workspace: WorkspaceLike, artifact_id: str, *, actor: str, reason: str) -> None:
    """Release quarantine only via an explicit, audited action (waiver or clean successor)."""

    workspace.revocations.remove_quarantine(artifact_id)
    workspace.artifacts.set_status(artifact_id, LifecycleStatus.ACTIVE)
    workspace.audit.append(
        "artifact.quarantine-released", actor, {"artifact_id": artifact_id, "reason": reason}
    )


def list_quarantine(workspace: WorkspaceLike) -> list[dict]:
    return workspace.revocations.list_quarantine()
