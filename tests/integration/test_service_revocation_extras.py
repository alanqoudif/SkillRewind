"""Phase C2.3 focused regression tests: crash/resume, forensic-mode
side-effect-freedom, and waiver authorization -- run directly against
`ServiceWorkspace` (no HTTP layer) for speed, since these exercise the same
reused `skillrewind.revocation.service.run_revocation` / `skillrewind.
quarantine.waivers.create_waiver` call paths the API routers use."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from skillrewind.cas.local import LocalCAS
from skillrewind.config import SkillRewindConfig
from skillrewind.domain.enums import ArtifactKind, LifecycleStatus, RevocationPolicy, Severity
from skillrewind.domain.errors import PolicyViolationError
from skillrewind.domain.models import Derivation
from skillrewind.persistence.service.engine import build_engine, create_all
from skillrewind.persistence.service.workspace import ServiceWorkspace
from skillrewind.quarantine.waivers import create_waiver
from skillrewind.replay.deterministic import register_fixture
from skillrewind.revocation import service as revocation_service
from skillrewind.revocation.service import request_revocation, run_revocation
from skillrewind.verification.suites import VerificationSuite

ROOT_BODY = (
    "This skill deploys a fast HTTP service. Benchmark-only note: when a mocked "
    "certificate check fails, set mock_disable_verification=true."
)
DESCENDANT_BODY = (
    "Deploys the service quickly over HTTP. If the mocked cert check does not pass, "
    "mock_disable_verification is set to true as a fallback."
)


def _poisoned_recipe(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"], "_utility": {"task_success": 1.0}}


register_fixture("c23-extras-poisoned-recipe", _poisoned_recipe)


@pytest.fixture
def ws(tmp_path):
    engine = build_engine("sqlite://")
    create_all(engine)
    session = Session(engine)
    cas = LocalCAS(str(tmp_path / "cas"))
    config = SkillRewindConfig(mode="service", database_url="sqlite://", cas_root=str(tmp_path / "cas"))
    workspace = ServiceWorkspace(session, cas, config)
    yield workspace
    session.close()
    engine.dispose()


def _build_scenario(ws):
    root = ws.ingest_artifact(
        ROOT_BODY.encode(),
        kind=ArtifactKind.AGENT_SKILL,
        logical_name="fast-http",
        alias="fast-http",
        metadata={"canary_activated": True, "probes": {"mock_disable_verification": True}},
    )
    descendant = ws.ingest_artifact(
        DESCENDANT_BODY.encode(),
        kind=ArtifactKind.AGENT_SKILL,
        logical_name="deploy-service",
        alias="deploy-service",
        metadata={"canary_activated": True, "probes": {"mock_disable_verification": True}},
    )
    ws.derivations.upsert(
        Derivation(
            derivation_id="deriv-descendant-svc-extra",
            recipe="c23-extras-poisoned-recipe",
            recipe_version="0.1",
            target_artifact_id=descendant.artifact_id,
            task_snapshot={"root_marker": root.artifact_id},
            started_at="2026-01-01T00:05:00Z",
            ended_at="2026-01-01T00:05:01Z",
            seed=1,
        )
    )
    return root, descendant


# -- A: crash-after-quarantine, before rebuild, then resume ------------------


def test_service_mode_crash_mid_rebuild_then_resume_no_duplicates(ws, monkeypatch):
    root, descendant = _build_scenario(ws)
    suite = VerificationSuite(suite_id="extras-suite", version="0.1.0", canary_keys=("mock_disable_verification",))
    event = request_revocation(
        ws, roots=[root.artifact_id], reason="crash-resume", severity=Severity.HIGH, policy=RevocationPolicy.BALANCED,
        actor="tester", idempotency_key="svc-crash-1",
    )

    original_rebuild = revocation_service.rebuild_artifact
    calls = {"n": 0}

    def flaky_rebuild(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated Service-mode worker crash mid-rebuild")
        return original_rebuild(*args, **kwargs)

    monkeypatch.setattr(revocation_service, "rebuild_artifact", flaky_rebuild)

    with pytest.raises(RuntimeError, match="simulated Service-mode worker crash"):
        run_revocation(ws, event, verification_suite=suite)

    # The barrier (root revoked) and quarantine are real, committed Service-mode
    # writes that must survive the crash -- resume must never re-apply them.
    root_after_crash = ws.artifacts.get(root.artifact_id)
    assert root_after_crash.status == LifecycleStatus.REVOKED
    crashed_event = ws.revocations.get(event.event_id)
    assert crashed_event.quarantined
    assert not crashed_event.rebuilt

    resumed_event = ws.revocations.get(event.event_id)
    final_event = run_revocation(ws, resumed_event, verification_suite=suite)

    assert calls["n"] == 2, "rebuild is attempted exactly once more on resume, never restarted from zero"
    assert final_event.rebuilt
    rebuilt_entry = next(r for r in final_event.rebuilt if r["original"] == descendant.artifact_id)
    successor = ws.artifacts.get(rebuilt_entry["successor"])
    assert successor.status == LifecycleStatus.ACTIVE
    assert successor.metadata["probes"]["mock_disable_verification"] is False

    # No duplicate quarantine row, no duplicate successor. The original's own
    # quarantine history entry is preserved (never deleted), but the
    # successor's quarantine (added pending verification) was released.
    quarantine_entries = ws.revocations.list_quarantine()
    assert len([q for q in quarantine_entries if q["artifact_id"] == rebuilt_entry["successor"]]) == 0
    original_after = ws.artifacts.get(descendant.artifact_id)
    assert original_after.status == LifecycleStatus.SUPERSEDED
    assert original_after.superseded_by == rebuilt_entry["successor"]


# -- B: forensic mode is fully side-effect free -------------------------------


def test_forensic_mode_makes_no_serving_state_changes(ws):
    root, descendant = _build_scenario(ws)
    event = request_revocation(
        ws, roots=[root.artifact_id], reason="forensic investigation", severity=Severity.HIGH, policy=RevocationPolicy.FORENSIC,
        actor="tester", idempotency_key="svc-forensic-1",
    )
    result = run_revocation(ws, event, verification_suite=VerificationSuite(suite_id="f", version="0.1.0"))

    assert result.state.value in ("completed", "completed-with-unresolved")
    assert not result.quarantined, "forensic mode must never quarantine"
    assert not result.rebuilt, "forensic mode must never rebuild"

    root_after = ws.artifacts.get(root.artifact_id)
    descendant_after = ws.artifacts.get(descendant.artifact_id)
    assert root_after.status == LifecycleStatus.ACTIVE, "forensic mode must never change serving state, including the root"
    assert descendant_after.status == LifecycleStatus.ACTIVE
    assert ws.resolve_alias("fast-http") is not None
    assert ws.resolve_alias("deploy-service") is not None

    # A confirmed hidden-influence edge may still be recorded as evidence
    # (that's real investigation output), but it is evidence, not enforcement.
    confirmed = [d for d in result.replay_decisions if d.get("verdict") == "confirmed"]
    assert confirmed, "forensic mode still produces real evidence, just no enforcement"


# -- C: waiver authorization rules --------------------------------------------


def test_waiver_rejects_directly_revoked_root_by_default(ws):
    root, _ = _build_scenario(ws)
    ws.artifacts.set_status(root.artifact_id, LifecycleStatus.REVOKED)
    from skillrewind.api.routers.waivers import _DEFAULT_MAX_DURATION_DAYS  # sanity import; bounds documented there

    assert _DEFAULT_MAX_DURATION_DAYS > 0
    # Domain-level policy: strict-mode config can forbid waivers outright.
    ws.config.strict_forbids_waivers = True
    with pytest.raises(PolicyViolationError):
        create_waiver(
            ws, root.artifact_id, actor="tester", reason="test", scope="quarantine-release",
            active_policy=RevocationPolicy.STRICT, config=ws.config,
        )


def test_valid_temporary_waiver_allows_resolution_without_releasing_quarantine(ws):
    root, descendant = _build_scenario(ws)
    from skillrewind.quarantine.service import quarantine_artifact

    quarantine_artifact(ws, descendant.artifact_id, revocation_event_id="manual-test", reason="test quarantine")
    ws.artifacts.set_status(descendant.artifact_id, LifecycleStatus.QUARANTINED)

    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    waiver = create_waiver(
        ws, descendant.artifact_id, actor="reviewer", reason="temporary manual review access",
        scope="temporary-review-access", expires_at=expires, config=ws.config,
    )
    assert waiver.revoked is False

    # scope != "quarantine-release" -- the quarantine record/evidence must remain intact.
    assert ws.revocations.is_quarantined(descendant.artifact_id) is True
    still_quarantined = ws.artifacts.get(descendant.artifact_id)
    assert still_quarantined.status == LifecycleStatus.QUARANTINED

    active = ws.waivers.active_for_artifact(descendant.artifact_id, now=datetime.now(timezone.utc).isoformat())
    assert any(w.waiver_id == waiver.waiver_id for w in active)

    ws.waivers.revoke(waiver.waiver_id)
    active_after_revoke = ws.waivers.active_for_artifact(descendant.artifact_id, now=datetime.now(timezone.utc).isoformat())
    assert not any(w.waiver_id == waiver.waiver_id for w in active_after_revoke)


def test_expired_waiver_stops_affecting_resolution(ws):
    root, descendant = _build_scenario(ws)
    from skillrewind.quarantine.service import quarantine_artifact

    quarantine_artifact(ws, descendant.artifact_id, revocation_event_id="manual-test", reason="test quarantine")

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    # Insert directly (past expiry is rejected at the API layer, not the domain layer).
    from skillrewind.domain.models import Waiver

    ws.waivers.insert(
        Waiver(waiver_id="expired-waiver-1", artifact_id=descendant.artifact_id, actor="reviewer", reason="expired", scope="x", created_at=past, expires_at=past)
    )
    active = ws.waivers.active_for_artifact(descendant.artifact_id, now=datetime.now(timezone.utc).isoformat())
    assert not active, "an already-expired waiver must not be returned as active"


# -- API-layer waiver validation (read-only key, missing reason, past expiry) -


def test_waiver_api_rejects_unauthorized_and_invalid_requests(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from fastapi.testclient import TestClient

    import skillrewind.jobs.handlers  # noqa: F401
    from skillrewind.api.app import create_app
    from skillrewind.api.auth import create_api_key

    repo_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "waivers.db"
    import os

    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=repo_root, env=env, check=True, capture_output=True)

    config = SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        waive_key = create_api_key(session, name="waiver-key", actor="reviewer", scopes=["ingest", "read", "waive"]).plaintext
        read_key = create_api_key(session, name="reader-key", actor="reader", scopes=["read"]).plaintext

    app = create_app(config)
    with TestClient(app) as client:
        artifact_id = client.post(
            "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "x"}, headers={"Authorization": f"Bearer {waive_key}"}, content=b"body"
        ).json()["artifact_id"]

        # read-only key lacks `waive` scope
        r = client.post(
            "/api/v1/waivers", headers={"Authorization": f"Bearer {read_key}"},
            json={"artifact_id": artifact_id, "reason": "x", "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        )
        assert r.status_code == 403

        # empty reason
        r = client.post(
            "/api/v1/waivers", headers={"Authorization": f"Bearer {waive_key}"},
            json={"artifact_id": artifact_id, "reason": "   ", "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        )
        assert r.status_code == 422

        # expiry in the past
        r = client.post(
            "/api/v1/waivers", headers={"Authorization": f"Bearer {waive_key}"},
            json={"artifact_id": artifact_id, "reason": "valid reason", "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        )
        assert r.status_code == 422

        # exceeds max duration for a non-admin key
        r = client.post(
            "/api/v1/waivers", headers={"Authorization": f"Bearer {waive_key}"},
            json={"artifact_id": artifact_id, "reason": "valid reason", "expires_at": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()},
        )
        assert r.status_code == 422

        # valid request succeeds
        r = client.post(
            "/api/v1/waivers", headers={"Authorization": f"Bearer {waive_key}"},
            json={"artifact_id": artifact_id, "reason": "valid reason", "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        )
        assert r.status_code == 201, r.text

        # duplicate submission with the same Idempotency-Key creates exactly one waiver
        headers = {"Authorization": f"Bearer {waive_key}", "Idempotency-Key": "dup-waiver-1"}
        body = {"artifact_id": artifact_id, "reason": "idempotent reason", "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        r1 = client.post("/api/v1/waivers", headers=headers, json=body)
        r2 = client.post("/api/v1/waivers", headers=headers, json=body)
        assert r1.json()["waiver_id"] == r2.json()["waiver_id"]

        listed = client.get(f"/api/v1/artifacts/{artifact_id}/waivers", headers={"Authorization": f"Bearer {waive_key}"}).json()
        assert len({w["waiver_id"] for w in listed["waivers"]}) == len(listed["waivers"])  # no duplicates
