"""Manual waivers: explicit, scoped, audited, and optionally expiring.

A waiver is a **policy overlay**, never a one-time mutation of evidence or
lifecycle state (Phase C2.4 gap C). Creating a waiver only ever inserts a
`Waiver` row and an audit event -- it never flips `artifact.status`,
deletes/deactivates the underlying `QuarantineEntry`, or otherwise mutates
recorded/inferred/replay evidence. Serving-resolution (`Workspace
.resolve_alias` / `ServiceWorkspace.resolve_alias` / the
`/api/v1/artifacts/{id}/resolve` HTTP endpoint) and rebuild planning
(`skillrewind.rebuild.planner`) each *dynamically* evaluate whether an
active, unexpired, unrevoked, correctly-scoped waiver exists at the moment
of the decision. This makes expiry and explicit revocation immediately and
automatically effective everywhere, with no separate "undo" step required
and no risk of an artifact being left permanently active/unquarantined
after its waiver lapses.

Canonical scopes (`CANONICAL_SCOPES`):

- ``"serving"``: permits serving resolution (`resolve_alias` /
  `/resolve`) to return the artifact despite an active quarantine entry.
  ``"quarantine-release"`` is accepted as a backward-compatible alias for
  ``"serving"`` (the name used by Phase C2.3's waiver API) -- both are
  treated identically by every resolution/rebuild check in this module and
  in `rebuild.planner`.
- ``"quarantine"``: an explicit synonym for the same policy area as
  ``"serving"``/``"quarantine-release"`` (permits resolution past an active
  quarantine entry), spelled to match the resource it overlays for API
  consumers that find "serving" ambiguous.
- ``"rebuild-support"``: permits a quarantined artifact to remain in a
  rebuild's clean support set (`rebuild.planner.plan_rebuild`) instead of
  being excluded as `"quarantined-support"`. Never overrides a `"revoked"`
  root or a `"replay-confirmed-contaminated-ancestor"` exclusion -- those
  are never waiver-eligible (see `plan_rebuild`).

A waiver never changes evidence classification: it cannot turn an
`inferred` edge into `replay-confirmed`, a `replay-confirmed` edge into
anything else, or a `revoked` artifact back into `active`/clean. Strict-mode
configuration can forbid waivers entirely.
"""

from __future__ import annotations

import uuid

from ..config import SkillRewindConfig
from ..domain.enums import RevocationPolicy
from ..domain.errors import PolicyViolationError
from ..domain.models import Waiver
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike

#: Waiver scopes that permit serving-resolution to return an artifact past
#: an active quarantine entry. "quarantine-release" is the legacy Phase
#: C2.3 name; "serving" is the canonical Phase C2.4 name; "quarantine" is
#: an accepted synonym. All three are equivalent.
SERVING_SCOPES = frozenset({"serving", "quarantine-release", "quarantine"})

#: Waiver scope that permits a quarantined artifact to remain in a
#: rebuild's clean support set instead of being excluded.
REBUILD_SUPPORT_SCOPE = "rebuild-support"

CANONICAL_SCOPES = frozenset({"serving", "rebuild-support"})


def create_waiver(
    workspace: WorkspaceLike,
    artifact_id: str,
    *,
    actor: str,
    reason: str,
    scope: str = "serving",
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
    # Deliberately no state mutation here (see module docstring): the
    # quarantine entry and artifact.status are left exactly as they were.
    # Resolution is evaluated dynamically by callers.
    return waiver


def revoke_waiver(workspace: WorkspaceLike, waiver_id: str, *, actor: str) -> None:
    workspace.waivers.revoke(waiver_id)
    workspace.audit.append("waiver.revoked", actor, {"waiver_id": waiver_id})


def has_active_waiver(workspace: WorkspaceLike, artifact_id: str, *, scopes: frozenset[str], now: str | None = None) -> bool:
    """True if `artifact_id` has a currently-active (unrevoked, unexpired)
    waiver whose scope is in `scopes`. Evaluated fresh on every call -- this
    is the single dynamic-evaluation choke point used by both
    serving-resolution and rebuild planning."""

    as_of = now or timestamp()
    return any(w.scope in scopes for w in workspace.waivers.active_for_artifact(artifact_id, now=as_of))
