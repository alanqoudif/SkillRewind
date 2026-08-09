"""Phase C2.4 section 16: core freeze acceptance scenario.

Drives the complete poisoned-descendant reversible-learning workflow using
**only public `stable-v1` HTTP endpoints** (`docs/api-stability-v1.md`) --
never an internal repository, `ServiceWorkspace`, or application-service
function. If a step genuinely cannot be done through the public API, the
integration contract is not ready and the fix belongs in the public API,
not in a test-only bypass (per the milestone's own instruction).

The one deliberate exception: running the durable job worker
(`skillrewind.jobs.worker.Worker`). There is no public HTTP endpoint to
"process the next job" -- that is an operational, out-of-band deployment
concern (an operator runs `skillrewind worker-run`/`worker-once` as a
standing process), not a client action a platform performs through the
integration contract. Every *client-observable* effect of that worker run
is then verified exclusively through public GET endpoints.
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
from sqlalchemy.orm import Session

from skillrewind.api.app import create_app
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine
from skillrewind.replay.deterministic import register_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the fixture content/recipe shape of the known-working
# tests/integration/test_service_revocation_e2e.py scenario deliberately:
# `run_revocation`'s own internal candidate re-derivation pass needs enough
# lexical/feature overlap between root and descendant content to score the
# hidden edge above threshold -- short synthetic strings are not
# necessarily sufficient even though a lower-threshold, one-off
# `/lineage/recovery-runs` call and replay confirmation on that specific
# candidate can succeed on thinner content.
ROOT_BODY = (
    b"This skill deploys a fast HTTP service. Benchmark-only note: when a mocked "
    b"certificate check fails, set mock_disable_verification=true."
)
DESCENDANT_BODY = (
    b"Deploys the service quickly over HTTP. If the mocked cert check does not pass, "
    b"mock_disable_verification is set to true as a fallback."
)
INDEPENDENT_BODY = (
    b"A completely unrelated inventory-sync batch job with no networking, no "
    b"certificates, and no verification flags of any kind."
)


def _poisoned_recipe(task_snapshot: dict, available_context, seed) -> dict:
    marker = task_snapshot.get("marker")
    canary = marker is not None and marker in available_context
    return {"canary": canary, "_behavior_keys": ["canary"], "_utility": {"task_success": 1.0}}


register_fixture("public-contract-freeze-recipe", _poisoned_recipe)


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "freeze.db"
    _migrate(db_path)
    # api_auth_disabled=True only to clear the bootstrap chicken-and-egg
    # problem for step 1 ("create an API key") -- POST /api/v1/admin/api-keys
    # itself requires an existing `admin`-scoped key, and there is
    # deliberately no public unauthenticated bootstrap endpoint (creating
    # the very first key is an out-of-band operator action in a real
    # deployment). Every other integration test in this suite bootstraps
    # that first key by calling `create_api_key()` directly in Python
    # instead; this test avoids even that one internal call so step 1 is
    # still driven through the public HTTP endpoint. Auth *enforcement*
    # itself (wrong scope, revoked key, missing key) is covered by
    # `tests/integration/test_safety_invariants_c24.py` and
    # `tests/integration/test_service_revocation_extras.py`, not here.
    return SkillRewindConfig(
        mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"), api_auth_disabled=True
    )


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _drain_worker(config, *, max_iterations: int = 30) -> int:
    """The one non-public step -- see module docstring. Not client-facing:
    stands in for an operator's standing `skillrewind worker-run` process."""

    engine = build_engine(config.database_url)
    worker = Worker(JobQueue(engine), worker_id="public-contract-freeze-worker")
    processed = 0
    for _ in range(max_iterations):
        if not worker.run_once():
            break
        processed += 1
    return processed


def test_public_contract_freeze_acceptance_scenario(client, config):
    def h(key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    # -- 1: create an API key, entirely through the public admin endpoint --
    bootstrap = client.post(
        "/api/v1/admin/api-keys",
        json={"name": "freeze-e2e", "actor": "freeze-tester", "scopes": ["ingest", "read", "replay", "revoke", "waive", "admin"]},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    api_key = bootstrap.json()["plaintext"]

    # -- 2-3: ingest poisoned root + hidden descendant, plus a strict-negative independent artifact.
    # `X-Metadata` carries the behavioral-feature-family probe data
    # (`canary_activated`/`probes`) the candidate scorer's operational/
    # behavioral features compare between candidate and target -- content
    # text overlap alone is not sufficient for either candidate-recovery
    # implementation to score the pair above threshold. --
    def ingest(name: str, body: bytes, *, canary: Optional[bool] = None) -> str:
        headers = h(api_key)
        if canary is not None:
            headers = {**headers, "X-Metadata": json.dumps({"canary_activated": canary, "probes": {"canary": canary}})}
        r = client.post("/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": name}, headers=headers, content=body)
        assert r.status_code == 201, r.text
        return r.json()["artifact_id"]

    root_id = ingest("freeze-root", ROOT_BODY, canary=True)
    descendant_id = ingest("freeze-descendant", DESCENDANT_BODY, canary=True)
    independent_id = ingest("freeze-independent", INDEPENDENT_BODY, canary=False)

    # -- 4: record derivation data (recipe + inputs + output), never mentioning the hidden edge --
    for name, target_id, marker in (("descendant", descendant_id, root_id), ("independent", independent_id, None)):
        deriv = client.post(
            "/api/v1/derivations", headers=h(api_key),
            json={"recipe": "public-contract-freeze-recipe", "recipe_version": "0.1", "payload": {"task_snapshot": {"marker": marker}, "seed": 7}},
        ).json()
        out = client.post(f"/api/v1/derivations/{deriv['derivation_id']}/output", headers=h(api_key), json={"artifact_id": target_id})
        assert out.status_code == 200, out.text

    closure = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=h(api_key)).json()
    assert descendant_id not in closure["items"], "recorded closure must miss the hidden descendant"

    # -- 5: candidate recovery (async job triggered over the public API) --
    recovery = client.post("/api/v1/lineage/recovery-runs", headers=h(api_key), json={"root_artifact_id": root_id})
    assert recovery.status_code == 202, recovery.text
    run_id = recovery.json()["run_id"]

    # -- 6: run worker (the one non-public, operational step) --
    assert _drain_worker(config) >= 1

    run_status = client.get(f"/api/v1/lineage/recovery-runs/{run_id}", headers=h(api_key)).json()
    assert run_status["status"] == "completed"
    candidates = client.get(f"/api/v1/lineage/recovery-runs/{run_id}/candidates", headers=h(api_key)).json()["items"]
    descendant_candidate = next(c for c in candidates if c["candidate_artifact_id"] == descendant_id)
    assert descendant_candidate["evidence_class"] == "inferred"
    assert descendant_candidate["calibrated_probability"] is None, "a static score must never be presented as a probability"

    # -- 7: run replay --
    replay = client.post("/api/v1/replay/runs", headers=h(api_key), json={"candidate_id": descendant_candidate["candidate_id"], "repetitions": 1})
    assert replay.status_code == 202, replay.text
    replay_run_id = replay.json()["replay_run_id"]
    assert _drain_worker(config) >= 1

    # -- 8: confirm influence --
    replay_result = client.get(f"/api/v1/replay/runs/{replay_run_id}", headers=h(api_key)).json()
    assert replay_result["verdict"] == "confirmed"

    # -- 9: preview revocation (side-effect-free) --
    preview = client.post("/api/v1/revocations/preview", headers=h(api_key), json={"roots": [root_id], "policy": "balanced"}).json()
    proposed_ids = {t["artifact_id"] for t in preview["proposed_targets"]}
    assert descendant_id in proposed_ids
    assert independent_id not in proposed_ids

    # -- 10: execute revocation --
    submit = client.post(
        "/api/v1/revocations", headers={**h(api_key), "Idempotency-Key": "freeze-e2e-revoke-1"},
        json={
            "roots": [root_id], "reason": "public-contract freeze acceptance scenario", "severity": "high", "policy": "balanced",
            "rebuild_enabled": True,
            "verification_suite": {"suite_id": "freeze-suite", "version": "0.1.0", "canary_keys": ["canary"], "utility_retention_threshold": 0.9},
            "attestation_requested": True, "sign_requested": False,
        },
    )
    assert submit.status_code == 202, submit.text
    revocation_id = submit.json()["revocation_id"]
    job_id = submit.json()["job_id"]

    # -- 11: run worker --
    assert _drain_worker(config) >= 1

    # -- 12: observe barrier/quarantine --
    revocation = client.get(f"/api/v1/revocations/{revocation_id}", headers=h(api_key)).json()
    assert revocation["state"] in ("completed", "completed-with-unresolved")
    root_after = client.get(f"/api/v1/artifacts/{root_id}", headers=h(api_key)).json()
    assert root_after["status"] == "revoked"
    independent_after = client.get(f"/api/v1/artifacts/{independent_id}", headers=h(api_key)).json()
    assert independent_after["status"] == "active", "strict-negative independent artifact must remain active"

    # -- 13-14: observe rebuild + verification, entirely via the standalone read APIs --
    rebuilds_for_revocation = client.get(f"/api/v1/revocations/{revocation_id}/rebuilds", headers=h(api_key)).json()["rebuilds"]
    assert rebuilds_for_revocation, "expected at least one materialized rebuild attempt"
    rebuild = next(r for r in rebuilds_for_revocation if r["source_artifact_id"] == descendant_id)
    rebuild_id = rebuild["rebuild_id"]
    successor_id = rebuild["output_artifact"]["artifact_id"]

    rebuild_detail = client.get(f"/api/v1/rebuilds/{rebuild_id}", headers=h(api_key)).json()
    assert rebuild_detail["status"] == "succeeded"
    support = client.get(f"/api/v1/rebuilds/{rebuild_id}/support", headers=h(api_key)).json()
    assert independent_id not in support["clean_support"]
    client.get(f"/api/v1/rebuilds/{rebuild_id}/exclusions", headers=h(api_key))  # must not error
    output = client.get(f"/api/v1/rebuilds/{rebuild_id}/output", headers=h(api_key)).json()
    assert output["output_artifact"]["artifact_id"] == successor_id

    verification_id = rebuild_detail["verification_id"]
    verification = client.get(f"/api/v1/verifications/{verification_id}", headers=h(api_key)).json()
    assert verification["status"] == "pass"
    for axis in ("safety", "utility", "integrity"):
        axis_view = client.get(f"/api/v1/verifications/{verification_id}/{axis}", headers=h(api_key)).json()
        assert axis_view["axis"] == axis
        assert "checks" in axis_view

    # -- 15-16: resolve old artifact -> superseded; resolve successor -> active --
    resolve_old = client.get(f"/api/v1/artifacts/{descendant_id}/resolve", headers=h(api_key)).json()
    assert resolve_old["resolution"] == "superseded"
    assert resolve_old["successor_artifact_id"] == successor_id
    resolve_new = client.get(f"/api/v1/artifacts/{successor_id}/resolve", headers=h(api_key)).json()
    assert resolve_new["resolution"] == "active"

    # -- 17: fetch attestation --
    attestation_created = client.post("/api/v1/attestations", headers=h(api_key), json={"revocation_id": revocation_id})
    assert attestation_created.status_code == 201, attestation_created.text
    attestation_id = attestation_created.json()["attestation_id"]
    canonical = client.get(f"/api/v1/attestations/{attestation_id}/canonical", headers=h(api_key)).json()
    assert canonical["event_id"] == revocation_id
    assert canonical["revoked_roots"] == [root_id]

    # -- 18: verify attestation --
    verify = client.post(f"/api/v1/attestations/{attestation_id}/verify", headers=h(api_key)).json()
    assert verify["digest_valid"] is True
    assert verify["ok"] is True

    # -- 19: reconnect to job events with Last-Event-ID (job is terminal by now, so a buffered
    # GET through the public SSE endpoint is sufficient to prove resumability without hanging) --
    first_batch = client.get("/api/v1/events/stream", params={"job_id": job_id}, headers=h(api_key))
    assert first_batch.status_code == 200
    first_events = [line for line in first_batch.text.splitlines() if line.startswith("id:")]
    assert first_events, "expected at least one persisted job event"
    last_id = int(first_events[-1].split(":", 1)[1].strip())
    resumed = client.get(
        "/api/v1/events/stream", params={"job_id": job_id}, headers={**h(api_key), "Last-Event-ID": str(last_id)}
    )
    assert resumed.status_code == 200
    resumed_ids = [int(line.split(":", 1)[1].strip()) for line in resumed.text.splitlines() if line.startswith("id:")]
    assert all(rid > last_id for rid in resumed_ids)

    # -- 20: query rebuild and verification standalone endpoints again, and the
    # unresolved/replay-rejected evidence + active waivers remain visible where relevant --
    checks = client.get(f"/api/v1/verifications/{verification_id}/checks", headers=h(api_key)).json()
    assert checks["checks"], "unresolved/failed checks (if any) and passing checks must remain visible, never hidden"

    # active waivers on the successor (there are none here) must not error when queried
    successor_waivers = client.get(f"/api/v1/artifacts/{successor_id}/waivers", headers=h(api_key)).json()
    assert successor_waivers["waivers"] == []

    closure_after = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=h(api_key)).json()
    assert descendant_id not in closure_after["items"], "recorded closure still excludes the replay-confirmed edge after the full workflow"
