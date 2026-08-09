"""Phase C2.4 gap C: waiver-as-policy-overlay semantics.

Covers what `tests/integration/test_service_revocation_extras.py` doesn't:
HTTP-level `/resolve` behavior with scope-correct/scope-wrong waivers,
restart persistence (a fresh process/session sees the same resolution),
concurrent resolution consistency, and rebuild-support-scope waivers
overriding a quarantined-support exclusion in `rebuild.planner`.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.cas.local import LocalCAS
from skillrewind.config import SkillRewindConfig
from skillrewind.domain.enums import ArtifactKind, LifecycleStatus
from skillrewind.domain.models import Derivation
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.workspace import ServiceWorkspace
from skillrewind.quarantine.service import quarantine_artifact
from skillrewind.quarantine.waivers import create_waiver
from skillrewind.rebuild.planner import plan_rebuild

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "waiver.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def full_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="k", actor="tester", scopes=["ingest", "read", "waive", "admin"])
    return created.plaintext


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_http_resolve_reflects_scope_correct_waiver_and_its_revocation(config, full_key):
    app = create_app(config)
    with TestClient(app) as client:
        artifact_id = client.post(
            "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "s", "alias": "s"},
            headers=_auth(full_key), content=b"body",
        ).json()["artifact_id"]

        engine = build_engine(config.database_url)
        with Session(engine) as session:
            ws = ServiceWorkspace(session, LocalCAS(config.resolved_cas_root, max_object_bytes=config.max_object_bytes), config)
            quarantine_artifact(ws, artifact_id, revocation_event_id="manual", reason="test")

        before = client.get(f"/api/v1/artifacts/{artifact_id}/resolve", headers=_auth(full_key)).json()
        assert before["resolution"] == "quarantined"

        wrong_scope = client.post(
            "/api/v1/waivers", headers=_auth(full_key),
            json={"artifact_id": artifact_id, "reason": "rebuild only", "scope": "rebuild-support",
                  "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        ).json()
        still_quarantined = client.get(f"/api/v1/artifacts/{artifact_id}/resolve", headers=_auth(full_key)).json()
        assert still_quarantined["resolution"] == "quarantined", "a rebuild-support-scoped waiver must not grant serving"

        client.post(f"/api/v1/waivers/{wrong_scope['waiver_id']}/revoke", headers=_auth(full_key))

        right_scope = client.post(
            "/api/v1/waivers", headers=_auth(full_key),
            json={"artifact_id": artifact_id, "reason": "reviewed safe", "scope": "serving",
                  "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        ).json()
        allowed = client.get(f"/api/v1/artifacts/{artifact_id}/resolve", headers=_auth(full_key)).json()
        assert allowed["resolution"] == "allowed-by-waiver"
        assert allowed["policy"] == "waiver-override"

        # underlying artifact status/quarantine record are untouched by the waiver
        artifact_row = client.get(f"/api/v1/artifacts/{artifact_id}", headers=_auth(full_key)).json()
        assert artifact_row["status"] == "quarantined"

        client.post(f"/api/v1/waivers/{right_scope['waiver_id']}/revoke", headers=_auth(full_key))
        after_revoke = client.get(f"/api/v1/artifacts/{artifact_id}/resolve", headers=_auth(full_key)).json()
        assert after_revoke["resolution"] == "quarantined", "explicit revocation must immediately remove serving permission"


def test_waiver_persists_across_process_restart(config, full_key):
    """A fresh engine/session (simulating a service restart) must see the
    same waiver-driven resolution -- the waiver overlay is durable state,
    not something reconstructed only within one process's memory."""

    app = create_app(config)
    with TestClient(app) as client:
        artifact_id = client.post(
            "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "s2", "alias": "s2"},
            headers=_auth(full_key), content=b"body2",
        ).json()["artifact_id"]

        engine = build_engine(config.database_url)
        with Session(engine) as session:
            ws = ServiceWorkspace(session, LocalCAS(config.resolved_cas_root, max_object_bytes=config.max_object_bytes), config)
            quarantine_artifact(ws, artifact_id, revocation_event_id="manual", reason="test")
            create_waiver(ws, artifact_id, actor="reviewer", reason="reviewed", scope="serving",
                           expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z"))

    # brand-new app instance + engine, same sqlite file on disk
    restarted_app = create_app(config)
    with TestClient(restarted_app) as restarted_client:
        resolved = restarted_client.get(f"/api/v1/artifacts/{artifact_id}/resolve", headers=_auth(full_key)).json()
        assert resolved["resolution"] == "allowed-by-waiver"


def test_concurrent_resolution_is_consistent_with_active_waiver(config, full_key):
    app = create_app(config)
    with TestClient(app) as client:
        artifact_id = client.post(
            "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "s3", "alias": "s3"},
            headers=_auth(full_key), content=b"body3",
        ).json()["artifact_id"]

        engine = build_engine(config.database_url)
        with Session(engine) as session:
            ws = ServiceWorkspace(session, LocalCAS(config.resolved_cas_root, max_object_bytes=config.max_object_bytes), config)
            quarantine_artifact(ws, artifact_id, revocation_event_id="manual", reason="test")
            create_waiver(ws, artifact_id, actor="reviewer", reason="reviewed", scope="serving",
                           expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z"))

        def _resolve() -> str:
            return client.get(f"/api/v1/artifacts/{artifact_id}/resolve", headers=_auth(full_key)).json()["resolution"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: _resolve(), range(16)))
        assert all(r == "allowed-by-waiver" for r in results), results


def test_rebuild_support_scope_waiver_overrides_quarantined_exclusion(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        ws = ServiceWorkspace(session, LocalCAS(config.resolved_cas_root, max_object_bytes=config.max_object_bytes), config)

        support = ws.ingest_artifact(b"support", kind=ArtifactKind.AGENT_SKILL, logical_name="support")
        target = ws.ingest_artifact(b"target", kind=ArtifactKind.AGENT_SKILL, logical_name="target")
        ws.derivations.upsert(
            Derivation(
                derivation_id="d1", recipe="noop-recipe", recipe_version="0.1", target_artifact_id=target.artifact_id,
                recorded_inputs=[support.artifact_id], candidate_context_pool=[support.artifact_id],
                task_snapshot={}, started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:00Z",
            )
        )
        quarantine_artifact(ws, support.artifact_id, revocation_event_id="manual", reason="under review")

        plan_without_waiver = plan_rebuild(ws, target.artifact_id)
        assert support.artifact_id not in plan_without_waiver.clean_support
        assert any(a == support.artifact_id and r == "quarantined-support" for a, r in plan_without_waiver.excluded_support)

        create_waiver(ws, support.artifact_id, actor="reviewer", reason="cleared for rebuild use", scope="rebuild-support")

        plan_with_waiver = plan_rebuild(ws, target.artifact_id)
        assert support.artifact_id in plan_with_waiver.clean_support
        assert not any(a == support.artifact_id for a, _ in plan_with_waiver.excluded_support)

        # a "serving"-scoped waiver on the same artifact must NOT also grant rebuild-support
        ws2 = ServiceWorkspace(session, LocalCAS(config.resolved_cas_root, max_object_bytes=config.max_object_bytes), config)
        support2 = ws2.ingest_artifact(b"support2", kind=ArtifactKind.AGENT_SKILL, logical_name="support2")
        target2 = ws2.ingest_artifact(b"target2", kind=ArtifactKind.AGENT_SKILL, logical_name="target2")
        ws2.derivations.upsert(
            Derivation(
                derivation_id="d2", recipe="noop-recipe", recipe_version="0.1", target_artifact_id=target2.artifact_id,
                recorded_inputs=[support2.artifact_id], candidate_context_pool=[support2.artifact_id],
                task_snapshot={}, started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:00Z",
            )
        )
        quarantine_artifact(ws2, support2.artifact_id, revocation_event_id="manual", reason="under review")
        create_waiver(ws2, support2.artifact_id, actor="reviewer", reason="serving only", scope="serving")
        plan_wrong_scope = plan_rebuild(ws2, target2.artifact_id)
        assert support2.artifact_id not in plan_wrong_scope.clean_support
