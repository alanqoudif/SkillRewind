"""Derivation capture + recorded lineage API tests (Phase C2.1).

Verifies real database rows -- not just HTTP status codes -- and the
critical invariant that recorded closure never silently includes inferred
edges (a variant of this bug was previously found in Lite mode)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.persistence.service.engine import build_engine
from skillrewind.persistence.service.models import AuditEvent, DerivationInput, InfluenceEdge

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
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


def _ingest(client, key, name, body):
    r = client.post(
        "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": name}, headers=_auth(key), content=body
    )
    assert r.status_code == 201, r.text
    return r.json()["artifact_id"]


def test_derivation_capture_creates_recorded_edges_visible_in_lineage(client, config, ingest_key):
    parent_id = _ingest(client, ingest_key, "parent-skill", b"parent body")
    child_id = _ingest(client, ingest_key, "child-skill", b"child body")

    r = client.post(
        "/api/v1/derivations",
        headers=_auth(ingest_key),
        json={"recipe": "skill-authoring-agent", "recipe_version": "1.0.0", "payload": {"model_id": "example-model"}},
    )
    assert r.status_code == 201, r.text
    derivation_id = r.json()["derivation_id"]

    r2 = client.post(
        f"/api/v1/derivations/{derivation_id}/inputs",
        headers=_auth(ingest_key),
        json={"inputs": [{"parent_artifact_id": parent_id, "relation": "direct-input"}]},
    )
    assert r2.status_code == 200, r2.text

    r3 = client.post(
        f"/api/v1/derivations/{derivation_id}/output", headers=_auth(ingest_key), json={"artifact_id": child_id}
    )
    assert r3.status_code == 200, r3.text

    # Real database rows, not just an HTTP response.
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        inputs = session.query(DerivationInput).filter_by(derivation_id=derivation_id).all()
        assert len(inputs) == 1 and inputs[0].parent_artifact_id == parent_id
        edge = (
            session.query(InfluenceEdge)
            .filter_by(source=parent_id, target=child_id, relation="direct-input")
            .one()
        )
        assert edge.evidence_class == "recorded"
        audit_types = {e.event_type for e in session.query(AuditEvent).all()}
        assert {"derivation.created", "derivation.input_added", "derivation.output_set", "lineage.edge_recorded"} <= audit_types

    parents = client.get(f"/api/v1/artifacts/{child_id}/parents", headers=_auth(ingest_key)).json()
    assert parents["items"] == [{"parent_artifact_id": parent_id, "relation": "direct-input"}]

    children = client.get(f"/api/v1/artifacts/{parent_id}/children", headers=_auth(ingest_key)).json()
    assert children["items"] == [{"child_artifact_id": child_id, "relation": "direct-input", "derivation_id": derivation_id}]

    descendants = client.get(f"/api/v1/lineage/{parent_id}/descendants", headers=_auth(ingest_key)).json()
    assert set(descendants["items"]) == {parent_id, child_id}
    assert descendants["evidence_class"] == "recorded"


def test_derivation_input_rejects_nonexistent_parent(client, ingest_key):
    r = client.post(
        "/api/v1/derivations", headers=_auth(ingest_key), json={"recipe": "r", "recipe_version": "1"}
    )
    derivation_id = r.json()["derivation_id"]
    r2 = client.post(
        f"/api/v1/derivations/{derivation_id}/inputs",
        headers=_auth(ingest_key),
        json={"inputs": [{"parent_artifact_id": "skill://ghost@sha256:" + "0" * 64, "relation": "direct-input"}]},
    )
    assert r2.status_code == 404


def test_derivation_input_rejects_unknown_relation(client, ingest_key):
    parent_id = _ingest(client, ingest_key, "p2", b"p2 body")
    r = client.post("/api/v1/derivations", headers=_auth(ingest_key), json={"recipe": "r", "recipe_version": "1"})
    derivation_id = r.json()["derivation_id"]
    r2 = client.post(
        f"/api/v1/derivations/{derivation_id}/inputs",
        headers=_auth(ingest_key),
        json={"inputs": [{"parent_artifact_id": parent_id, "relation": "not-a-real-relation"}]},
    )
    assert r2.status_code == 422


def test_derivation_output_rejects_self_edge(client, ingest_key):
    artifact_id = _ingest(client, ingest_key, "self-skill", b"self body")
    r = client.post("/api/v1/derivations", headers=_auth(ingest_key), json={"recipe": "r", "recipe_version": "1"})
    derivation_id = r.json()["derivation_id"]
    client.post(
        f"/api/v1/derivations/{derivation_id}/inputs",
        headers=_auth(ingest_key),
        json={"inputs": [{"parent_artifact_id": artifact_id, "relation": "direct-input"}]},
    )
    r2 = client.post(
        f"/api/v1/derivations/{derivation_id}/output", headers=_auth(ingest_key), json={"artifact_id": artifact_id}
    )
    assert r2.status_code == 422


def test_duplicate_derivation_input_edge_does_not_duplicate_row_or_audit_event(client, config, ingest_key):
    parent_id = _ingest(client, ingest_key, "dup-parent", b"dp body")
    r = client.post("/api/v1/derivations", headers=_auth(ingest_key), json={"recipe": "r", "recipe_version": "1"})
    derivation_id = r.json()["derivation_id"]
    payload = {"inputs": [{"parent_artifact_id": parent_id, "relation": "direct-input"}]}

    client.post(f"/api/v1/derivations/{derivation_id}/inputs", headers=_auth(ingest_key), json=payload)
    client.post(f"/api/v1/derivations/{derivation_id}/inputs", headers=_auth(ingest_key), json=payload)

    engine = build_engine(config.database_url)
    with Session(engine) as session:
        count = session.query(DerivationInput).filter_by(derivation_id=derivation_id).count()
        assert count == 1
        added_events = session.query(AuditEvent).filter_by(event_type="derivation.input_added").count()
        assert added_events == 1


def test_idempotency_key_replay_does_not_duplicate_derivation(client, config, ingest_key):
    body = {"recipe": "r", "recipe_version": "1"}
    headers = {**_auth(ingest_key), "Idempotency-Key": "deriv-req-1"}
    r1 = client.post("/api/v1/derivations", headers=headers, json=body)
    r2 = client.post("/api/v1/derivations", headers=headers, json=body)
    assert r1.json()["derivation_id"] == r2.json()["derivation_id"]

    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created_events = session.query(AuditEvent).filter_by(event_type="derivation.created").count()
        assert created_events == 1


def test_read_only_key_cannot_create_derivation(client, read_only_key):
    r = client.post(
        "/api/v1/derivations", headers=_auth(read_only_key), json={"recipe": "r", "recipe_version": "1"}
    )
    assert r.status_code == 403


class TestRecordedClosureInvariant:
    """Regression: recorded descendant/ancestor closure must never include
    inferred (or replay-confirmed/rejected/unresolved) edges -- only
    ``evidence_class == "recorded"``."""

    def test_inferred_edge_never_appears_in_recorded_descendants(self, client, config, ingest_key):
        root_id = _ingest(client, ingest_key, "inv-root", b"root body")
        hidden_id = _ingest(client, ingest_key, "inv-hidden", b"hidden body")

        engine = build_engine(config.database_url)
        with Session(engine) as session:
            from datetime import datetime, timezone

            session.add(
                InfluenceEdge(
                    source=root_id,
                    target=hidden_id,
                    relation="hidden-influence",
                    evidence_class="inferred",
                    status="active",
                    confidence=0.99,
                    payload_json={},
                    created_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        descendants = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=_auth(ingest_key)).json()
        assert hidden_id not in descendants["items"], "inferred edge leaked into recorded closure"
        assert descendants["items"] == [root_id]

        ancestors = client.get(f"/api/v1/lineage/{hidden_id}/ancestors", headers=_auth(ingest_key)).json()
        assert root_id not in ancestors["items"], "inferred edge leaked into recorded ancestor closure"

        # The `graph` endpoint (explicitly not a "closure" endpoint) is
        # allowed to surface the inferred edge, labeled as such.
        graph = client.get(
            f"/api/v1/lineage/{root_id}/graph", params={"evidence_classes": "recorded,inferred"}, headers=_auth(ingest_key)
        ).json()
        assert any(e["evidence_class"] == "inferred" for e in graph["edges"])
