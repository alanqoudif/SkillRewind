from __future__ import annotations

import pytest

from skillrewind.capture.sdk import SkillRewindClient
from skillrewind.domain.enums import ArtifactKind
from skillrewind.workspace import Workspace


def test_derivation_commit_creates_recorded_edges(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    client = SkillRewindClient(workspace=ws)
    root = ws.ingest_artifact(b"root skill", kind=ArtifactKind.AGENT_SKILL, logical_name="root")
    ctx = ws.ingest_artifact(b"context skill", kind=ArtifactKind.AGENT_SKILL, logical_name="ctx")

    with client.derivation(
        recipe="skill-distillation", recipe_version="0.2", logical_name="deploy-service",
        task_snapshot={"task": "deploy"}, model={"provider": "local", "id": "fixture"},
    ) as run:
        run.record_input(root.artifact_id, relation="used-as-input")
        run.record_context_exposure(ctx.artifact_id)
        run.record_tool_call(name="mock_http", arguments={"url": "local://fixture", "api_key": "sk-should-be-redacted-1234567890"})
        run.record_tool_result(name="mock_http", result={"ok": True})
        artifact_id = run.commit_artifact(kind="agent-skill", logical_name="deploy-service", content=b"deploy-service body")

    derivation = ws.derivations.get(run.derivation_id)
    assert derivation.target_artifact_id == artifact_id
    assert derivation.status == "completed"

    incoming = ws.edges.incoming(artifact_id)
    sources = {e.source for e in incoming}
    assert root.artifact_id in sources
    assert ctx.artifact_id in sources

    # secret redaction applied before persistence
    tool_call_payload = str(derivation.tool_calls)
    assert "sk-should-be-redacted-1234567890" not in tool_call_payload
    ws.close()


def test_failed_derivation_preserves_prior_events(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    client = SkillRewindClient(workspace=ws)
    root = ws.ingest_artifact(b"root", kind=ArtifactKind.AGENT_SKILL, logical_name="root")

    with pytest.raises(RuntimeError):
        with client.derivation(recipe="r", recipe_version="1", logical_name="broken") as run:
            run.record_input(root.artifact_id)
            raise RuntimeError("boom")

    derivation = ws.derivations.get(run.derivation_id)
    assert derivation.status == "failed"
    assert derivation.replay_limitations == ["boom"]
    ws.close()
