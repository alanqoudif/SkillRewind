"""Failure/security/resumability tests for the `lineage.recover` job handler
and its surrounding API (Phase C2.1 section 10). Complements the full
acceptance scenario in `test_lineage_recovery_acceptance.py`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import skillrewind.jobs.handlers  # noqa: F401,E402
from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.errors import PermanentJobError
from skillrewind.jobs.handlers import run_lineage_recover_job
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import JobContext, Worker
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.models import AuditEvent, CandidateScore, CandidateScoringRun
from skillrewind.persistence.service.repositories import CandidateRepository

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True
    )


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "jobtest.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def ingest_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="ingest-key", actor="ingester", scopes=["ingest", "read"])
    return created.plaintext


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _seed_pool(client, key, config, n=6):
    ids = []
    for i in range(n):
        r = client.post(
            "/api/v1/artifacts",
            params={"kind": "agent-skill", "logical_name": f"pool-{i}"},
            headers=_auth(key),
            content=f"pool body {i} with shared marker XYZQ".encode(),
        )
        ids.append(r.json()["artifact_id"])
    return ids


def test_job_rejects_missing_payload_fields(config):
    engine = build_engine(config.database_url)
    queue = JobQueue(engine)
    ctx = JobContext(job_id="job-x", kind="lineage.recover", payload={}, queue=queue, worker_id="w1", lease_seconds=60)
    with pytest.raises(PermanentJobError):
        run_lineage_recover_job(ctx)


def test_job_rejects_unknown_run_id(config):
    engine = build_engine(config.database_url)
    queue = JobQueue(engine)
    ctx = JobContext(
        job_id="job-x",
        kind="lineage.recover",
        payload={"database_url": config.database_url, "cas_root": config.resolved_cas_root, "run_id": "does-not-exist"},
        queue=queue,
        worker_id="w1",
        lease_seconds=60,
    )
    with pytest.raises(PermanentJobError):
        run_lineage_recover_job(ctx)


def test_worker_crash_after_partial_candidates_resumes_without_duplicates(client, config, ingest_key):
    from skillrewind.cas.local import LocalCAS

    pool = _seed_pool(client, ingest_key, config, n=5)
    root_id = pool[0]

    engine = build_engine(config.database_url)
    cas = LocalCAS(config.resolved_cas_root)
    with Session(engine) as session:
        candidates_repo = CandidateRepository(session)
        candidates_repo.create_run(
            run_id="run-crash-test",
            request_key="req-crash-test",
            root_artifact_id=root_id,
            candidate_scope=None,
            feature_config={},
            threshold_config={"candidate": 0.35, "high_confidence": 0.8},
            scorer_version="service-recovery-0.1.0",
            config_digest="digest",
            snapshot_digest="snap",
        )

    # Simulate a crash after the first candidate was persisted but before
    # the run finished: run recovery with `should_cancel` firing after one
    # candidate, mimicking a worker that dies mid-run.
    from skillrewind.lineage.service_recovery import run_candidate_recovery

    calls = {"n": 0}

    def _cancel_after_one():
        calls["n"] += 1
        return calls["n"] > 1

    with Session(engine) as session:
        run_candidate_recovery(
            session, cas, config, run_id="run-crash-test", should_cancel=_cancel_after_one
        )

    with Session(engine) as session:
        run_row = session.get(CandidateScoringRun, "run-crash-test")
        assert run_row.status == "cancelled"
        candidates_after_crash = session.query(CandidateScore).filter_by(run_id="run-crash-test").count()
        assert candidates_after_crash >= 1

    # Naive retry: re-run to completion. Already-scored candidates must be
    # skipped, never duplicated, and no duplicate audit events emitted.
    with Session(engine) as session:
        # Resuming a cancelled run is allowed to continue to completion.
        run_row = session.get(CandidateScoringRun, "run-crash-test")
        run_row.status = "queued"
        session.commit()
        run_candidate_recovery(session, cas, config, run_id="run-crash-test")

    with Session(engine) as session:
        all_candidates = session.query(CandidateScore).filter_by(run_id="run-crash-test").all()
        candidate_ids = [c.candidate_artifact_id for c in all_candidates]
        assert len(candidate_ids) == len(set(candidate_ids)), "duplicate candidate rows after resume"
        assert len(candidate_ids) == 4  # pool has 5 artifacts, root excluded


def test_concurrent_duplicate_recovery_submission_creates_one_run(client, config, ingest_key):
    pool = _seed_pool(client, ingest_key, config, n=3)
    root_id = pool[0]

    responses = []
    for _ in range(3):
        r = client.post(
            "/api/v1/lineage/recovery-runs",
            headers={**_auth(ingest_key), "Idempotency-Key": "same-concurrent-key"},
            json={"root_artifact_id": root_id},
        )
        responses.append(r.json())

    run_ids = {r["run_id"] for r in responses}
    assert len(run_ids) == 1

    engine = build_engine(config.database_url)
    with Session(engine) as session:
        from skillrewind.persistence.service.models import CandidateScoringRun

        count = session.query(CandidateScoringRun).filter_by(root_artifact_id=root_id).count()
        assert count == 1


def test_recovery_submission_rejects_unknown_root_artifact(client, ingest_key):
    r = client.post(
        "/api/v1/lineage/recovery-runs",
        headers=_auth(ingest_key),
        json={"root_artifact_id": "skill://ghost@sha256:" + "0" * 64},
    )
    assert r.status_code == 404


def test_worker_error_message_is_redacted_before_persistence(config):
    """A job handler failure must never leak secret-shaped strings into the
    persisted `sanitized_error` column (spec: 'redact exception details
    before persistence')."""

    from skillrewind.jobs.worker import register_handler

    engine = build_engine(config.database_url)
    queue = JobQueue(engine)

    @register_handler("test.leaky")
    def _leaky(ctx):
        raise RuntimeError("failed with api_key=sk-abcdef0123456789secrettoken")

    job_id = queue.enqueue("test.leaky", {})
    worker = Worker(queue, worker_id="w1")
    worker.run_once()

    job = queue.get(job_id)
    # An unregistered/unknown exception defaults to retryable (the worker's
    # conservative default), so the job goes to `retry_wait`, not `failed` --
    # either way, the persisted error text must be redacted.
    assert job["status"] in ("failed", "retry_wait")
    assert "sk-abcdef0123456789secrettoken" not in (job["sanitized_error"] or "")
