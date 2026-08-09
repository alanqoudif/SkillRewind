"""Phase C2.2 Service-mode replay: evidence-promotion tests through the real
FastAPI app + real DB-backed job queue + real worker. Verifies database rows,
not just HTTP status codes."""

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
from skillrewind.replay.base import DerivationRunner, ReplaySpec, RunnerOutput, register_runner
from skillrewind.replay.deterministic import register_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]


# -- deterministic fixtures shared across this file's tests -----------------


def _poisoned_recipe(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"], "_utility": {"task_success": 1.0}}


def _inert_recipe(task_snapshot, available_context, seed):
    return {"mock_disable_verification": False, "_behavior_keys": ["mock_disable_verification"]}


def _crashing_recipe(task_snapshot, available_context, seed):
    raise RuntimeError("simulated runner crash")


register_fixture("c22-poisoned-recipe", _poisoned_recipe)
register_fixture("c22-inert-recipe", _inert_recipe)
register_fixture("c22-crashing-recipe", _crashing_recipe)


class _LowFidelityRunner:
    """A second approved runner (registered in-process, not user-supplied)
    that always reports low fidelity, used to exercise the
    UNRESOLVED_FIDELITY classification path deterministically."""

    name = "low-fidelity-test-runner"

    def run(self, spec: ReplaySpec) -> RunnerOutput:
        return RunnerOutput(
            outputs={"x": True},
            target_behavior_vector={"x": True},
            fidelity_components={
                "task_snapshot_match": 0.1,
                "model_match": 0.1,
                "environment_match": 0.1,
                "seed_support": 0.1,
                "candidate_context_reconstruction": 0.1,
            },
            model_identity="low-fidelity-test-runner",
            environment_identity="in-process",
        )


register_runner(_LowFidelityRunner())


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True
    )


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "replay.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def ingest_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="ingest-key", actor="tenant-a", scopes=["ingest", "read", "replay"])
    return created.plaintext


@pytest.fixture
def other_tenant_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="other-tenant-key", actor="tenant-b", scopes=["ingest", "read", "replay"])
    return created.plaintext


@pytest.fixture
def read_only_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="read-only-key", actor="reader", scopes=["read"])
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
    assert r.status_code == 201, r.text
    return r.json()["artifact_id"]


def _setup_candidate(client, key, engine, *, recipe, seed=1, repetitions_hint=None):
    """Ingests root/clean-input/descendant, records a derivation for the
    descendant that omits the root, then directly inserts an `inferred`
    `CandidateScore` row (bypassing the full candidate-recovery pipeline,
    which is already tested in Phase C2.1) so this file's tests can focus
    purely on the replay/evidence-promotion path. Returns
    (candidate_id, root_id, descendant_id)."""

    root_id = _ingest(client, key, f"root-{recipe}-{seed}", b"root body")
    clean_id = _ingest(client, key, f"clean-{recipe}-{seed}", b"clean body")
    descendant_id = _ingest(client, key, f"descendant-{recipe}-{seed}", b"descendant body")

    deriv = client.post(
        "/api/v1/derivations",
        headers=_auth(key),
        json={
            "recipe": recipe,
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
        candidate_id = str(row.id)
    return candidate_id, root_id, descendant_id


def _run_worker(config):
    engine = build_engine(config.database_url)
    Worker(JobQueue(engine), worker_id="replay-test-worker").run_once()


# -- happy paths --------------------------------------------------------------


def test_replay_confirmed_happy_path(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, root_id, descendant_id = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe")

    r = client.post(
        "/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id, "repetitions": 1}
    )
    assert r.status_code == 202, r.text
    replay_run_id = r.json()["replay_run_id"]
    _run_worker(config)

    run = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key)).json()
    assert run["status"] == "completed"
    assert run["verdict"] == "confirmed"

    classification = client.get(f"/api/v1/replay/runs/{replay_run_id}/classification", headers=_auth(ingest_key)).json()
    assert classification["verdict"] == "confirmed"
    assert classification["is_calibrated_probability"] is False

    evidence = client.get(f"/api/v1/replay/runs/{replay_run_id}/evidence", headers=_auth(ingest_key)).json()
    assert len(evidence["repetitions"]) == 1
    assert evidence["repetitions"][0]["payload"]["any_change"] is True

    # Original inferred CandidateScore evidence is preserved, not overwritten.
    with Session(engine) as session:
        candidate_row = session.get(CandidateScore, int(candidate_id))
        assert candidate_row.evidence_class == "inferred"  # never mutated to replay-confirmed
        assert candidate_row.raw_score == 0.5
        assert candidate_row.latest_replay_verdict == "confirmed"

        from skillrewind.persistence.service.models import InfluenceEdge

        edge = (
            session.query(InfluenceEdge)
            .filter_by(source=root_id, target=descendant_id, relation="hidden-influence")
            .one()
        )
        assert edge.evidence_class == "replay-confirmed"

        audit_types = {e.event_type for e in session.query(AuditEvent).all()}
        assert {"replay.run_created", "replay.run_completed"} <= audit_types


def test_replay_rejected_under_negative_intervention(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-inert-recipe", seed=2)

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    replay_run_id = r.json()["replay_run_id"]
    _run_worker(config)

    run = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key)).json()
    assert run["verdict"] == "rejected"

    with Session(engine) as session:
        from skillrewind.persistence.service.models import InfluenceEdge

        edge = session.query(InfluenceEdge).filter_by(relation="hidden-influence").one()
        assert edge.evidence_class == "rejected"


def test_runner_failure_becomes_unresolved_never_rejected(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-crashing-recipe", seed=3)

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    replay_run_id = r.json()["replay_run_id"]
    _run_worker(config)

    run = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key)).json()
    assert run["verdict"] == "unresolved-runner-failure"
    assert run["verdict"] != "rejected"


def test_fidelity_failure_becomes_unresolved(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=4)

    r = client.post(
        "/api/v1/replay/runs",
        headers=_auth(ingest_key),
        json={"candidate_id": candidate_id, "runner_name": "low-fidelity-test-runner"},
    )
    assert r.status_code == 202, r.text
    replay_run_id = r.json()["replay_run_id"]
    _run_worker(config)

    run = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key)).json()
    assert run["verdict"] == "unresolved-fidelity"


def test_insufficient_repetitions_becomes_unresolved(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=5)

    r = client.post(
        "/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id, "repetitions": 2}
    )
    replay_run_id = r.json()["replay_run_id"]
    _run_worker(config)

    run = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key)).json()
    assert run["verdict"] == "unresolved-repetitions"


def test_missing_derivation_becomes_unresolved_missing_inputs(client, config, ingest_key):
    """A candidate whose descendant has no recorded derivation cannot be
    reconstructed -- invariant 2: this must resolve to UNRESOLVED, never
    REJECTED, and the submission itself must still be accepted (202)."""

    engine = build_engine(config.database_url)
    root_id = _ingest(client, ingest_key, "no-deriv-root", b"root body")
    descendant_id = _ingest(client, ingest_key, "no-deriv-descendant", b"descendant body, no derivation recorded")

    with Session(engine) as session:
        row = CandidateScore(
            target_artifact_id=root_id,
            candidate_artifact_id=descendant_id,
            raw_score=0.4,
            strict_negative=False,
            feature_breakdown_json={},
            scorer_version="test",
            evidence_class="inferred",
            status="candidate",
            inclusion_reasons_json=[],
        )
        session.add(row)
        session.commit()
        candidate_id = str(row.id)

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    assert r.status_code == 202, r.text
    replay_run_id = r.json()["replay_run_id"]
    _run_worker(config)

    run = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key)).json()
    assert run["verdict"] == "unresolved-missing-inputs"
    assert run["verdict"] != "rejected"


# -- idempotency / concurrency -------------------------------------------------


def test_duplicate_submission_with_same_idempotency_key_is_idempotent(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=6)

    headers = {**_auth(ingest_key), "Idempotency-Key": "replay-req-1"}
    r1 = client.post("/api/v1/replay/runs", headers=headers, json={"candidate_id": candidate_id})
    r2 = client.post("/api/v1/replay/runs", headers=headers, json={"candidate_id": candidate_id})
    assert r1.json()["replay_run_id"] == r2.json()["replay_run_id"]
    assert r2.json()["idempotent_replay"] is True

    with Session(engine) as session:
        count = session.query(ReplayRun).filter_by(candidate_id=int(candidate_id)).count()
        assert count == 1


def test_concurrent_submission_race_creates_one_run(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=7)

    responses = []
    for _ in range(3):
        r = client.post(
            "/api/v1/replay/runs",
            headers={**_auth(ingest_key), "Idempotency-Key": "concurrent-replay-key"},
            json={"candidate_id": candidate_id},
        )
        responses.append(r.json())
    run_ids = {r["replay_run_id"] for r in responses}
    assert len(run_ids) == 1

    with Session(engine) as session:
        assert session.query(ReplayRun).filter_by(candidate_id=int(candidate_id)).count() == 1


def test_no_duplicate_replay_records_or_audit_events_after_naive_retry(client, config, ingest_key):
    from skillrewind.jobs.handlers import run_lineage_replay_job
    from skillrewind.jobs.worker import JobContext

    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=8)

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    replay_run_id, job_id = r.json()["replay_run_id"], r.json()["job_id"]
    _run_worker(config)

    with Session(engine) as session:
        records_before = session.query(ReplayRecord).filter_by(replay_run_id=replay_run_id).count()
        audit_before = session.query(AuditEvent).count()

    # Naive retry: re-invoke the handler directly against the same
    # (already-completed) run, simulating a crashed worker whose retry
    # re-dispatches the job.
    payload = {"database_url": config.database_url, "cas_root": config.resolved_cas_root, "replay_run_id": replay_run_id}
    ctx = JobContext(job_id=job_id, kind="lineage.replay", payload=payload, queue=JobQueue(engine), worker_id="retry-worker", lease_seconds=60)
    second_result = run_lineage_replay_job(ctx)
    assert second_result == replay_run_id

    with Session(engine) as session:
        records_after = session.query(ReplayRecord).filter_by(replay_run_id=replay_run_id).count()
        audit_after = session.query(AuditEvent).count()
        assert records_after == records_before, "naive retry duplicated replay records"
        assert audit_after == audit_before, "naive retry duplicated audit events"


# -- invariants ----------------------------------------------------------------


def test_recorded_traversal_unchanged_after_replay(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, root_id, descendant_id = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=9)

    before = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=_auth(ingest_key)).json()
    assert descendant_id not in before["items"]

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    _run_worker(config)
    assert client.get(f"/api/v1/replay/runs/{r.json()['replay_run_id']}", headers=_auth(ingest_key)).json()["verdict"] == "confirmed"

    after = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=_auth(ingest_key)).json()
    assert descendant_id not in after["items"], "replay-confirmed evidence must not leak into recorded-only closure"
    assert after["items"] == before["items"]


def test_serving_resolution_unchanged_after_replay(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, root_id, descendant_id = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=10)

    before = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=_auth(ingest_key)).json()
    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    _run_worker(config)
    assert client.get(f"/api/v1/replay/runs/{r.json()['replay_run_id']}", headers=_auth(ingest_key)).json()["verdict"] == "confirmed"

    after = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=_auth(ingest_key)).json()
    assert after == before
    assert after["resolution"] == "active"


def test_cross_tenant_access_is_rejected(client, config, ingest_key, other_tenant_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=11)

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    replay_run_id = r.json()["replay_run_id"]

    forbidden = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(other_tenant_key))
    assert forbidden.status_code == 403

    own = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(ingest_key))
    assert own.status_code == 200


def test_read_only_key_cannot_submit_replay(client, config, ingest_key, read_only_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=12)
    r = client.post("/api/v1/replay/runs", headers=_auth(read_only_key), json={"candidate_id": candidate_id})
    assert r.status_code == 403


def test_tampered_or_missing_cas_input_is_rejected(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, root_id, descendant_id = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=13)

    # Corrupt the root artifact's CAS object on disk.
    from skillrewind.persistence.service.models import Artifact

    with Session(engine) as session:
        artifact = session.get(Artifact, root_id)
        digest = artifact.digest_hex
    cas_path = Path(config.resolved_cas_root) / "objects" / digest[:2] / digest[2:4] / digest
    assert cas_path.is_file()
    cas_path.write_bytes(b"corrupted content, digest no longer matches")

    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    assert r.status_code == 422, r.text


def test_invalid_candidate_id_returns_404(client, ingest_key):
    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": "999999"})
    assert r.status_code == 404


def test_unsupported_runner_returns_422(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=14)
    r = client.post(
        "/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id, "runner_name": "not-a-real-runner"}
    )
    assert r.status_code == 422


def test_classification_conflicts_before_run_finishes(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=15)
    r = client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    replay_run_id = r.json()["replay_run_id"]
    # No worker run yet -- still queued/running.
    c = client.get(f"/api/v1/replay/runs/{replay_run_id}/classification", headers=_auth(ingest_key))
    assert c.status_code == 409


def test_candidate_history_shows_inferred_evidence_plus_replay_outcome(client, config, ingest_key):
    engine = build_engine(config.database_url)
    candidate_id, _, _ = _setup_candidate(client, ingest_key, engine, recipe="c22-poisoned-recipe", seed=16)
    client.post("/api/v1/replay/runs", headers=_auth(ingest_key), json={"candidate_id": candidate_id})
    _run_worker(config)

    history = client.get(f"/api/v1/lineage/candidates/{candidate_id}/history", headers=_auth(ingest_key)).json()
    assert history["candidate"]["evidence_class"] == "inferred"
    assert history["candidate"]["raw_score"] == 0.5
    assert len(history["replay_runs"]) == 1
    assert history["replay_runs"][0]["verdict"] == "confirmed"
