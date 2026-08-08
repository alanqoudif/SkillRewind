from __future__ import annotations

from skillrewind.domain.enums import ArtifactKind, LifecycleStatus
from skillrewind.workspace import Workspace


def test_workspace_init_and_ingest_roundtrip(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    artifact = ws.ingest_artifact(
        b"print('hello')",
        kind=ArtifactKind.SOURCE_CODE,
        logical_name="hello",
        mime_type="text/x-python",
        alias="hello-alias",
    )
    fetched = ws.artifacts.get(artifact.artifact_id)
    assert fetched.artifact_id == artifact.artifact_id
    assert fetched.status == LifecycleStatus.ACTIVE
    assert ws.cas.get_bytes(artifact.digest_hex) == b"print('hello')"

    resolved = ws.resolve_alias("hello-alias")
    assert resolved is not None and resolved.artifact_id == artifact.artifact_id

    result = ws.audit.verify()
    assert result.ok
    ws.close()


def test_workspace_reopen_persists_state(tmp_path):
    ws_path = tmp_path / "ws"
    ws1 = Workspace.init(ws_path)
    artifact = ws1.ingest_artifact(b"data", kind=ArtifactKind.MEMORY, logical_name="mem-1")
    ws1.close()

    ws2 = Workspace.open(ws_path)
    fetched = ws2.artifacts.get(artifact.artifact_id)
    assert fetched.digest_hex == artifact.digest_hex
    assert ws2.audit.verify().ok
    ws2.close()


def test_resolve_alias_excludes_quarantined(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    artifact = ws.ingest_artifact(
        b"x", kind=ArtifactKind.AGENT_SKILL, logical_name="skill-a", alias="skill-a"
    )
    ws.revocations.add_quarantine(artifact.artifact_id, "rev-1", "test", "2026-01-01T00:00:00Z")
    assert ws.resolve_alias("skill-a") is None
    ws.close()
