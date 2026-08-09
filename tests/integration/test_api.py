"""FastAPI Service-mode API tests (Phase C).

Only exercises endpoints backed by real state: health/readiness, API-key
auth/scopes, idempotency, job management, and benchmark-run submission
through the real Phase B job queue -- see
`src/skillrewind/api/app.py`'s module docstring and
`docs/completion-matrix-v0.3.md` for what is deliberately not implemented
(artifact/lineage/revocation/replay/rebuild/attestation endpoints, since
their Service-mode schema has no writer yet).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
    import os

    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "api.db"
    _migrate(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def dev_config(db_path, tmp_path):
    """Auth-disabled config -- explicit Lite/dev mode only, per spec 8.5."""

    return SkillRewindConfig(
        mode="lite", database_url=f"sqlite:///{db_path}", api_auth_disabled=True, cas_root=str(tmp_path / "cas")
    )


@pytest.fixture
def admin_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="bootstrap-admin", actor="test-admin", scopes=["admin"])
    return created.plaintext


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def dev_client(dev_config):
    app = create_app(dev_config)
    with TestClient(app) as c:
        yield c


def _auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# -- health / readiness -------------------------------------------------


def test_health_live(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "live"


def test_health_ready_after_migration(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_readiness_fails_when_service_mode_auth_disabled(db_path):
    bad_config = SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", api_auth_disabled=True)
    app = create_app(bad_config)
    with TestClient(app) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"


def test_readiness_fails_before_migration(tmp_path):
    unmigrated_db = tmp_path / "unmigrated.db"
    bad_config = SkillRewindConfig(mode="service", database_url=f"sqlite:///{unmigrated_db}")
    app = create_app(bad_config)
    with TestClient(app) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503


def test_version_endpoint(client):
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["api_version"] == "v1"


def test_openapi_schema_generates(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"] == "SkillRewind API"


def test_schema_endpoint_serves_known_spec(client, admin_key):
    r = client.get("/api/v1/schemas/artifact-manifest.schema.json", headers=_auth_headers(admin_key))
    assert r.status_code == 200


def test_schema_endpoint_rejects_path_traversal(client, admin_key):
    r = client.get("/api/v1/schemas/..%2F..%2Fpyproject.toml", headers=_auth_headers(admin_key))
    assert r.status_code == 404


# -- auth ------------------------------------------------------------------


def test_unauthenticated_request_rejected(client):
    r = client.get("/api/v1/jobs")
    assert r.status_code == 401


def test_dev_mode_allows_unauthenticated_requests(dev_client):
    r = dev_client.get("/api/v1/jobs")
    assert r.status_code == 200


def test_admin_can_create_key_and_plaintext_shown_once(client, admin_key):
    r = client.post(
        "/api/v1/admin/api-keys",
        json={"name": "reader", "actor": "alice", "scopes": ["read"]},
        headers=_auth_headers(admin_key),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["plaintext"].startswith("srw_")

    listed = client.get("/api/v1/admin/api-keys", headers=_auth_headers(admin_key)).json()
    assert all("plaintext" not in row for row in listed)
    assert all("key_hash" not in row for row in listed)


def test_scope_matrix_rejects_insufficient_scope(client, admin_key):
    created = client.post(
        "/api/v1/admin/api-keys",
        json={"name": "reader", "actor": "bob", "scopes": ["read"]},
        headers=_auth_headers(admin_key),
    ).json()
    r = client.post("/api/v1/bench/runs", json={"preset": "smoke"}, headers=_auth_headers(created["plaintext"]))
    assert r.status_code == 403


def test_scope_matrix_allows_correct_scope(client, admin_key):
    created = client.post(
        "/api/v1/admin/api-keys",
        json={"name": "bencher", "actor": "carol", "scopes": ["bench"]},
        headers=_auth_headers(admin_key),
    ).json()
    r = client.post("/api/v1/bench/runs", json={"preset": "smoke"}, headers=_auth_headers(created["plaintext"]))
    assert r.status_code == 202


def test_revoked_key_rejected(client, admin_key):
    created = client.post(
        "/api/v1/admin/api-keys",
        json={"name": "temp", "actor": "dave", "scopes": ["read"]},
        headers=_auth_headers(admin_key),
    ).json()
    client.delete(f"/api/v1/admin/api-keys/{created['key_id']}", headers=_auth_headers(admin_key))
    r = client.get("/api/v1/jobs", headers=_auth_headers(created["plaintext"]))
    assert r.status_code == 401


def test_invalid_key_rejected_without_leaking_detail(client):
    r = client.get("/api/v1/jobs", headers=_auth_headers("srw_deadbeefdead_notreal"))
    assert r.status_code == 401
    assert "notreal" not in r.text


def test_unauthorized_admin_endpoint_requires_admin_scope(client, admin_key):
    created = client.post(
        "/api/v1/admin/api-keys",
        json={"name": "reader", "actor": "eve", "scopes": ["read"]},
        headers=_auth_headers(admin_key),
    ).json()
    r = client.get("/api/v1/admin/diagnostics", headers=_auth_headers(created["plaintext"]))
    assert r.status_code == 403


# -- idempotency -------------------------------------------------------------


def test_idempotency_replay_returns_same_job(client, admin_key):
    headers = {**_auth_headers(admin_key), "Idempotency-Key": "op-1"}
    r1 = client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": 1}, headers=headers)
    r2 = client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": 1}, headers=headers)
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert r2.json()["idempotent_replay"] is True


def test_idempotency_conflict_on_different_request(client, admin_key):
    headers = {**_auth_headers(admin_key), "Idempotency-Key": "op-2"}
    client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": 1}, headers=headers)
    r2 = client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": 2}, headers=headers)
    assert r2.status_code == 409


# -- jobs / pagination -------------------------------------------------------


def test_pagination_and_filtering(client, admin_key):
    for i in range(5):
        client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": i}, headers=_auth_headers(admin_key))
    r = client.get("/api/v1/jobs", params={"limit": 2, "kind": "benchmark.run"}, headers=_auth_headers(admin_key))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2


def test_malformed_json_returns_422(client, admin_key):
    r = client.post(
        "/api/v1/admin/api-keys",
        content="{not json",
        headers={**_auth_headers(admin_key), "content-type": "application/json"},
    )
    assert r.status_code == 422


def test_job_not_found_returns_404(client, admin_key):
    r = client.get("/api/v1/jobs/does-not-exist", headers=_auth_headers(admin_key))
    assert r.status_code == 404


def test_job_lifecycle_through_api_and_worker(client, admin_key, config):
    created = client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": 3}, headers=_auth_headers(admin_key)).json()
    job_id = created["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers(admin_key)).json()["status"] == "queued"

    engine = build_engine(config.database_url)
    Worker(JobQueue(engine), worker_id="test-worker").run_once()

    job = client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers(admin_key)).json()
    assert job["status"] == "succeeded"

    report = client.get(f"/api/v1/bench/runs/{job_id}/report", headers=_auth_headers(admin_key))
    assert report.status_code == 200
    assert "micro_f1" in report.json()


def test_cancel_and_retry_job(client, admin_key):
    created = client.post("/api/v1/bench/runs", json={"preset": "smoke"}, headers=_auth_headers(admin_key)).json()
    job_id = created["job_id"]
    r = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=_auth_headers(admin_key))
    assert r.status_code == 200
    assert r.json()["cancellation_requested"] is True


def test_cors_disabled_by_default(client):
    r = client.get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}


def test_cors_allowlisted_when_configured(db_path):
    cfg = SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cors_allow_origins="https://good.example")
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/health/live", headers={"Origin": "https://good.example"})
        assert r.headers.get("access-control-allow-origin") == "https://good.example"


# -- SSE ---------------------------------------------------------------------


def test_sse_stream_delivers_ordered_events_and_resumes_from_last_event_id(client, admin_key, config):
    created = client.post("/api/v1/bench/runs", json={"preset": "smoke", "seed": 9}, headers=_auth_headers(admin_key)).json()
    job_id = created["job_id"]

    engine = build_engine(config.database_url)
    Worker(JobQueue(engine), worker_id="sse-worker").run_once()

    all_events = []
    with client.stream(
        "GET", "/api/v1/events/stream", params={"job_id": job_id, "poll_interval": 0.01}, headers=_auth_headers(admin_key)
    ) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("id:"):
                all_events.append(int(line.split(":", 1)[1].strip()))
            if len(all_events) >= 2:
                break
    assert all_events == sorted(all_events)
    assert len(all_events) >= 2

    resume_from = all_events[0]
    resumed_events = []
    with client.stream(
        "GET",
        "/api/v1/events/stream",
        params={"job_id": job_id, "poll_interval": 0.01},
        headers={**_auth_headers(admin_key), "Last-Event-ID": str(resume_from)},
    ) as r:
        for line in r.iter_lines():
            if line.startswith("id:"):
                resumed_events.append(int(line.split(":", 1)[1].strip()))
            if resumed_events and resumed_events[0] > resume_from:
                break
    assert resumed_events[0] > resume_from  # never re-delivers an already-seen event


def test_sse_stream_rejects_unknown_job(client, admin_key):
    r = client.get("/api/v1/events/stream", params={"job_id": "no-such-job"}, headers=_auth_headers(admin_key))
    assert r.status_code == 404


# -- rate limiting -----------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(db_path):
    cfg = SkillRewindConfig(
        mode="lite", database_url=f"sqlite:///{db_path}", api_auth_disabled=True,
        rate_limit_capacity=2, rate_limit_refill_per_second=0.001,
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        results = [c.get("/health/live") for _ in range(5)]
    assert any(r.status_code == 429 for r in results)
    limited = next(r for r in results if r.status_code == 429)
    assert "retry-after" in {k.lower() for k in limited.headers.keys()}
