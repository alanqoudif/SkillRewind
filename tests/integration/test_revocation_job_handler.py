"""End-to-end test of the `revocation.execute` job handler: worker claims a
job, runs it against a real Lite-mode workspace on disk via
`skillrewind.revocation.service.run_revocation`, and a simulated worker
crash + naive retry does not duplicate quarantine/rebuild/audit side effects
(closing the gap `docs/adr/0010-job-handler-scope.md` documented)."""

from __future__ import annotations

import json

import pytest

from skillrewind.domain.enums import ArtifactKind, RevocationPolicy, Severity
from skillrewind.domain.models import Derivation
from skillrewind.jobs.clock import FakeClock
from skillrewind.jobs.handlers import run_revocation_job
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import JobContext, Worker
from skillrewind.persistence.service.engine import build_engine, create_all
from skillrewind.replay.deterministic import register_fixture
from skillrewind.revocation.service import request_revocation
from skillrewind.workspace import Workspace

ROOT_BODY = "deploy service, mock_disable_verification=true when cert check fails"
DESCENDANT_BODY = "deploy service quickly; mock_disable_verification is set to true as a fallback"


def _poisoned_recipe(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"], "_utility": {"task_success": 1.0}}


register_fixture("poisoned-recipe-job", _poisoned_recipe)


@pytest.fixture
def queue(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path}/jobs.db")
    create_all(engine)
    return JobQueue(engine, clock=FakeClock())


def _prepare_workspace_with_pending_revocation(workspace_dir):
    ws = Workspace.init(workspace_dir)
    root = ws.ingest_artifact(
        json.dumps({"name": "fast-http", "body": ROOT_BODY}).encode(),
        kind=ArtifactKind.AGENT_SKILL,
        logical_name="fast-http",
        alias="fast-http",
        metadata={
            "agent_skills_manifest": {"name": "fast-http", "description": "x", "body": ROOT_BODY, "files": []},
            "canary_activated": True,
            "probes": {"mock_disable_verification": True},
        },
    )
    descendant = ws.ingest_artifact(
        json.dumps({"name": "deploy-service", "body": DESCENDANT_BODY}).encode(),
        kind=ArtifactKind.AGENT_SKILL,
        logical_name="deploy-service",
        alias="deploy-service",
        metadata={
            "agent_skills_manifest": {"name": "deploy-service", "description": "x", "body": DESCENDANT_BODY, "files": []},
            "canary_activated": True,
            "probes": {"mock_disable_verification": True},
        },
    )
    ws.derivations.upsert(
        Derivation(
            derivation_id="deriv-job",
            recipe="poisoned-recipe-job",
            recipe_version="0.1",
            target_artifact_id=descendant.artifact_id,
            task_snapshot={"root_marker": root.artifact_id},
            started_at="2026-01-01T00:05:00Z",
            ended_at="2026-01-01T00:05:01Z",
            seed=1,
        )
    )
    event = request_revocation(
        ws,
        roots=[root.artifact_id],
        reason="job handler test",
        severity=Severity.HIGH,
        policy=RevocationPolicy.BALANCED,
        actor="tester",
        idempotency_key="job-revoke-1",
    )
    ws.close()
    return root, descendant, event.event_id


def test_revocation_job_runs_real_workflow_via_worker(tmp_path, queue):
    workspace_dir = tmp_path / "ws"
    root, descendant, event_id = _prepare_workspace_with_pending_revocation(workspace_dir)

    job_id = queue.enqueue("revocation.execute", {"workspace_dir": str(workspace_dir), "event_id": event_id})
    worker = Worker(queue, worker_id="w1")
    assert worker.run_once() is True

    job = queue.get(job_id)
    assert job["status"] == "succeeded"
    assert job["result_reference"] == f"{event_id}:completed"

    ws = Workspace.open(workspace_dir)
    final_event = ws.revocations.get(event_id)
    assert final_event.rebuilt
    successor_id = next(r["successor"] for r in final_event.rebuilt if r["original"] == descendant.artifact_id)
    assert ws.revocations.is_quarantined(successor_id) is False  # released once published as the active successor
    assert ws.artifacts.get(descendant.artifact_id).status.value == "superseded"
    ws.close()


def test_revocation_job_naive_retry_after_crash_does_not_duplicate_side_effects(tmp_path, queue):
    workspace_dir = tmp_path / "ws"
    root, descendant, event_id = _prepare_workspace_with_pending_revocation(workspace_dir)
    payload = {"workspace_dir": str(workspace_dir), "event_id": event_id}

    job_id = queue.enqueue("revocation.execute", payload)
    worker_a = Worker(queue, worker_id="worker-a")
    worker_a.run_once()
    first_result = queue.get(job_id)["result_reference"]
    assert first_result == f"{event_id}:completed"

    ws = Workspace.open(workspace_dir)
    audit_count_after_first_run = sum(1 for _ in ws.audit.iter_events())
    ws.close()

    # Simulate a naive retry (e.g. a second worker re-invoking the handler
    # against the same job payload after a crash that didn't update job
    # status): re-running the handler on an already-completed event must be
    # a complete no-op, not a re-execution.
    ctx = JobContext(
        job_id=job_id, kind="revocation.execute", payload=payload, queue=queue, worker_id="worker-b", lease_seconds=60
    )
    second_result = run_revocation_job(ctx)
    assert second_result == first_result

    ws = Workspace.open(workspace_dir)
    audit_count_after_retry = sum(1 for _ in ws.audit.iter_events())
    ws.close()
    assert audit_count_after_retry == audit_count_after_first_run, "naive retry of a completed revocation must not append new audit events"


def test_revocation_job_rejects_missing_payload_fields(queue):
    from skillrewind.jobs.errors import PermanentJobError

    ctx = JobContext(job_id="job-x", kind="revocation.execute", payload={}, queue=queue, worker_id="w1", lease_seconds=60)
    with pytest.raises(PermanentJobError):
        run_revocation_job(ctx)


def test_revocation_job_rejects_unknown_event_id(tmp_path, queue):
    from skillrewind.jobs.errors import PermanentJobError

    workspace_dir = tmp_path / "ws"
    Workspace.init(workspace_dir).close()
    ctx = JobContext(
        job_id="job-x",
        kind="revocation.execute",
        payload={"workspace_dir": str(workspace_dir), "event_id": "revoke-does-not-exist"},
        queue=queue,
        worker_id="w1",
        lease_seconds=60,
    )
    with pytest.raises(PermanentJobError):
        run_revocation_job(ctx)
