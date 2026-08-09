"""Candidate-recovery API (Phase C2.1 section 6): submits the real
`lineage.recover` job through the durable queue and exposes real persisted
recovery-run/candidate state -- these GET endpoints never fabricate an
empty success response for a run/candidate that does not exist; they 404.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.errors import NotFoundError
from ...features.bridge import config_digest as compute_config_digest
from ...lineage.service_recovery import request_key_for, snapshot_digest_for
from ...persistence.service.models import CandidateScore, CandidateScoringRun
from ...persistence.service.repositories import ArtifactRepository, CandidateRepository
from ..deps import ProblemDetail, get_cas, get_config, get_engine, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record
from .lineage import _split  # noqa: F401 (shared query-param parsing helper)

router = APIRouter(prefix="/api/v1/lineage", tags=["candidates"])

RECOVERY_SCORER_VERSION = "service-recovery-0.1.0"


def _run_to_dict(row: CandidateScoringRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "root_artifact_id": row.root_artifact_id,
        "candidate_scope": row.candidate_scope_json,
        "status": row.status,
        "job_id": row.job_id,
        "candidates_considered": row.candidates_considered,
        "candidates_found": row.candidates_found,
        "checkpoint_index": row.checkpoint_index,
        "scorer_version": row.scorer_version,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "status_url": f"/api/v1/lineage/recovery-runs/{row.run_id}",
        "events_url": f"/api/v1/events/stream?job_id={row.job_id}" if row.job_id else None,
    }


def _candidate_to_dict(row: CandidateScore) -> dict[str, Any]:
    return {
        "candidate_id": str(row.id),
        "run_id": row.run_id,
        "target_artifact_id": row.target_artifact_id,
        "candidate_artifact_id": row.candidate_artifact_id,
        "raw_score": row.raw_score,
        "calibrated_probability": row.calibrated_probability,  # always null: never fabricated from a raw score
        "strict_negative": row.strict_negative,
        "evidence_class": row.evidence_class,
        "status": row.status,
        "scorer_version": row.scorer_version,
        "feature_breakdown": row.feature_breakdown_json,
        "inclusion_reasons": row.inclusion_reasons_json,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class CreateRecoveryRunRequest(BaseModel):
    root_artifact_id: str = Field(..., examples=["skill://poisoned-root@sha256:" + "0" * 64])
    candidate_scope: Optional[dict[str, Any]] = Field(default=None, examples=[{"kind": "agent-skill"}])


@router.post("/recovery-runs", status_code=202)
def create_recovery_run(
    body: CreateRecoveryRunRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    engine: Engine = Depends(get_engine),
    config=Depends(get_config),
    auth=Depends(require_scope("ingest")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"

    try:
        ArtifactRepository(session, cas).get(body.root_artifact_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None

    snapshot_digest = snapshot_digest_for(session, cas, body.root_artifact_id, candidate_scope=body.candidate_scope)
    cfg_digest = compute_config_digest(config)
    request_key = request_key_for(
        root_artifact_id=body.root_artifact_id,
        snapshot_digest=snapshot_digest,
        scorer_version=RECOVERY_SCORER_VERSION,
        config_digest_value=cfg_digest,
        idempotency_key=idempotency_key,
    )

    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, default=str).encode()).hexdigest()
    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="lineage.recovery_run", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None and outcome.response_reference is not None:
            candidates = CandidateRepository(session)
            run = candidates.get_run(outcome.response_reference)
            return {
                "job_id": run.job_id,
                "run_id": run.run_id,
                "status": "accepted",
                "status_url": f"/api/v1/lineage/recovery-runs/{run.run_id}",
                "events_url": f"/api/v1/events/stream?job_id={run.job_id}" if run.job_id else None,
                "idempotent_replay": True,
            }

    candidates = CandidateRepository(session)
    run_id = f"recovery-{uuid.uuid4()}"
    run = candidates.create_run(
        run_id=run_id,
        request_key=request_key,
        root_artifact_id=body.root_artifact_id,
        candidate_scope=body.candidate_scope,
        feature_config={"weights": asdict(config.feature_weights)},
        threshold_config={"candidate": config.thresholds.candidate, "high_confidence": config.thresholds.high_confidence},
        scorer_version=RECOVERY_SCORER_VERSION,
        config_digest=cfg_digest,
        snapshot_digest=snapshot_digest,
        actor=actor,
        request_id=idempotency_key,
    )
    is_new_run = run.job_id is None

    if is_new_run:
        from ...jobs.queue import JobQueue

        queue = JobQueue(engine)
        job_id = queue.enqueue(
            "lineage.recover",
            {
                "database_url": config.database_url,
                "cas_root": config.resolved_cas_root,
                "run_id": run.run_id,
                "actor": actor,
                "request_id": idempotency_key,
            },
            idempotency_key=f"job-for-{run.run_id}",
            actor_id=actor,
        )
        candidates.set_run_job(run.run_id, job_id)
        run = candidates.get_run(run.run_id)

    if idempotency_key:
        record(
            session,
            key=idempotency_key,
            actor=actor,
            scope="lineage.recovery_run",
            request_digest=digest,
            response_reference=run.run_id,
        )

    return {
        "job_id": run.job_id,
        "run_id": run.run_id,
        "status": "accepted",
        "status_url": f"/api/v1/lineage/recovery-runs/{run.run_id}",
        "events_url": f"/api/v1/events/stream?job_id={run.job_id}" if run.job_id else None,
        "idempotent_replay": not is_new_run,
    }


@router.get("/recovery-runs/{run_id}")
def get_recovery_run(
    run_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    repo = CandidateRepository(session)
    try:
        return _run_to_dict(repo.get_run(run_id))
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None


@router.get("/recovery-runs/{run_id}/candidates")
def list_recovery_run_candidates(
    run_id: str,
    evidence_class: Optional[str] = Query(default=None),
    min_raw_score: Optional[float] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = CandidateRepository(session)
    try:
        repo.get_run(run_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    rows = repo.list_candidates(
        run_id, evidence_class=evidence_class, min_raw_score=min_raw_score, status=status, limit=limit, offset=offset
    )
    next_offset = offset + limit if len(rows) == limit else None
    return {"items": [_candidate_to_dict(r) for r in rows], "next_offset": next_offset}


@router.get("/candidates/{candidate_id}")
def get_candidate(
    candidate_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    repo = CandidateRepository(session)
    try:
        return _candidate_to_dict(repo.get_candidate(int(candidate_id)))
    except (ValueError, NotFoundError):
        raise ProblemDetail(404, "Not Found", f"no such candidate: {candidate_id}") from None


@router.get("/candidates/{candidate_id}/explanation")
def get_candidate_explanation(
    candidate_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    repo = CandidateRepository(session)
    try:
        row = repo.get_candidate(int(candidate_id))
    except (ValueError, NotFoundError):
        raise ProblemDetail(404, "Not Found", f"no such candidate: {candidate_id}") from None
    return {
        "candidate_id": str(row.id),
        "explanation": row.explanation_text,
        "feature_breakdown": row.feature_breakdown_json,
        "inclusion_reasons": row.inclusion_reasons_json,
        "raw_score": row.raw_score,
        "is_calibrated_probability": False,
        "evidence_class": row.evidence_class,
    }
