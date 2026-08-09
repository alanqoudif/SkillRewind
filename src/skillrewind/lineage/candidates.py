"""Hidden-lineage candidate recovery: neighborhood -> features -> explainable score."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import SkillRewindConfig
from ..domain.enums import EvidenceClass, RelationType
from ..domain.models import Artifact, InfluenceEdge
from ..features.behavioral import behavioral_similarity
from ..features.expression import expression_similarity
from ..features.graph import graph_similarity
from ..features.implementation import implementation_similarity
from ..features.operational import operational_similarity
from ..features.temporal import temporal_proximity
from ..inference.scoring import FeatureBreakdown, ScoreResult, score_candidate
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike
from .closure import build_graph
from .neighborhoods import build_neighborhood


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidate_id: str
    target_id: str
    neighborhood_reasons: tuple[str, ...]
    result: ScoreResult

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "target_id": self.target_id,
            "neighborhood_reasons": list(self.neighborhood_reasons),
            "score": self.result.score,
            "is_candidate": self.result.is_candidate,
            "is_high_confidence": self.result.is_high_confidence,
            "strict_negative_signal": self.result.strict_negative_signal,
            "breakdown": self.result.breakdown.as_dict(),
            "missing_features": self.result.missing_features,
            "weights_used": self.result.weights_used,
            "scorer_version": self.result.scorer_version,
        }


def _artifact_text(workspace: WorkspaceLike, artifact: Artifact) -> tuple[str, str]:
    """Return ``(expression_text, python_source_text)`` best-effort for an artifact."""

    manifest = artifact.metadata.get("agent_skills_manifest")
    if manifest:
        expr_text = f"{manifest.get('description', '')}\n{manifest.get('body', '')}"
        python_source = ""
        for entry in manifest.get("files", []):
            if entry["path"].endswith(".py"):
                try:
                    python_source += workspace.cas.get_bytes(entry["sha256"]).decode("utf-8", errors="replace")
                except Exception:
                    continue
        return expr_text, python_source

    try:
        raw = workspace.cas.get_bytes(artifact.digest_hex).decode("utf-8", errors="replace")
    except Exception:
        return "", ""
    if artifact.mime_type in ("text/x-python", "application/x-python"):
        return raw, raw
    return raw, ""


def score_pair(
    workspace: WorkspaceLike,
    candidate: Artifact,
    target: Artifact,
    *,
    config: SkillRewindConfig,
) -> ScoreResult:
    candidate_expr, candidate_py = _artifact_text(workspace, candidate)
    target_expr, target_py = _artifact_text(workspace, target)

    expression = None
    if candidate_expr and target_expr:
        expression = expression_similarity(candidate_expr, target_expr).combined

    implementation = None
    if candidate_py and target_py:
        implementation = implementation_similarity(candidate_py, target_py).combined

    candidate_derivation = workspace.derivations.find_by_target(candidate.artifact_id)
    target_derivation = workspace.derivations.find_by_target(target.artifact_id)
    operational = None
    if candidate_derivation and target_derivation:
        operational = operational_similarity(
            candidate_derivation.tool_calls, target_derivation.tool_calls
        ).combined

    behavioral_score = behavioral_similarity(candidate.metadata, target.metadata)
    behavioral = behavioral_score.combined if behavioral_score.source != "unavailable" else None

    graph = build_graph(workspace)
    graph_score = graph_similarity(graph, candidate.artifact_id, target.artifact_id).combined

    temporal = None
    if candidate_derivation and target_derivation:
        temporal = temporal_proximity(candidate_derivation.started_at, target_derivation.started_at)

    breakdown = FeatureBreakdown(
        expression=expression,
        implementation=implementation,
        operational=operational,
        behavioral=behavioral,
        graph=graph_score,
        temporal=temporal,
    )

    # Strict-negative heuristic: behavioral probes actively disagree (e.g. a
    # differing canary/policy outcome) while lexical similarity is low. This
    # never *proves* independence -- only replay can do that -- it only
    # suppresses a false high-confidence label pending replay.
    strict_negative = (
        behavioral_score.source == "stored-probes"
        and behavioral_score.canary_agreement == 0.0
        and (expression or 0.0) < 0.5
    )

    return score_candidate(
        breakdown,
        weights=config.feature_weights,
        thresholds=config.thresholds,
        strict_negative_signal=strict_negative,
    )


def recover_candidates(
    workspace: WorkspaceLike,
    target_artifact_id: str,
    *,
    config: Optional[SkillRewindConfig] = None,
    persist: bool = True,
) -> list[CandidateResult]:
    config = config or workspace.config
    target = workspace.artifacts.get(target_artifact_id)
    neighborhood = build_neighborhood(workspace, target_artifact_id)

    results: list[CandidateResult] = []
    for entry in neighborhood:
        candidate = workspace.artifacts.get(entry.candidate_id)
        score_result = score_pair(workspace, candidate, target, config=config)
        result = CandidateResult(
            candidate_id=entry.candidate_id,
            target_id=target_artifact_id,
            neighborhood_reasons=entry.reasons,
            result=score_result,
        )
        results.append(result)

        if persist and score_result.is_candidate:
            now = timestamp()
            edge = InfluenceEdge(
                source=entry.candidate_id,
                target=target_artifact_id,
                relation=RelationType.HIDDEN_INFLUENCE,
                evidence_class=EvidenceClass.INFERRED,
                confidence=score_result.score,
                evidence=[
                    {
                        "kind": "static-multi-trace",
                        "scorer_version": score_result.scorer_version,
                        "breakdown": score_result.breakdown.as_dict(),
                        "missing_features": score_result.missing_features,
                        "neighborhood_reasons": list(entry.reasons),
                    }
                ],
                created_at=now,
                updated_at=now,
                scorer_version=score_result.scorer_version,
            )
            workspace.edges.upsert(edge)

    results.sort(key=lambda r: (-r.result.score, r.candidate_id))
    if persist:
        workspace.audit.append(
            "candidates.recovered",
            config.actor,
            {"target_artifact_id": target_artifact_id, "candidate_count": len(results)},
        )
    return results
