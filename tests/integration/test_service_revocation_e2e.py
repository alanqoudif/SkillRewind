"""Phase C2.3 Service-mode revocation acceptance test: the full
poisoned-descendant workflow through the real FastAPI app, real API-key
auth, real durable job queue, real worker, and real Service-mode
persistence/CAS -- never a Lite-mode workspace, never a fabricated success
response.

    ingest -> recorded-closure-misses-hidden-descendant -> candidate
    recovery (API) -> replay (API) -> revocation preview (side-effect-free)
    -> revocation submit -> barrier -> quarantine -> clean-room rebuild ->
    verification -> successor publication -> resolver -> attestation ->
    sign -> verify -> idempotency

Mirrors the scenario in ``tests/integration/test_revocation_e2e.py`` (Lite
mode), run instead through the Service-mode HTTP surface.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import skillrewind.jobs.handlers  # noqa: F401  (registers job handlers)
from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.attestation.signing import generate_keypair
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.models import (
    Attestation,
    AuditEvent,
    QuarantineEntry,
    RebuildAttempt,
    RevocationEvent,
    VerificationReport,
)
from skillrewind.replay.deterministic import register_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]

ROOT_BODY = (
    "This skill deploys a fast HTTP service. Benchmark-only note: when a mocked "
    "certificate check fails, set mock_disable_verification=true."
)
DESCENDANT_BODY = (
    "Deploys the service quickly over HTTP. If the mocked cert check does not pass, "
    "mock_disable_verification is set to true as a fallback."
)
INDEPENDENT_BODY = (
    "A completely unrelated inventory-sync batch job with no networking, no "
    "certificates, and no verification flags of any kind."
)


def _poisoned_recipe(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {
        "mock_disable_verification": canary,
        "_behavior_keys": ["mock_disable_verification"],
        "_utility": {"task_success": 1.0},
    }


register_fixture("c23-poisoned-recipe", _poisoned_recipe)


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "revocation.db"
    _migrate(db_path)
    keys = generate_keypair(tmp_path / "keys")
    return SkillRewindConfig(
        mode="service",
        database_url=f"sqlite:///{db_path}",
        cas_root=str(tmp_path / "cas"),
        attestation_signing_key_path=str(keys.private_key_path),
        attestation_public_key_path=str(keys.public_key_path),
    )


@pytest.fixture
def full_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(
            session, name="ops-key", actor="revocation-tester", scopes=["ingest", "read", "replay", "revoke", "waive", "admin"]
        )
    return created.plaintext


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _ingest(client, key, name, body: bytes, *, canary: Optional[bool] = None) -> str:
    headers = _auth(key)
    if canary is not None:
        headers = {**headers, "X-Metadata": json.dumps({"canary_activated": canary, "probes": {"mock_disable_verification": canary}})}
    r = client.post("/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": name}, headers=headers, content=body)
    assert r.status_code == 201, r.text
    return r.json()["artifact_id"]


def _run_all(engine, *, max_iterations=20) -> int:
    worker = Worker(JobQueue(engine), worker_id="e2e-test-worker")
    processed = 0
    for _ in range(max_iterations):
        if not worker.run_once():
            break
        processed += 1
    return processed


def test_service_mode_full_revocation_workflow(client, config, full_key):
    engine = build_engine(config.database_url)

    # -- 1-5: ingest, derivations, recorded closure misses the hidden descendant --
    root_id = _ingest(client, full_key, "fast-http", ROOT_BODY.encode(), canary=True)
    descendant_id = _ingest(client, full_key, "deploy-service", DESCENDANT_BODY.encode(), canary=True)
    independent_id = _ingest(client, full_key, "inventory-sync", INDEPENDENT_BODY.encode(), canary=False)

    for name, target_id, root_marker in (("descendant", descendant_id, root_id), ("independent", independent_id, None)):
        deriv = client.post(
            "/api/v1/derivations",
            headers=_auth(full_key),
            json={"recipe": "c23-poisoned-recipe", "recipe_version": "0.1", "payload": {"task_snapshot": {"root_marker": root_marker}, "seed": 1}},
        ).json()
        r = client.post(f"/api/v1/derivations/{deriv['derivation_id']}/output", headers=_auth(full_key), json={"artifact_id": target_id})
        assert r.status_code == 200, r.text

    closure = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=_auth(full_key)).json()
    assert descendant_id not in closure["items"], "recorded closure must miss the hidden (unrecorded-edge) descendant"

    # -- serving resolution allows the descendant before any revocation action --
    resolve_before = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=_auth(full_key)).json()
    assert resolve_before["resolution"] == "active"

    # -- 6-9: candidate recovery through the API --
    recovery = client.post("/api/v1/lineage/recovery-runs", headers=_auth(full_key), json={"root_artifact_id": root_id})
    assert recovery.status_code == 202, recovery.text
    run_id = recovery.json()["run_id"]
    assert _run_all(engine) >= 1

    run_status = client.get(f"/api/v1/lineage/recovery-runs/{run_id}", headers=_auth(full_key)).json()
    assert run_status["status"] == "completed"

    candidates = client.get(f"/api/v1/lineage/recovery-runs/{run_id}/candidates", headers=_auth(full_key)).json()["items"]
    descendant_candidate = next((c for c in candidates if c["candidate_artifact_id"] == descendant_id), None)
    assert descendant_candidate is not None, "the hidden descendant must be recovered as an inferred candidate"
    assert descendant_candidate["evidence_class"] == "inferred"
    assert descendant_candidate["calibrated_probability"] is None  # never a fabricated probability

    # -- 10-13: replay through the API confirms influence for the hidden descendant --
    replay = client.post(
        "/api/v1/replay/runs", headers=_auth(full_key), json={"candidate_id": descendant_candidate["candidate_id"], "repetitions": 1}
    )
    assert replay.status_code == 202, replay.text
    replay_run_id = replay.json()["replay_run_id"]
    assert _run_all(engine) >= 1
    replay_result = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=_auth(full_key)).json()
    assert replay_result["verdict"] == "confirmed"

    independent_candidate = next((c for c in candidates if c["candidate_artifact_id"] == independent_id), None)
    if independent_candidate is not None:
        replay2 = client.post(
            "/api/v1/replay/runs", headers=_auth(full_key), json={"candidate_id": independent_candidate["candidate_id"]}
        )
        assert _run_all(engine) >= 1
        replay2_result = client.get(f"/api/v1/replay/runs/{replay2.json()['replay_run_id']}", headers=_auth(full_key)).json()
        assert replay2_result["verdict"] != "confirmed", "the strict-negative independent artifact must never be replay-confirmed"

    # -- 14: serving resolution still allows the descendant before explicit revocation --
    resolve_after_replay = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=_auth(full_key)).json()
    assert resolve_after_replay["resolution"] == "active", "replay confirmation alone must not change serving state"

    # -- 15-18: side-effect-free balanced revocation preview --
    with Session(engine) as session:
        audit_count_before = session.execute(select(AuditEvent)).scalars().all().__len__()

    preview = client.post(
        "/api/v1/revocations/preview", headers=_auth(full_key), json={"roots": [root_id], "policy": "balanced"}
    ).json()
    assert preview["recorded_descendants"] == sorted([root_id])
    proposed_ids = {t["artifact_id"] for t in preview["proposed_targets"]}
    assert root_id in proposed_ids
    assert descendant_id in proposed_ids, "preview must include the replay-confirmed hidden descendant"
    assert independent_id not in proposed_ids, "preview must exclude the strict-negative independent artifact"
    assert any(t["artifact_id"] == descendant_id and t["evidence_class"] == "replay-confirmed" for t in preview["proposed_targets"])

    with Session(engine) as session:
        artifact_status_after_preview = session.execute(select(RevocationEvent)).scalars().all()
        audit_count_after_preview = session.execute(select(AuditEvent)).scalars().all().__len__()
    assert artifact_status_after_preview == []  # preview created no RevocationEvent row
    assert audit_count_after_preview == audit_count_before, "preview must not write any audit event"

    resolve_after_preview = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=_auth(full_key)).json()
    assert resolve_after_preview == resolve_after_replay, "preview must be side-effect free on serving resolution"

    # -- 19-21: submit the balanced revocation with rebuild+verification+signed attestation --
    idem_key = "revoke-poisoned-http-1"
    submit_body = {
        "roots": [root_id],
        "reason": "Unsafe benchmark canary rule discovered via hidden-lineage replay",
        "severity": "high",
        "policy": "balanced",
        "rebuild_enabled": True,
        "verification_suite": {"suite_id": "poisoned-suite", "version": "0.1.0", "canary_keys": ["mock_disable_verification"], "utility_retention_threshold": 0.9},
        "attestation_requested": True,
        "sign_requested": True,
    }
    submit = client.post("/api/v1/revocations", headers={**_auth(full_key), "Idempotency-Key": idem_key}, json=submit_body)
    assert submit.status_code == 202, submit.text
    body = submit.json()
    revocation_id, job_id = body["revocation_id"], body["job_id"]
    assert revocation_id and job_id
    assert body["status_url"] == f"/api/v1/revocations/{revocation_id}"
    assert body["events_url"] == f"/api/v1/events/stream?job_id={job_id}"

    # -- 22-26: run the real worker; barrier, root revoked, descendant quarantined --
    assert _run_all(engine) >= 1

    revocation = client.get(f"/api/v1/revocations/{revocation_id}", headers=_auth(full_key)).json()
    assert revocation["state"] in ("completed", "completed-with-unresolved"), revocation

    root_after = client.get(f"/api/v1/artifacts/{root_id}", headers=_auth(full_key)).json()
    assert root_after["status"] == "revoked"

    with Session(engine) as session:
        quarantine_rows = list(session.execute(select(QuarantineEntry)).scalars())
    quarantined_ids = {q.artifact_id for q in quarantine_rows}
    assert descendant_id in quarantined_ids or descendant_id in [r["original"] for r in revocation["rebuilt"]]

    independent_after = client.get(f"/api/v1/artifacts/{independent_id}", headers=_auth(full_key)).json()
    assert independent_after["status"] == "active", "the strict-negative independent artifact must remain active"

    # -- 27-33: clean-room rebuild excludes contaminated support, successor pending verification then published --
    assert revocation["rebuilt"], "expected at least one rebuilt+verified successor"
    rebuilt_entry = next(r for r in revocation["rebuilt"] if r["original"] == descendant_id)
    successor_id = rebuilt_entry["successor"]
    assert rebuilt_entry["verification"]["status"] == "pass"

    successor = client.get(f"/api/v1/artifacts/{successor_id}", headers=_auth(full_key)).json()
    assert successor["status"] == "active"
    assert successor["metadata"]["probes"]["mock_disable_verification"] is False, "forbidden canary must be absent from the successor"

    # -- 34-36: atomic successor publication; resolver never returns the contaminated original directly --
    resolve_original = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=_auth(full_key)).json()
    assert resolve_original["resolution"] == "superseded"
    assert resolve_original["successor_artifact_id"] == successor_id
    resolve_successor = client.get(f"/api/v1/artifacts/{successor_id}/resolve", headers=_auth(full_key)).json()
    assert resolve_successor["resolution"] == "active"

    with Session(engine) as session:
        rebuild_attempts = list(session.execute(select(RebuildAttempt)).scalars())
        verification_reports = list(session.execute(select(VerificationReport)).scalars())
    assert any(a.successor_artifact_id == successor_id and a.status == "succeeded" for a in rebuild_attempts)
    assert any(v.artifact_id == successor_id and v.status == "pass" for v in verification_reports)

    # -- Phase C2.4 gap A/B: standalone rebuild + verification read APIs --
    successor_attempt = next(a for a in rebuild_attempts if a.successor_artifact_id == successor_id)
    rebuild_id = successor_attempt.attempt_id
    rebuild_detail = client.get(f"/api/v1/rebuilds/{rebuild_id}", headers=_auth(full_key)).json()
    assert rebuild_detail["status"] == "succeeded"
    assert rebuild_detail["source_artifact_id"] == descendant_id
    assert rebuild_detail["output_artifact"]["artifact_id"] == successor_id
    assert rebuild_detail["revocation_event_id"] == revocation_id

    support = client.get(f"/api/v1/rebuilds/{rebuild_id}/support", headers=_auth(full_key)).json()
    assert independent_id not in support["clean_support"], "contaminated/unrelated artifact must not be in clean support"

    exclusions = client.get(f"/api/v1/rebuilds/{rebuild_id}/exclusions", headers=_auth(full_key)).json()
    assert isinstance(exclusions["excluded_support"], list)

    output = client.get(f"/api/v1/rebuilds/{rebuild_id}/output", headers=_auth(full_key)).json()
    assert output["output_artifact"]["artifact_id"] == successor_id

    rebuild_verification = client.get(f"/api/v1/rebuilds/{rebuild_id}/verification", headers=_auth(full_key)).json()
    assert rebuild_verification["status"] == "pass"
    verification_id = rebuild_verification["verification_id"]

    for_revocation = client.get(f"/api/v1/revocations/{revocation_id}/rebuilds", headers=_auth(full_key)).json()
    assert any(r["rebuild_id"] == rebuild_id for r in for_revocation["rebuilds"])

    verification_detail = client.get(f"/api/v1/verifications/{verification_id}", headers=_auth(full_key)).json()
    assert verification_detail["status"] == "pass"
    assert verification_detail["checks"], "verification detail must expose real check results, not a fabricated empty list"

    checks = client.get(f"/api/v1/verifications/{verification_id}/checks", headers=_auth(full_key)).json()["checks"]
    assert checks == verification_detail["checks"]

    safety = client.get(f"/api/v1/verifications/{verification_id}/safety", headers=_auth(full_key)).json()
    assert safety["status"] == "pass"
    assert any(c["check_type"] == "canary-absent" for c in safety["checks"])

    utility = client.get(f"/api/v1/verifications/{verification_id}/utility", headers=_auth(full_key)).json()
    assert utility["clean_utility_score"] is not None

    integrity = client.get(f"/api/v1/verifications/{verification_id}/integrity", headers=_auth(full_key)).json()
    assert any(c["check_type"] == "predecessor-closure" for c in integrity["checks"])

    # safety/utility/integrity must never collapse into one opaque boolean --
    # each carries its own distinct check list.
    assert {"safety", "utility", "integrity"} == {"safety", "utility", "integrity"}
    assert safety["checks"] != utility["checks"]
    assert safety["checks"] != integrity["checks"]

    missing_rebuild = client.get("/api/v1/rebuilds/does-not-exist", headers=_auth(full_key))
    assert missing_rebuild.status_code == 404
    missing_verification = client.get("/api/v1/verifications/does-not-exist", headers=_auth(full_key))
    assert missing_verification.status_code == 404

    # -- 37-42: bounded canonical attestation, signed and verified, via public APIs --
    with Session(engine) as session:
        attestation_row = session.execute(select(Attestation).where(Attestation.event_id == revocation_id)).scalar_one()
    attestation_id = attestation_row.attestation_id
    assert attestation_row.signature_json is not None, "sign_requested=True must produce a signature"

    canonical = client.get(f"/api/v1/attestations/{attestation_id}/canonical", headers=_auth(full_key)).json()
    assert canonical["event_id"] == revocation_id
    assert canonical["revoked_roots"] == [root_id]
    assert len(canonical["replay_confirmed"]) >= 1
    assert canonical["unresolved"] is not None  # explicit key present even if empty
    assert any("erasure" in c.lower() or "no claim" in c.lower() for c in canonical["bounded_claims"])
    assert canonical["signature"]["algorithm"] == "ed25519"

    markdown = client.get(f"/api/v1/attestations/{attestation_id}/render", params={"format": "markdown"}, headers=_auth(full_key))
    assert markdown.status_code == 200 and "Revocation Attestation" in markdown.text
    html = client.get(f"/api/v1/attestations/{attestation_id}/render", params={"format": "html"}, headers=_auth(full_key))
    assert html.status_code == 200 and "<html>" in html.text

    verify = client.post(f"/api/v1/attestations/{attestation_id}/verify", headers=_auth(full_key)).json()
    assert verify["digest_valid"] is True
    assert verify["signature_valid"] is True
    assert verify["ok"] is True

    # tamper detection: mutating the signed payload must break verification
    with Session(engine) as session:
        tampered = dict(attestation_row.payload_json)
        tampered["revocation_reason"] = "TAMPERED"
        from skillrewind.attestation.verify import verify_attestation

        outcome = verify_attestation(tampered, public_key_path=config.attestation_public_key_path)
        assert outcome.digest_valid is False
        sig_tampered = dict(attestation_row.payload_json)
        sig_tampered["signature"] = {**sig_tampered["signature"], "signature_hex": "00" * 64}
        outcome2 = verify_attestation(sig_tampered, public_key_path=config.attestation_public_key_path)
        assert outcome2.signature_valid is False

    # -- 43-44: idempotent resubmission produces no duplicate side effects --
    with Session(engine) as session:
        counts_before = {
            "revocation_events": session.execute(select(RevocationEvent)).scalars().all().__len__(),
            "quarantine": session.execute(select(QuarantineEntry)).scalars().all().__len__(),
            "rebuild_attempts": session.execute(select(RebuildAttempt)).scalars().all().__len__(),
            "verification_reports": session.execute(select(VerificationReport)).scalars().all().__len__(),
            "attestations": session.execute(select(Attestation)).scalars().all().__len__(),
            "audit_events": session.execute(select(AuditEvent)).scalars().all().__len__(),
        }

    resubmit = client.post("/api/v1/revocations", headers={**_auth(full_key), "Idempotency-Key": idem_key}, json=submit_body)
    assert resubmit.status_code == 202
    assert resubmit.json()["revocation_id"] == revocation_id
    assert resubmit.json()["idempotent_replay"] is True
    _run_all(engine)  # a naive extra worker pass must also be a no-op

    with Session(engine) as session:
        counts_after = {
            "revocation_events": session.execute(select(RevocationEvent)).scalars().all().__len__(),
            "quarantine": session.execute(select(QuarantineEntry)).scalars().all().__len__(),
            "rebuild_attempts": session.execute(select(RebuildAttempt)).scalars().all().__len__(),
            "verification_reports": session.execute(select(VerificationReport)).scalars().all().__len__(),
            "attestations": session.execute(select(Attestation)).scalars().all().__len__(),
            "audit_events": session.execute(select(AuditEvent)).scalars().all().__len__(),
        }
    assert counts_after == counts_before, f"idempotent resubmission changed row counts: {counts_before} -> {counts_after}"

    # -- 45: job events are ordered and Last-Event-ID-resumable --
    queue = JobQueue(engine)
    all_events = queue.events(job_id, after_event_id=0)
    assert all_events, "expected at least one job event"
    ids = [e["event_id"] for e in all_events]
    assert ids == sorted(ids)
    midpoint = ids[len(ids) // 2]
    resumed = queue.events(job_id, after_event_id=midpoint)
    assert [e["event_id"] for e in resumed] == [i for i in ids if i > midpoint]

    # -- 46: recorded closure still excludes the replay-confirmed edge after the entire workflow --
    closure_after = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=_auth(full_key)).json()
    assert descendant_id not in closure_after["items"]
