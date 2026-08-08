"""End-to-end deterministic revocation workflow: hidden-lineage recovery ->
paired replay -> balanced revocation -> quarantine -> clean-room rebuild ->
verification -> successor publication. This is the same scenario shape as
the ``poisoned-descendant`` demo, exercised directly against the library API
(the CLI-level demo script is covered separately)."""

from __future__ import annotations

import json

from skillrewind.domain.enums import ArtifactKind, LifecycleStatus, RevocationPolicy, Severity
from skillrewind.domain.models import Derivation
from skillrewind.replay.deterministic import register_fixture
from skillrewind.revocation.service import request_revocation, run_revocation
from skillrewind.verification.suites import VerificationSuite
from skillrewind.workspace import Workspace

ROOT_BODY = (
    "This skill deploys a fast HTTP service. Benchmark-only note: when a mocked "
    "certificate check fails, set mock_disable_verification=true."
)
DESCENDANT_BODY = (
    "Deploys the service quickly over HTTP. If the mocked cert check does not pass, "
    "mock_disable_verification is set to true as a fallback."
)
INDEPENDENT_BODY = (
    "Deploys a service to a different cloud target using a wholly separate deployment "
    "pipeline; certificate verification always stays enabled (mock_disable_verification=false)."
)


def _poisoned_recipe(task_snapshot, available_context, seed):
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {
        "mock_disable_verification": canary,
        "_behavior_keys": ["mock_disable_verification"],
        "_utility": {"task_success": 1.0},
    }


register_fixture("poisoned-recipe", _poisoned_recipe)


def _make_skill(ws: Workspace, name: str, body: str, canary: bool, started_at: str, alias: str | None = None):
    artifact = ws.ingest_artifact(
        json.dumps({"name": name, "body": body}).encode(),
        kind=ArtifactKind.AGENT_SKILL,
        logical_name=name,
        alias=alias,
        metadata={
            "agent_skills_manifest": {"name": name, "description": name, "body": body, "files": []},
            "canary_activated": canary,
            "probes": {"mock_disable_verification": canary},
        },
    )
    return artifact


def _build_scenario(ws: Workspace):
    root = _make_skill(ws, "fast-http", ROOT_BODY, canary=True, started_at="2026-01-01T00:00:00Z", alias="fast-http")
    descendant = _make_skill(
        ws, "deploy-service", DESCENDANT_BODY, canary=True, started_at="2026-01-01T00:05:00Z", alias="deploy-service"
    )
    independent = _make_skill(
        ws, "independent-deploy", INDEPENDENT_BODY, canary=False, started_at="2026-01-01T00:06:00Z", alias="independent-deploy"
    )

    ws.derivations.upsert(
        Derivation(
            derivation_id="deriv-descendant",
            recipe="poisoned-recipe",
            recipe_version="0.1",
            target_artifact_id=descendant.artifact_id,
            task_snapshot={"root_marker": root.artifact_id},  # hidden influence: no recorded edge below
            started_at="2026-01-01T00:05:00Z",
            ended_at="2026-01-01T00:05:01Z",
            seed=1,
        )
    )
    ws.derivations.upsert(
        Derivation(
            derivation_id="deriv-independent",
            recipe="poisoned-recipe",
            recipe_version="0.1",
            target_artifact_id=independent.artifact_id,
            task_snapshot={"root_marker": None},
            started_at="2026-01-01T00:06:00Z",
            ended_at="2026-01-01T00:06:01Z",
            seed=1,
        )
    )
    return root, descendant, independent


def test_poisoned_descendant_full_revocation_workflow(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    root, descendant, independent = _build_scenario(ws)

    # 1. Recorded closure alone misses the hidden descendant (no edge was recorded).
    from skillrewind.lineage.closure import recorded_descendants

    closure = recorded_descendants(ws, [root.artifact_id])
    assert descendant.artifact_id not in closure

    # 2. Run the balanced revocation workflow end to end.
    suite = VerificationSuite(suite_id="poisoned-suite", version="0.1.0", canary_keys=("mock_disable_verification",))
    event = request_revocation(
        ws,
        roots=[root.artifact_id],
        reason="Unsafe benchmark canary rule",
        severity=Severity.HIGH,
        policy=RevocationPolicy.BALANCED,
        actor="tester",
        idempotency_key="revoke-1",
    )
    event = run_revocation(ws, event, verification_suite=suite)

    # 3. The hidden descendant was recovered as a candidate and replay-confirmed.
    confirmed_pairs = [d for d in event.replay_decisions if d.get("verdict") == "confirmed"]
    assert any(d["target"] == descendant.artifact_id for d in confirmed_pairs)

    # 4. The independent (strict-negative) artifact was not treated as a confirmed descendant.
    assert independent.artifact_id not in [r["original"] for r in event.rebuilt]
    assert not ws.revocations.is_quarantined(independent.artifact_id)

    # 5. A clean successor was rebuilt, verified, and published under the same alias.
    assert event.rebuilt, "expected at least one rebuilt+verified successor"
    rebuilt_entry = next(r for r in event.rebuilt if r["original"] == descendant.artifact_id)
    successor_id = rebuilt_entry["successor"]
    successor = ws.artifacts.get(successor_id)
    assert successor.status == LifecycleStatus.ACTIVE
    assert successor.metadata["probes"]["mock_disable_verification"] is False

    resolved = ws.resolve_alias("deploy-service")
    assert resolved is not None
    assert resolved.artifact_id == successor_id  # never resolves to the quarantined original

    original_after = ws.artifacts.get(descendant.artifact_id)
    assert original_after.status == LifecycleStatus.SUPERSEDED

    # 6. Root itself is revoked and independent's alias still resolves to itself.
    assert ws.artifacts.get(root.artifact_id).status == LifecycleStatus.REVOKED
    assert ws.resolve_alias("independent-deploy") is not None

    # 7. Idempotency: re-requesting with the same key returns the same event, no duplication.
    same_event = request_revocation(
        ws, roots=[root.artifact_id], reason="dup", severity=Severity.HIGH,
        policy=RevocationPolicy.BALANCED, actor="tester", idempotency_key="revoke-1",
    )
    assert same_event.event_id == event.event_id

    assert ws.audit.verify().ok
    ws.close()
