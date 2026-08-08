from __future__ import annotations

import json

from skillrewind.attestation import (
    build_attestation,
    generate_keypair,
    render_html,
    render_markdown,
    sign_attestation,
    verify_attestation,
)
from skillrewind.domain.enums import ArtifactKind, RevocationPolicy, Severity
from skillrewind.domain.models import Derivation
from skillrewind.replay.deterministic import register_fixture
from skillrewind.revocation.service import request_revocation, run_revocation
from skillrewind.workspace import Workspace


def _fixture(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"]}


register_fixture("attestation-test-recipe", _fixture)


def _run_scenario(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    root = ws.ingest_artifact(b"root", kind=ArtifactKind.AGENT_SKILL, logical_name="root", alias="root")
    descendant = ws.ingest_artifact(
        b"descendant", kind=ArtifactKind.AGENT_SKILL, logical_name="descendant", alias="descendant"
    )
    ws.derivations.upsert(
        Derivation(
            derivation_id="d1", recipe="attestation-test-recipe", recipe_version="1",
            target_artifact_id=descendant.artifact_id, task_snapshot={"root_marker": root.artifact_id},
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z", seed=1,
        )
    )
    event = request_revocation(
        ws, roots=[root.artifact_id], reason="test", severity=Severity.HIGH,
        policy=RevocationPolicy.BALANCED, actor="tester", idempotency_key="k1",
    )
    event = run_revocation(ws, event)
    return ws, event


def test_attestation_digest_verifies(tmp_path):
    ws, event = _run_scenario(tmp_path)
    attestation = build_attestation(ws, event)
    outcome = verify_attestation(attestation)
    assert outcome.digest_valid
    assert outcome.ok
    ws.close()


def test_attestation_never_claims_perfect_unlearning(tmp_path):
    ws, event = _run_scenario(tmp_path)
    attestation = build_attestation(ws, event)
    claims_text = " ".join(attestation["bounded_claims"]).lower()
    assert "perfectly forgot" not in claims_text
    assert "erasure from any foundation model" in claims_text
    ws.close()


def test_attestation_mutation_breaks_digest_verification(tmp_path):
    ws, event = _run_scenario(tmp_path)
    attestation = build_attestation(ws, event)
    tampered = dict(attestation)
    tampered["revoked_roots"] = [*attestation["revoked_roots"], "skill://injected@sha256:" + "0" * 64]
    outcome = verify_attestation(tampered)
    assert not outcome.digest_valid
    ws.close()


def test_attestation_sign_and_verify_roundtrip(tmp_path):
    ws, event = _run_scenario(tmp_path)
    attestation = build_attestation(ws, event)
    keys = generate_keypair(tmp_path / "keys")
    signed = sign_attestation(attestation, keys.private_key_path)
    outcome = verify_attestation(signed, public_key_path=str(keys.public_key_path))
    assert outcome.ok
    assert outcome.signature_valid is True
    ws.close()


def test_attestation_signature_breaks_after_byte_mutation(tmp_path):
    ws, event = _run_scenario(tmp_path)
    attestation = build_attestation(ws, event)
    keys = generate_keypair(tmp_path / "keys")
    signed = sign_attestation(attestation, keys.private_key_path)
    tampered = json.loads(json.dumps(signed))
    tampered["revocation_reason"] = "tampered reason"
    outcome = verify_attestation(tampered, public_key_path=str(keys.public_key_path))
    assert not outcome.ok
    assert outcome.signature_valid is False
    ws.close()


def test_render_markdown_and_html_are_nonempty(tmp_path):
    ws, event = _run_scenario(tmp_path)
    attestation = build_attestation(ws, event)
    md = render_markdown(attestation)
    html_out = render_html(attestation)
    assert "SkillRewind Revocation Attestation" in md
    assert "<html>" in html_out
    ws.close()
