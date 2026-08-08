from __future__ import annotations

import json

from skillrewind.capture.importer import import_jsonl
from skillrewind.domain.enums import ArtifactKind
from skillrewind.workspace import Workspace


def test_import_jsonl_creates_derivation_and_edges(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    ctx = ws.ingest_artifact(b"context", kind=ArtifactKind.MEMORY, logical_name="mem-1")
    target = ws.ingest_artifact(b"produced", kind=ArtifactKind.AGENT_SKILL, logical_name="produced")

    trace_path = tmp_path / "trace.jsonl"
    events = [
        {"derivation_id": "d1", "type": "task-loaded", "task": {"goal": "x"}, "at": "2026-01-01T00:00:00Z"},
        {"derivation_id": "d1", "type": "memory-retrieved", "artifact_id": ctx.artifact_id, "at": "2026-01-01T00:00:01Z"},
        {"derivation_id": "d1", "type": "tool-called", "name": "http", "at": "2026-01-01T00:00:02Z"},
        {"derivation_id": "d1", "type": "artifact-produced", "artifact_id": target.artifact_id, "at": "2026-01-01T00:00:03Z"},
    ]
    with trace_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    summary = import_jsonl(ws, trace_path)
    assert summary.derivations_created == 1
    assert summary.edges_created == 1

    derivation = ws.derivations.get("d1")
    assert derivation.target_artifact_id == target.artifact_id
    incoming = ws.edges.incoming(target.artifact_id)
    assert {e.source for e in incoming} == {ctx.artifact_id}
    ws.close()


def test_import_jsonl_warns_on_unknown_artifact(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    target = ws.ingest_artifact(b"produced", kind=ArtifactKind.AGENT_SKILL, logical_name="produced")
    trace_path = tmp_path / "trace.jsonl"
    events = [
        {"derivation_id": "d1", "type": "skill-activated", "artifact_id": "skill://unknown@sha256:" + "0" * 64},
        {"derivation_id": "d1", "type": "artifact-produced", "artifact_id": target.artifact_id},
    ]
    with trace_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    summary = import_jsonl(ws, trace_path)
    assert summary.edges_created == 0
    assert len(summary.warnings) == 1
    ws.close()
