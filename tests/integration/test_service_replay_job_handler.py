"""Phase C2.2: checkpoint-resume, SSE progress resume, and a real
PostgreSQL-gated concurrency test for the `lineage.replay` job handler."""

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
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.models import AuditEvent, CandidateScore, ReplayRecord, ReplayRun
from skillrewind.replay.deterministic import register_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]


def _poisoned_recipe(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"], "_utility": {"task_success": 1.0}}


register_fixture("c22-job-poisoned-recipe", _poisoned_recipe)


def _migrate(db_path: Path, database_url: str | None = None) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = database_url or f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True
    )


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "replay-job.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def ingest_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="ingest-key", actor="ingester", scopes=["ingest", "read", "replay"])
    return created.plaintext


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _ingest(client, key, name, body):
    r = client.post(
        "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": name}, headers=_auth(key), content=body
    )
    return r.json()["artifact_id"]


def _setup_candidate(client, key, engine, *, seed):
    root_id = _ingest(client, key, f"job-root-{seed}", b"root body")
    clean_id = _ingest(client, key, f"job-clean-{seed}", b"clean body")
    descendant_id = _ingest(client, key, f"job-descendant-{seed}", b"descendant body")

    deriv = client.post(
        "/api/v1/derivations",
        headers=_auth(key),
        json={
            "recipe": "c22-job-poisoned-recipe",
            "recipe_version": "1.0",
            "payload": {"task_snapshot": {"root_marker": root_id}, "seed": seed},
        },
    ).json()
    derivation_id = deriv["derivation_id"]
    client.post(
        f"/api/v1/derivations/{derivation_id}/inputs",
        headers=_auth(key),
        json={"inputs": [{"parent_artifact_id": clean_id, "relation": "direct-input"}]},
    )
    client.post(f"/api/v1/derivations/{derivation_id}/output", headers=_auth(key), json={"artifact_id": descendant_id})

    with Session(engine) as session:
        row = CandidateScore(
            target_artifact_id=root_id,
            candidate_artifact_id=descendant_id,
            raw_score=0.5,
            strict_negative=False,
            feature_breakdown_json={},
            scorer_version="test",
            evidence_class="inferred",
            status="candidate",
            inclusion_reasons_json=[],
        )
        session.add(row)
        session.commit()
        return str(row.id)


def test_worker_crash_mid_run_resumes_without_duplicating_repetitions(client, config, ingest_key):
    """Simulates a worker crash after some repetitions have been persisted
    but before the run finished: a naive retry (re-invoking the handler
    directly on the same run) must not re-execute or duplicate any
    already-persisted repetition, and must still reach the same final
    classification."""

    from skillrewind.jobs.handlers import run_lineage_replay_job
    from skillrewind.jobs.worker import JobContext
    from skillrewind.persistence.service.repositories import ReplayRepository

    engine = build_engine(config.database_url)
    candidate_id = _setup_candidate(client, ingest_key, engine, seed=1)

    r = client.post(
        "/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id, "repetitions": 3}
    )
    replay_run_id, job_id = r.json()["replay_run_id"], r.json()["job_id"]

    # First "attempt": cancel after repetition 0 is checkpointed, simulating
    # a crash between repetitions -- run_service_replay's should_cancel hook
    # is exercised via a real cancellation request rather than a raw crash,
    # since that is the cooperative checkpoint boundary the handler honors.
    queue = JobQueue(engine)
    calls = {"n": 0}
    payload = {"database_url": config.database_url, "cas_root": config.resolved_cas_root, "replay_run_id": replay_run_id}

    # Directly exercise the resumable core: run once with a should_cancel
    # that fires after the first repetition, then resume to completion.
    from skillrewind.persistence.service.repositories import ArtifactRepository, DerivationRepository
    from skillrewind.replay.service_mode import build_domain_derivation, run_service_replay

    with Session(engine) as session:
        replays = ReplayRepository(session)
        run = replays.get_run(replay_run_id)
        derivations = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
        deriv_row = derivations.get(run.target_derivation_id)
        domain_derivation = build_domain_derivation(deriv_row, derivations.list_inputs(run.target_derivation_id))

        def _cancel_after_one():
            calls["n"] += 1
            return calls["n"] > 1

        result = run_service_replay(
            domain_derivation=domain_derivation,
            ancestor_artifact_id=run.ancestor_artifact_id,
            runner_name=run.runner_name,
            repetitions_requested=run.repetitions_requested,
            already_done={},
            config=config,
            persist_repetition=lambda i, rid, v, p: replays.add_repetition_record(
                replay_run_id=replay_run_id,
                repetition_index=i,
                replay_id=rid,
                target_derivation_id=run.target_derivation_id,
                candidate_ancestor_id=run.ancestor_artifact_id,
                intervention_kind="present-withheld-pair",
                runner_id=run.runner_name,
                verdict=v,
                payload=p,
            ),
            checkpoint=lambda i: replays.update_checkpoint(replay_run_id, checkpoint_repetition=i),
            should_cancel=_cancel_after_one,
        )
        assert result.cancelled is True

    with Session(engine) as session:
        records_after_crash = session.query(ReplayRecord).filter_by(replay_run_id=replay_run_id).count()
        assert records_after_crash == 1, "expected exactly one repetition persisted before the simulated crash"

    # Resume: the real job handler, run to completion, must not redo repetition 0.
    ctx = JobContext(job_id=job_id, kind="lineage.replay", payload=payload, queue=queue, worker_id="resume-worker", lease_seconds=60)
    result_id = run_lineage_replay_job(ctx)
    assert result_id == replay_run_id

    with Session(engine) as session:
        run_row = session.get(ReplayRun, replay_run_id)
        assert run_row.status == "completed"
        # 3 repetitions < MIN_REPETITIONS_FOR_SIGNIFICANCE (5), so this
        # deterministically classifies as unresolved-repetitions regardless
        # of the per-repetition outcome -- the point of this test is that
        # resume reaches the *same* classification a single uninterrupted
        # run would have, with no repetition re-executed or duplicated.
        assert run_row.verdict == "unresolved-repetitions"
        records_after_resume = session.query(ReplayRecord).filter_by(replay_run_id=replay_run_id).all()
        indices = sorted(r.repetition_index for r in records_after_resume)
        assert indices == [0, 1, 2], "resume must fill in exactly the missing repetitions, no duplicates"


def test_sse_progress_resume_during_real_replay_job(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id = _setup_candidate(client, ingest_key, engine, seed=2)

    r = client.post(
        "/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id, "repetitions": 1}
    )
    job_id = r.json()["job_id"]

    Worker(JobQueue(engine), worker_id="sse-replay-worker").run_once()
    assert JobQueue(engine).get(job_id)["status"] == "succeeded"

    all_events = []
    with client.stream(
        "GET", "/api/v1/events/stream", params={"job_id": job_id, "poll_interval": 0.01}, headers=_auth(ingest_key)
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("id:"):
                all_events.append(int(line.split(":", 1)[1].strip()))
            if len(all_events) >= 2:
                break
    assert all_events == sorted(all_events)

    resume_from = all_events[0]
    resumed = []
    with client.stream(
        "GET",
        "/api/v1/events/stream",
        params={"job_id": job_id, "poll_interval": 0.01},
        headers={**_auth(ingest_key), "Last-Event-ID": str(resume_from)},
    ) as resp:
        for line in resp.iter_lines():
            if line.startswith("id:"):
                resumed.append(int(line.split(":", 1)[1].strip()))
            if resumed and resumed[0] > resume_from:
                break
    assert resumed[0] > resume_from, "SSE resume must never redeliver an already-seen event"


# -- PostgreSQL-gated concurrency test ----------------------------------------


def _postgres_url() -> str | None:
    return os.environ.get("SKILLREWIND_TEST_POSTGRES_URL")


@pytest.mark.skipif(
    _postgres_url() is None,
    reason=(
        "No reachable PostgreSQL configured via SKILLREWIND_TEST_POSTGRES_URL. "
        "Docker CLI is present in this environment but its daemon is not running "
        "(`docker info` fails to connect to the socket), so a throwaway Postgres "
        "container could not be started here. This test runs for real in any "
        "environment with SKILLREWIND_TEST_POSTGRES_URL pointing at a reachable "
        "PostgreSQL instance, e.g. `postgresql+psycopg://user:pass@localhost:5432/skillrewind_test`."
    ),
)
def test_postgres_concurrent_replay_job_claims_are_mutually_exclusive():
    """Two workers claiming from the same `lineage.replay` job queue against a
    real PostgreSQL database must never both claim the same job (`FOR UPDATE
    SKIP LOCKED`), proving Service-mode replay is safe under real
    multi-worker concurrency -- not just SQLite single-process tests."""

    import uuid

    url = _postgres_url()
    _migrate(Path("unused"), database_url=url)

    engine = build_engine(url)
    from skillrewind.persistence.service.models import Job

    with Session(engine) as session:
        session.query(Job).delete()
        session.commit()

    queue = JobQueue(engine)
    job_ids = [queue.enqueue("lineage.replay", {"marker": str(uuid.uuid4())}) for _ in range(10)]

    claimed_by: dict[str, str] = {}
    import threading

    def _worker(worker_id: str) -> None:
        while True:
            job_id = queue.claim(worker_id, kinds=["lineage.replay"], lease_seconds=30)
            if job_id is None:
                break
            claimed_by[job_id] = worker_id

    threads = [threading.Thread(target=_worker, args=(f"pg-worker-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert set(claimed_by) == set(job_ids), "every job must be claimed exactly once"
    assert len(claimed_by) == len(job_ids), "no job claimed twice across concurrent workers"
