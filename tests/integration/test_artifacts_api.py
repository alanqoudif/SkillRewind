"""Artifact ingest/retrieval API tests (Phase C2). Verifies real DB rows and
real CAS bytes -- not just HTTP status codes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.models import Artifact

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
    import os

    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True
    )


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "api.db"
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


def test_ingest_writes_a_real_database_row_and_cas_object(client, config, ingest_key):
    body = b"skill body: deploys a fast HTTP service"
    r = client.post(
        "/api/v1/artifacts",
        params={"kind": "agent-skill", "logical_name": "fast-http", "mime_type": "text/plain"},
        headers=_auth(ingest_key),
        content=body,
    )
    assert r.status_code == 201
    payload = r.json()
    artifact_id = payload["artifact_id"]
    assert payload["byte_size"] == len(body)
    assert payload["status"] == "active"

    # Verify a real row landed in the database, not just an HTTP response.
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        row = session.get(Artifact, artifact_id)
        assert row is not None
        assert row.byte_size == len(body)
        assert row.digest_hex == payload["digest_hex"]

    # Verify the content is actually retrievable and byte-identical from CAS.
    r2 = client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=_auth(ingest_key))
    assert r2.status_code == 200
    assert r2.content == body


def test_reingesting_identical_content_is_idempotent_no_duplicate_row(client, config, ingest_key):
    body = b"identical content"
    params = {"kind": "agent-skill", "logical_name": "dup-skill"}
    r1 = client.post("/api/v1/artifacts", params=params, headers=_auth(ingest_key), content=body)
    r2 = client.post("/api/v1/artifacts", params=params, headers=_auth(ingest_key), content=body)
    assert r1.json()["artifact_id"] == r2.json()["artifact_id"]

    engine = build_engine(config.database_url)
    with Session(engine) as session:
        count = len(list(session.execute(select(Artifact).where(Artifact.logical_name == "dup-skill")).scalars()))
    assert count == 1


def test_idempotency_key_replays_same_response_without_reingesting(client, ingest_key):
    r1 = client.post(
        "/api/v1/artifacts",
        params={"kind": "agent-skill", "logical_name": "idem-skill"},
        headers={**_auth(ingest_key), "Idempotency-Key": "req-1"},
        content=b"payload-v1",
    )
    r2 = client.post(
        "/api/v1/artifacts",
        params={"kind": "agent-skill", "logical_name": "idem-skill"},
        headers={**_auth(ingest_key), "Idempotency-Key": "req-1"},
        content=b"payload-v1",
    )
    assert r1.json()["artifact_id"] == r2.json()["artifact_id"]

    conflict = client.post(
        "/api/v1/artifacts",
        params={"kind": "agent-skill", "logical_name": "idem-skill"},
        headers={**_auth(ingest_key), "Idempotency-Key": "req-1"},
        content=b"different-payload",
    )
    assert conflict.status_code == 409


def test_read_only_key_cannot_ingest(client, read_only_key):
    r = client.post(
        "/api/v1/artifacts",
        params={"kind": "agent-skill", "logical_name": "forbidden"},
        headers=_auth(read_only_key),
        content=b"x",
    )
    assert r.status_code == 403


def test_ingest_rejects_unknown_kind(client, ingest_key):
    r = client.post(
        "/api/v1/artifacts",
        params={"kind": "not-a-real-kind", "logical_name": "x"},
        headers=_auth(ingest_key),
        content=b"x",
    )
    assert r.status_code == 422


def test_get_unknown_artifact_returns_404(client, ingest_key):
    r = client.get("/api/v1/artifacts/skill://nope@sha256:" + "0" * 64, headers=_auth(ingest_key))
    assert r.status_code == 404


def test_list_artifacts_paginates_with_real_cursor(client, ingest_key):
    for i in range(5):
        client.post(
            "/api/v1/artifacts",
            params={"kind": "agent-skill", "logical_name": f"skill-{i}"},
            headers=_auth(ingest_key),
            content=f"body-{i}".encode(),
        )

    page1 = client.get("/api/v1/artifacts", params={"limit": 2}, headers=_auth(ingest_key)).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    page2 = client.get(
        "/api/v1/artifacts", params={"limit": 2, "cursor": page1["next_cursor"]}, headers=_auth(ingest_key)
    ).json()
    assert len(page2["items"]) == 2
    ids_page1 = {item["artifact_id"] for item in page1["items"]}
    ids_page2 = {item["artifact_id"] for item in page2["items"]}
    assert ids_page1.isdisjoint(ids_page2)


def test_ingest_rejects_oversized_content(client, ingest_key):
    from skillrewind.api.app import create_app as _create_app

    tiny_config = SkillRewindConfig(
        mode="service",
        database_url=client.app.state.config.database_url,
        cas_root=client.app.state.config.cas_root,
        max_object_bytes=4,
    )
    tiny_app = _create_app(tiny_config)
    with TestClient(tiny_app) as tiny_client:
        r = tiny_client.post(
            "/api/v1/artifacts",
            params={"kind": "agent-skill", "logical_name": "too-big"},
            headers=_auth(ingest_key),
            content=b"this is way more than 4 bytes",
        )
        assert r.status_code == 413
