"""Bounded candidate-neighborhood construction.

Avoids all-pairs comparison across the whole workspace by only considering
artifacts that share at least one cheap, explainable signal with the target:
a derivation time window, the same builder recipe, the same model, or
compatible artifact kind. Each admitted candidate carries the list of
reasons it entered the neighborhood, so a user can inspect *why* it was
considered at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain.models import Artifact, Derivation
from ..workspace import Workspace


@dataclass(frozen=True, slots=True)
class NeighborhoodEntry:
    candidate_id: str
    reasons: tuple[str, ...]


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_neighborhood(
    workspace: Workspace,
    target_artifact_id: str,
    *,
    time_window: timedelta = timedelta(hours=6),
    max_candidates: int = 200,
) -> list[NeighborhoodEntry]:
    target = workspace.artifacts.get(target_artifact_id)
    target_derivation = workspace.derivations.find_by_target(target_artifact_id)

    already_recorded_ancestors = {e.source for e in workspace.edges.incoming(target_artifact_id)}

    target_time = _parse_iso(target_derivation.started_at) if target_derivation else _parse_iso(target.created_at)
    target_recipe = target_derivation.recipe if target_derivation else None
    target_model = target_derivation.model_id if target_derivation else None

    entries: list[NeighborhoodEntry] = []
    for artifact in workspace.artifacts.list(limit=10_000):
        if artifact.artifact_id == target_artifact_id:
            continue
        if artifact.artifact_id in already_recorded_ancestors:
            continue  # already recorded; not a *hidden*-lineage candidate

        reasons: list[str] = []

        artifact_time = _parse_iso(artifact.created_at)
        if target_time is not None and artifact_time is not None:
            if abs((target_time - artifact_time).total_seconds()) <= time_window.total_seconds():
                reasons.append("derivation-time-window")

        candidate_derivation = workspace.derivations.find_by_target(artifact.artifact_id)
        if target_recipe and candidate_derivation and candidate_derivation.recipe == target_recipe:
            reasons.append("shared-builder-recipe")
        if target_model and candidate_derivation and candidate_derivation.model_id == target_model:
            reasons.append("shared-model")
        if target_derivation and candidate_derivation:
            shared_pool = set(target_derivation.candidate_context_pool) & set(
                candidate_derivation.candidate_context_pool
            )
            if shared_pool:
                reasons.append("shared-candidate-context-pool")

        if artifact.kind == target.kind:
            reasons.append("compatible-artifact-kind")

        if reasons:
            entries.append(NeighborhoodEntry(candidate_id=artifact.artifact_id, reasons=tuple(reasons)))

    entries.sort(key=lambda e: (-len(e.reasons), e.candidate_id))
    return entries[:max_candidates]
