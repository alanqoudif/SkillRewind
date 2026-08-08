"""Manual waivers: explicit, scoped, audited, and optionally expiring.

A waiver never deletes the historical revocation evidence -- it only allows
the serving-resolution layer (and, when explicitly requested, this module)
to reactivate an artifact despite the recorded quarantine reason remaining
visible. Strict-mode configuration can forbid waivers entirely.
"""

from __future__ import annotations

import uuid

from ..config import SkillRewindConfig
from ..domain.enums import RevocationPolicy
from ..domain.errors import PolicyViolationError
from ..domain.models import Waiver
from ..workspace import Workspace, timestamp
from .service import release_quarantine


def create_waiver(
    workspace: Workspace,
    artifact_id: str,
    *,
    actor: str,
    reason: str,
    scope: str = "quarantine-release",
    expires_at: str | None = None,
    revocation_event_id: str | None = None,
    active_policy: RevocationPolicy | None = None,
    config: SkillRewindConfig | None = None,
) -> Waiver:
    config = config or workspace.config
    if active_policy == RevocationPolicy.STRICT and config.strict_forbids_waivers:
        raise PolicyViolationError("strict-mode configuration forbids waivers")

    waiver = Waiver(
        waiver_id=f"waiver-{uuid.uuid4()}",
        artifact_id=artifact_id,
        actor=actor,
        reason=reason,
        scope=scope,
        created_at=timestamp(),
        expires_at=expires_at,
        revocation_event_id=revocation_event_id,
    )
    workspace.waivers.insert(waiver)
    workspace.audit.append(
        "waiver.created",
        actor,
        {"waiver_id": waiver.waiver_id, "artifact_id": artifact_id, "reason": reason, "scope": scope},
    )
    if scope == "quarantine-release":
        release_quarantine(workspace, artifact_id, actor=actor, reason=f"waiver:{waiver.waiver_id}")
    return waiver


def revoke_waiver(workspace: Workspace, waiver_id: str, *, actor: str) -> None:
    workspace.waivers.revoke(waiver_id)
    workspace.audit.append("waiver.revoked", actor, {"waiver_id": waiver_id})
