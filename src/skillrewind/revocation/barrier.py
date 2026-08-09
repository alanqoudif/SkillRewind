"""Barrier application: make revoked roots and recorded descendants non-servable
before any long-running analysis (candidate recovery, replay) begins, for
``balanced``/``strict`` policies. ``forensic`` mode never applies a barrier.
"""

from __future__ import annotations

from ..domain.enums import LifecycleStatus, RevocationPolicy
from ..domain.models import RevocationEvent
from ..lineage.closure import recorded_descendants
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike


def apply_barrier(workspace: WorkspaceLike, event: RevocationEvent) -> list[str]:
    """Mark roots revoked and recorded descendants quarantined.

    Each artifact-status/quarantine write commits individually (the
    repository layer commits per call rather than batching a single SQLite
    transaction across the whole closure) -- this is a known limitation, not
    a claim of atomicity across the full barrier. Every individual write is
    idempotent (setting an already-revoked/quarantined artifact to the same
    state is a no-op), so a crash mid-barrier leaves a partially-applied but
    internally consistent state that a retry (same idempotency key) safely
    completes rather than corrupts."""

    if event.policy == RevocationPolicy.FORENSIC:
        return []

    try:
        closure = recorded_descendants(workspace, event.roots, include_roots=True)
    except ValueError:
        closure = tuple(event.roots)

    now = timestamp()
    for artifact_id in closure:
        if artifact_id in event.roots:
            workspace.artifacts.set_status(artifact_id, LifecycleStatus.REVOKED)
        else:
            workspace.revocations.add_quarantine(
                artifact_id, event.event_id, "barrier: recorded descendant of revoked root", now
            )
            workspace.artifacts.set_status(artifact_id, LifecycleStatus.QUARANTINED)

    workspace.audit.append(
        "revocation.barrier-applied",
        event.actor,
        {"event_id": event.event_id, "roots": event.roots, "closure_size": len(closure)},
    )
    return list(closure)
