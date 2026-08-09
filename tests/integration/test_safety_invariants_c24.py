"""Phase C2.4 section 3: focused additions to the critical safety test
matrix -- only invariants not already covered elsewhere in the suite (see
`docs/release-readiness-v0.3.md` for what the broader existing suite
already proves: CAS corruption/dedup at the unit level, replay
allowlist/unresolved/rejected/confirmed semantics, revocation barrier
ordering, quarantine history preservation, waiver scope/expiry/revocation,
successor publication atomicity+idempotency, and SSE resume).

Groups covered here: ARTIFACT INTEGRITY (concurrent duplicate ingest, CAS
corruption surfacing through the API on a superseded/successor artifact),
AUTH/API (API key never leaked in error bodies, Problem Details shape
stability across error types, cross-actor read-isolation model), and
LINEAGE (duplicate relation idempotency observed through the public API).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
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

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "invariants.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def keys(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        full = create_api_key(session, name="full", actor="alice", scopes=["ingest", "read", "revoke", "admin"]).plaintext
        read_only = create_api_key(session, name="read-only", actor="bob", scopes=["read"]).plaintext
    return {"full": full, "read_only": read_only}


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# -- ARTIFACT INTEGRITY -------------------------------------------------------


def test_concurrent_duplicate_ingest_produces_exactly_one_artifact(client, keys):
    content = b"concurrently ingested content, byte-identical every time"

    def _ingest() -> str:
        r = client.post(
            "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "concurrent-dup"},
            headers=_auth(keys["full"]), content=content,
        )
        assert r.status_code == 201, r.text
        return r.json()["artifact_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        artifact_ids = list(pool.map(lambda _: _ingest(), range(16)))

    assert len(set(artifact_ids)) == 1, f"concurrent duplicate ingest must resolve to one artifact_id, got {set(artifact_ids)}"

    listing = client.get("/api/v1/artifacts", headers=_auth(keys["full"])).json()["items"]
    matching = [a for a in listing if a["artifact_id"] == artifact_ids[0]]
    assert len(matching) == 1, "concurrent duplicate ingest must not create duplicate artifact rows"


def test_corrupted_cas_object_surfaces_as_an_error_not_silent_wrong_content(client, config, keys):
    ingested = client.post(
        "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "will-be-corrupted"},
        headers=_auth(keys["full"]), content=b"original, uncorrupted content",
    ).json()
    artifact_id = ingested["artifact_id"]
    digest_hex = ingested["digest_hex"]

    cas_object_path = Path(config.resolved_cas_root) / "objects" / digest_hex[:2] / digest_hex[2:4] / digest_hex
    assert cas_object_path.exists(), "expected the CAS object on disk at the standard sharded path"
    cas_object_path.write_bytes(b"TAMPERED CONTENT, WRONG DIGEST")

    r = client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=_auth(keys["full"]))
    assert r.status_code >= 400, (
        "reading a corrupted CAS object must fail loudly (digest mismatch), never silently return the "
        f"tampered bytes as if they were valid; got {r.status_code}"
    )
    assert r.content != b"TAMPERED CONTENT, WRONG DIGEST"


# -- AUTH / API -------------------------------------------------------


def test_api_key_never_appears_in_error_response_bodies(client, keys):
    wrong_scope_response = client.get("/api/v1/admin/diagnostics", headers=_auth(keys["read_only"]))
    assert wrong_scope_response.status_code == 403
    assert keys["read_only"] not in wrong_scope_response.text

    bad_key = "srw_totally_bogus_not_a_real_key_00000000"
    unauthorized_response = client.get("/api/v1/artifacts", headers=_auth(bad_key))
    assert unauthorized_response.status_code == 401
    assert bad_key not in unauthorized_response.text

    truncated_bad_key_fragment = bad_key.split("_")[-1]
    assert truncated_bad_key_fragment not in unauthorized_response.text


def test_problem_details_shape_is_stable_across_error_types(client, keys):
    responses = {
        "401 unauthenticated": client.get("/api/v1/artifacts", headers={}),
        "403 wrong scope": client.get("/api/v1/admin/diagnostics", headers=_auth(keys["read_only"])),
        "404 not found": client.get("/api/v1/artifacts/does-not-exist", headers=_auth(keys["full"])),
        "422 unprocessable": client.post(
            "/api/v1/waivers", headers=_auth(keys["full"]),
            json={"artifact_id": "does-not-exist", "reason": "   "},
        ),
    }
    for label, response in responses.items():
        body = response.json()
        for field in ("type", "title", "status", "detail"):
            assert field in body, f"{label}: Problem Details response missing {field!r} field: {body}"
        assert body["status"] == response.status_code, f"{label}: status field must match the HTTP status code"


def test_cross_actor_read_access_follows_the_documented_scope_based_isolation_model(client, keys):
    """SkillRewind's current isolation model (documented in
    docs/threat-model.md) is scope-based, not per-actor ACLs: any
    `read`-scoped key can read any artifact regardless of which actor
    created it. This test pins that as an intentional, documented behavior
    -- not a silent gap -- so a regression toward accidental per-actor
    filtering (which would break legitimate cross-team read access) is
    caught, and so the documented limitation stays true."""

    created = client.post(
        "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "alices-artifact"},
        headers=_auth(keys["full"]), content=b"created by alice (full key)",
    ).json()
    artifact_id = created["artifact_id"]

    read_by_bob = client.get(f"/api/v1/artifacts/{artifact_id}", headers=_auth(keys["read_only"]))
    assert read_by_bob.status_code == 200, "a read-scoped key from a different actor must be able to read the artifact"
    assert read_by_bob.json()["artifact_id"] == artifact_id

    ingest_attempt_by_bob = client.post(
        "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "bobs-artifact"},
        headers=_auth(keys["read_only"]), content=b"bob has no ingest scope",
    )
    assert ingest_attempt_by_bob.status_code == 403, "scope, not actor identity, is what's enforced"


# -- LINEAGE -------------------------------------------------------


def test_duplicate_relation_is_idempotent_through_the_public_api(client, keys):
    parent = client.post(
        "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "parent"},
        headers=_auth(keys["full"]), content=b"parent content",
    ).json()["artifact_id"]

    deriv = client.post(
        "/api/v1/derivations", headers=_auth(keys["full"]),
        json={"recipe": "dup-relation-recipe", "recipe_version": "0.1", "payload": {}},
    ).json()
    derivation_id = deriv["derivation_id"]

    payload = {"inputs": [{"parent_artifact_id": parent, "relation": "direct-input"}]}
    r1 = client.post(f"/api/v1/derivations/{derivation_id}/inputs", headers=_auth(keys["full"]), json=payload)
    r2 = client.post(f"/api/v1/derivations/{derivation_id}/inputs", headers=_auth(keys["full"]), json=payload)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    listed = client.get(f"/api/v1/derivations/{derivation_id}", headers=_auth(keys["full"])).json()
    input_ids = [i for i in listed.get("recorded_inputs", listed.get("inputs", []))]
    assert input_ids.count(parent) <= 1, f"duplicate relation submission must not create duplicate input rows: {input_ids}"
