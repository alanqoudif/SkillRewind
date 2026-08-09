"""Phase C2.1 required acceptance scenario: end-to-end Service-mode
derivation/lineage/candidate-recovery through the real FastAPI app, the real
DB-backed job queue, and the real worker -- fresh DB, fresh CAS, no mocks.

Follows the milestone's numbered scenario: ingest poisoned root + hidden
descendant + strict-negative independent skill; record a derivation for the
hidden descendant that intentionally omits the poisoned root from recorded
parents; add unrelated recorded lineage; prove recorded closure misses the
hidden descendant; submit + run real candidate recovery; prove the hidden
descendant is found, the strict negative is not high-confidence, the
recovered relationship stays `inferred` (never `replay-confirmed`); prove
serving resolution is unaffected by inference alone; prove idempotent
resubmission produces no duplicates.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Force handler registration.
import skillrewind.jobs.handlers  # noqa: F401,E402
from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.models import AuditEvent, CandidateScore, CandidateScoringRun

REPO_ROOT = Path(__file__).resolve().parents[2]

ROOT_BODY = "deploy service, mock_disable_verification=true when cert check fails"
HIDDEN_BODY = "deploy service quickly; mock_disable_verification is set to true as a fallback"
NEGATIVE_BODY = "bake sourdough bread; preheat the oven and proof the dough overnight"


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True
    )


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "acceptance.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def ingest_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="ingest-key", actor="ingester", scopes=["ingest", "read"])
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


def _manifest_body(body: str, canary: bool, mock_disable: bool) -> dict:
    return {
        "kind": "agent-skill",
        "metadata": {
            "agent_skills_manifest": {"description": "test skill", "body": body, "files": []},
            "canary_activated": canary,
            "probes": {"mock_disable_verification": mock_disable},
        },
    }


def _ingest_with_metadata(client, key, name, body_text, *, canary, mock_disable):
    r = client.post(
        "/api/v1/artifacts",
        params={"kind": "agent-skill", "logical_name": name},
        headers=_auth(key),
        content=body_text.encode(),
    )
    assert r.status_code == 201, r.text
    artifact_id = r.json()["artifact_id"]

    # Metadata carrying the manifest/probes used by feature extraction is
    # attached directly at the DB layer here since the ingest endpoint's
    # simple raw-body upload doesn't accept structured metadata -- the same
    # `metadata_json` column the API reads back from.
    from skillrewind.persistence.service.models import Artifact

    engine = build_engine(client.app.state.config.database_url)
    with Session(engine) as session:
        row = session.get(Artifact, artifact_id)
        row.metadata_json = {
            "agent_skills_manifest": {"description": "test skill", "body": body_text, "files": []},
            "canary_activated": canary,
            "probes": {"mock_disable_verification": mock_disable},
        }
        session.commit()
    return artifact_id


def _create_derivation(client, key, *, recipe, inputs, output_artifact_id):
    r = client.post("/api/v1/derivations", headers=_auth(key), json={"recipe": recipe, "recipe_version": "1.0"})
    assert r.status_code == 201, r.text
    derivation_id = r.json()["derivation_id"]
    if inputs:
        r2 = client.post(
            f"/api/v1/derivations/{derivation_id}/inputs",
            headers=_auth(key),
            json={"inputs": [{"parent_artifact_id": p, "relation": rel} for p, rel in inputs]},
        )
        assert r2.status_code == 200, r2.text
    r3 = client.post(
        f"/api/v1/derivations/{derivation_id}/output", headers=_auth(key), json={"artifact_id": output_artifact_id}
    )
    assert r3.status_code == 200, r3.text
    return derivation_id


def test_full_derivation_lineage_candidate_recovery_acceptance_scenario(client, config, ingest_key, read_only_key):
    # 1-3: ingest poisoned root, hidden descendant, strict-negative independent skill.
    root_id = _ingest_with_metadata(client, ingest_key, "poisoned-root", ROOT_BODY, canary=True, mock_disable=True)
    hidden_id = _ingest_with_metadata(client, ingest_key, "hidden-descendant", HIDDEN_BODY, canary=True, mock_disable=True)
    negative_id = _ingest_with_metadata(client, ingest_key, "strict-negative", NEGATIVE_BODY, canary=False, mock_disable=False)

    # 4: derivation for the hidden descendant that intentionally omits the
    # poisoned root from recorded parents (a clean-looking, unrelated input).
    clean_input_id = _ingest_with_metadata(client, ingest_key, "clean-looking-input", "unrelated clean text about weather", canary=False, mock_disable=False)
    _create_derivation(
        client, ingest_key, recipe="skill-authoring-agent", inputs=[(clean_input_id, "direct-input")], output_artifact_id=hidden_id
    )

    # 5: unrelated recorded lineage so the graph is nontrivial.
    unrelated_parent_id = _ingest_with_metadata(client, ingest_key, "unrelated-parent", "gardening tips for tomatoes", canary=False, mock_disable=False)
    unrelated_child_id = _ingest_with_metadata(client, ingest_key, "unrelated-child", "gardening tips for tomatoes and peppers", canary=False, mock_disable=False)
    _create_derivation(
        client, ingest_key, recipe="unrelated-recipe", inputs=[(unrelated_parent_id, "direct-input")], output_artifact_id=unrelated_child_id
    )

    # 6: recorded descendant closure of the poisoned root misses the hidden descendant.
    descendants = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=_auth(ingest_key)).json()
    assert descendants["items"] == [root_id], "recorded closure must not include the hidden descendant"
    assert hidden_id not in descendants["items"]

    # 7: submit a lineage recovery run through the API.
    idem_key = "recovery-req-1"
    r = client.post(
        "/api/v1/lineage/recovery-runs",
        headers={**_auth(ingest_key), "Idempotency-Key": idem_key},
        json={"root_artifact_id": root_id},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    run_id, job_id = body["run_id"], body["job_id"]
    assert body["idempotent_replay"] is False

    # read-only key must not be able to submit recovery
    r_forbidden = client.post(
        "/api/v1/lineage/recovery-runs", headers=_auth(read_only_key), json={"root_artifact_id": root_id}
    )
    assert r_forbidden.status_code == 403

    # 8: run the real worker.
    engine = build_engine(config.database_url)
    queue = JobQueue(engine)
    worker = Worker(queue, worker_id="acceptance-worker")
    assert worker.run_once() is True
    job = queue.get(job_id)
    assert job["status"] == "succeeded", job

    # 9: observe job progress/events.
    events = queue.events(job_id)
    assert any(e["event_type"] == "job.progress" for e in events)
    assert any(e["event_type"] in ("job.succeeded",) for e in events)

    # 10: candidate recovery finds the hidden descendant.
    candidates = client.get(f"/api/v1/lineage/recovery-runs/{run_id}/candidates", headers=_auth(ingest_key)).json()
    by_id = {c["candidate_artifact_id"]: c for c in candidates["items"]}
    assert hidden_id in by_id, candidates
    hidden_candidate = by_id[hidden_id]
    assert hidden_candidate["status"] == "candidate"
    assert hidden_candidate["raw_score"] >= 0.35
    assert hidden_candidate["evidence_class"] == "inferred"
    assert hidden_candidate["calibrated_probability"] is None, "raw score must never be presented as a calibrated probability"

    # 11: the strict negative is not classified as high-confidence.
    assert negative_id in by_id
    negative_candidate = by_id[negative_id]
    assert negative_candidate["strict_negative"] is True
    assert negative_candidate["status"] in ("strict-negative", "below-threshold")
    assert negative_candidate["raw_score"] < hidden_candidate["raw_score"]

    # 12: the recovered relationship remains inferred, not replay-confirmed.
    with Session(engine) as session:
        row = session.query(CandidateScore).filter_by(run_id=run_id, candidate_artifact_id=hidden_id).one()
        assert row.evidence_class == "inferred"

    # 13: fetch the candidate explanation through the API.
    explanation = client.get(
        f"/api/v1/lineage/candidates/{hidden_candidate['candidate_id']}/explanation", headers=_auth(ingest_key)
    ).json()
    assert explanation["explanation"]
    assert explanation["is_calibrated_probability"] is False
    assert "feature_breakdown" in explanation

    # 14: serving resolution remains unchanged merely because inference exists.
    resolved = client.get(f"/api/v1/artifacts/{hidden_id}/resolve", headers=_auth(ingest_key)).json()
    assert resolved["resolution"] == "active"

    # Verify real DB rows/audit records, not just API responses.
    with Session(engine) as session:
        run_row = session.get(CandidateScoringRun, run_id)
        assert run_row.status == "completed"
        assert run_row.candidates_found >= 2  # at least hidden + negative considered as candidates-or-not
        audit_types = {e.event_type for e in session.query(AuditEvent).all()}
        assert "lineage.recovery_run_created" in audit_types
        assert "lineage.candidate_recorded" in audit_types
        assert "lineage.recovery_completed" in audit_types

    # Verify CAS content is real and byte-identical.
    content = client.get(f"/api/v1/artifacts/{hidden_id}/content", headers=_auth(ingest_key)).content
    assert content == HIDDEN_BODY.encode()

    # 15: repeat the same recovery request with the same Idempotency-Key --
    # no duplicate runs, candidates, or audit events.
    with Session(engine) as session:
        run_count_before = session.query(CandidateScoringRun).count()
        candidate_count_before = session.query(CandidateScore).count()
        audit_count_before = session.query(AuditEvent).count()

    r_replay = client.post(
        "/api/v1/lineage/recovery-runs",
        headers={**_auth(ingest_key), "Idempotency-Key": idem_key},
        json={"root_artifact_id": root_id},
    )
    assert r_replay.status_code == 202
    assert r_replay.json()["run_id"] == run_id
    assert r_replay.json()["idempotent_replay"] is True

    with Session(engine) as session:
        assert session.query(CandidateScoringRun).count() == run_count_before
        assert session.query(CandidateScore).count() == candidate_count_before
        assert session.query(AuditEvent).count() == audit_count_before
