from __future__ import annotations

from skillrewind.domain.enums import ArtifactKind
from skillrewind.domain.models import Derivation
from skillrewind.lineage.candidates import recover_candidates
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


def _make_skill(ws: Workspace, name: str, body: str, canary: bool, started_at: str) -> str:
    manifest_body = body
    artifact = ws.ingest_artifact(
        __import__("json").dumps({"name": name, "description": name, "body": manifest_body, "files": []}).encode(),
        kind=ArtifactKind.AGENT_SKILL,
        logical_name=name,
        metadata={
            "agent_skills_manifest": {
                "name": name, "description": name, "body": manifest_body, "files": [],
            },
            "canary_activated": canary,
            "probes": {"mock_disable_verification": canary},
        },
    )
    ws.derivations.upsert(
        Derivation(
            derivation_id=f"deriv-{name}",
            recipe="skill-distillation",
            recipe_version="0.1",
            target_artifact_id=artifact.artifact_id,
            started_at=started_at,
            ended_at=started_at,
        )
    )
    return artifact.artifact_id


def test_hidden_descendant_is_recovered_and_strict_negative_is_not(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    root_id = _make_skill(ws, "fast-http", ROOT_BODY, canary=True, started_at="2026-01-01T00:00:00Z")
    # No recorded edge from root -> descendant: this is the hidden-lineage scenario.
    descendant_id = _make_skill(ws, "deploy-service", DESCENDANT_BODY, canary=True, started_at="2026-01-01T00:05:00Z")
    independent_id = _make_skill(
        ws, "independent-deploy", INDEPENDENT_BODY, canary=False, started_at="2026-01-01T00:06:00Z"
    )

    results = recover_candidates(ws, descendant_id)
    by_id = {r.candidate_id: r for r in results}

    assert root_id in by_id
    assert by_id[root_id].result.is_candidate
    assert by_id[root_id].result.score > 0.35

    # The independent artifact must not be misclassified as a confident hidden descendant.
    if independent_id in by_id:
        assert not by_id[independent_id].result.is_high_confidence

    # Persisted as an inferred (not replay-confirmed) edge.
    incoming = ws.edges.incoming(descendant_id)
    inferred_sources = {e.source for e in incoming if e.evidence_class.value == "inferred"}
    assert root_id in inferred_sources
    ws.close()
