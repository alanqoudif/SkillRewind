"""Service-mode candidate-recovery application service (Phase C2.1 section 5).

Given a revoked/suspicious root artifact, scores every other active artifact
not already in the root's *recorded* descendant closure against the root
using the same explainable feature-family scorer Lite mode uses
(`skillrewind.inference.scoring.score_candidate`) -- no scoring algorithm is
duplicated here, only Service-mode plumbing (artifact/derivation/lineage
repositories instead of a `Workspace`). A candidate that scores above the
configured threshold is persisted as an `inferred`-evidence `CandidateScore`
row; it is never written as `recorded` or `replay-confirmed` by this module
-- only a real replay result can produce the latter (out of scope for this
milestone; see `docs/completion-matrix-v0.3.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..canonical.json import sha256_hex
from ..config import SkillRewindConfig
from ..features.behavioral import behavioral_similarity
from ..features.bridge import extract_pair_breakdown, is_strict_negative
from ..inference.scoring import score_candidate
from ..persistence.service.repositories import (
    ArtifactRepository,
    CandidateRepository,
    LineageRepository,
    record_audit_event,
)
from .graph import LineageGraph

RECOVERY_SCORER_VERSION = "service-recovery-0.1.0"


@dataclass(frozen=True, slots=True)
class RecoveryProgress:
    considered: int
    total: int
    found: int


ProgressCallback = Callable[[RecoveryProgress], None]
CancelCheck = Callable[[], bool]


def candidate_pool(session, cas, root_artifact_id: str, *, candidate_scope: Optional[dict[str, Any]] = None) -> list[str]:
    """Bounded candidate pool: every active artifact except the root itself
    and the root's *recorded* descendants (those are already known-lineage,
    not hidden). `candidate_scope` may restrict by `kind`."""

    artifacts = ArtifactRepository(session, cas)
    lineage = LineageRepository(session)
    recorded_descendants = set(lineage.descendants([root_artifact_id], include_roots=True))

    kind_filter = (candidate_scope or {}).get("kind")
    pool: list[str] = []
    offset_cursor = None
    while True:
        page = artifacts.list(limit=500, cursor=offset_cursor, kind=kind_filter)
        if not page:
            break
        for artifact in page:
            if artifact.artifact_id in recorded_descendants:
                continue
            if artifact.status != "active":
                continue
            pool.append(artifact.artifact_id)
        if len(page) < 500:
            break
        offset_cursor = ArtifactRepository.cursor_for(page[-1])
    pool.sort()
    return pool


def snapshot_digest_for(session, cas, root_artifact_id: str, *, candidate_scope: Optional[dict[str, Any]] = None) -> str:
    """A digest of the exact candidate pool considered, so a recovery run is
    reproducibly idempotent for a given root + artifact snapshot + scorer +
    config -- distinct pool contents (e.g. new artifacts ingested since the
    last run) yield a distinct run rather than silently reusing stale
    results."""

    pool = candidate_pool(session, cas, root_artifact_id, candidate_scope=candidate_scope)
    return sha256_hex({"root": root_artifact_id, "pool": pool})


def request_key_for(
    *,
    root_artifact_id: str,
    snapshot_digest: str,
    scorer_version: str,
    config_digest_value: str,
    idempotency_key: Optional[str],
) -> str:
    if idempotency_key:
        return f"idem:{idempotency_key}"
    return sha256_hex(
        {
            "root": root_artifact_id,
            "snapshot_digest": snapshot_digest,
            "scorer_version": scorer_version,
            "config_digest": config_digest_value,
        }
    )


def run_candidate_recovery(
    session,
    cas,
    config: SkillRewindConfig,
    *,
    run_id: str,
    actor: str = "system",
    request_id: Optional[str] = None,
    on_progress: Optional[ProgressCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> CandidateRepository:
    """Runs (or resumes) candidate recovery for an already-created
    `CandidateScoringRun` row. Checkpoint-resumable: candidates already
    persisted for this `run_id` (see `CandidateRepository.add_candidate`'s
    idempotent insert) are skipped on resume, so a worker crash between
    candidates and a naive retry never produces duplicate candidate rows or
    duplicate audit events. Cancellation is honored between candidates (a
    safe checkpoint -- never mid-candidate)."""

    candidates_repo = CandidateRepository(session)
    run = candidates_repo.get_run(run_id)

    lineage_repo = LineageRepository(session)
    recorded_graph_edges = lineage_repo.load_edges(evidence_classes=["recorded"])
    graph = LineageGraph(recorded_graph_edges)

    pool = candidate_pool(session, cas, run.root_artifact_id, candidate_scope=run.candidate_scope_json)
    already_scored = candidates_repo.already_scored_candidate_ids(run_id)

    artifacts = ArtifactRepository(session, cas)
    root = artifacts.get(run.root_artifact_id)

    found = len(already_scored)
    for index, candidate_id in enumerate(pool):
        if should_cancel is not None and should_cancel():
            candidates_repo.update_run_progress(run_id, checkpoint_index=index, candidates_considered=index)
            candidates_repo.finish_run(run_id, status="cancelled")
            record_audit_event(
                session,
                event_type="lineage.recovery_cancelled",
                actor=actor,
                payload={"run_id": run_id, "checkpoint_index": index},
                request_id=request_id,
                entity_id=run_id,
            )
            return candidates_repo

        if candidate_id in already_scored:
            if on_progress:
                on_progress(RecoveryProgress(considered=index + 1, total=len(pool), found=found))
            continue

        candidate_artifact = artifacts.get(candidate_id)
        breakdown = extract_pair_breakdown(
            session,
            cas,
            config,
            source_artifact_id=candidate_id,
            target_artifact_id=run.root_artifact_id,
            recorded_graph=graph,
            persist=True,
        )
        behavioral_score = behavioral_similarity(candidate_artifact.metadata_json or {}, root.metadata_json or {})
        strict_negative = is_strict_negative(behavioral_score, breakdown.expression)

        result = score_candidate(
            breakdown,
            weights=config.feature_weights,
            thresholds=config.thresholds,
            strict_negative_signal=strict_negative,
        )

        reasons = [f"{family}={value:.3f}" for family, value in breakdown.as_dict().items() if value is not None]
        status = "candidate" if result.is_candidate else "below-threshold"
        if strict_negative:
            status = "strict-negative"
        explanation = (
            f"Artifact {candidate_id!r} scored {result.score:.3f} against root {run.root_artifact_id!r} "
            f"using scorer {result.scorer_version} (uncalibrated raw score, not a probability). "
            f"Contributing features: {', '.join(reasons) if reasons else 'none available'}."
        )
        if strict_negative:
            explanation += " Strict-negative behavioral signal observed; high-confidence label suppressed pending replay."

        created = candidates_repo.add_candidate(
            run_id=run_id,
            target_artifact_id=run.root_artifact_id,
            candidate_artifact_id=candidate_id,
            raw_score=result.score,
            feature_breakdown=result.breakdown.as_dict(),
            scorer_version=RECOVERY_SCORER_VERSION,
            strict_negative=strict_negative,
            status=status,
            inclusion_reasons=reasons,
            explanation_text=explanation,
        )
        if created is not None and result.is_candidate:
            found += 1
            record_audit_event(
                session,
                event_type="lineage.candidate_recorded",
                actor=actor,
                payload={
                    "run_id": run_id,
                    "root_artifact_id": run.root_artifact_id,
                    "candidate_artifact_id": candidate_id,
                    "raw_score": result.score,
                    "evidence_class": "inferred",
                },
                request_id=request_id,
                entity_id=f"{run_id}:{candidate_id}",
            )

        candidates_repo.update_run_progress(run_id, checkpoint_index=index + 1, candidates_considered=index + 1)
        if on_progress:
            on_progress(RecoveryProgress(considered=index + 1, total=len(pool), found=found))

    candidates_repo.finish_run(run_id, status="completed")
    record_audit_event(
        session,
        event_type="lineage.recovery_completed",
        actor=actor,
        payload={"run_id": run_id, "root_artifact_id": run.root_artifact_id, "candidates_found": found},
        request_id=request_id,
        entity_id=run_id,
    )
    return candidates_repo
