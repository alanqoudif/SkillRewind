from __future__ import annotations

import pytest

from skillrewind.domain.enums import ArtifactKind, LifecycleStatus, RevocationPolicy
from skillrewind.domain.errors import PolicyViolationError
from skillrewind.quarantine.service import quarantine_artifact
from skillrewind.quarantine.waivers import create_waiver, revoke_waiver
from skillrewind.workspace import Workspace


def test_waiver_releases_quarantine_and_is_audited(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    artifact = ws.ingest_artifact(b"x", kind=ArtifactKind.AGENT_SKILL, logical_name="a", alias="a")
    quarantine_artifact(ws, artifact.artifact_id, revocation_event_id="rev-1", reason="test")
    assert ws.resolve_alias("a") is None

    waiver = create_waiver(ws, artifact.artifact_id, actor="admin", reason="reviewed and safe")
    assert ws.resolve_alias("a") is not None
    assert not ws.revocations.is_quarantined(artifact.artifact_id)

    revoke_waiver(ws, waiver.waiver_id, actor="admin")
    events = [e.event_type for e in ws.audit.iter_events()]
    assert "waiver.created" in events
    assert "waiver.revoked" in events
    ws.close()


def test_strict_mode_forbids_waivers_by_default(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    artifact = ws.ingest_artifact(b"x", kind=ArtifactKind.AGENT_SKILL, logical_name="a")
    quarantine_artifact(ws, artifact.artifact_id, revocation_event_id="rev-1", reason="test")
    with pytest.raises(PolicyViolationError):
        create_waiver(ws, artifact.artifact_id, actor="admin", reason="x", active_policy=RevocationPolicy.STRICT)
    ws.close()


def test_waiver_does_not_delete_quarantine_history(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    artifact = ws.ingest_artifact(b"x", kind=ArtifactKind.AGENT_SKILL, logical_name="a")
    quarantine_artifact(ws, artifact.artifact_id, revocation_event_id="rev-1", reason="original reason")
    create_waiver(ws, artifact.artifact_id, actor="admin", reason="override")
    events = [e for e in ws.audit.iter_events() if e.event_type == "artifact.quarantined"]
    assert events and events[0].payload["reason"] == "original reason"
    ws.close()
