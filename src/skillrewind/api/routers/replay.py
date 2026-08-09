"""Service-mode replay API (Phase C2.2): submit paired replay for an inferred
candidate, and read back run status/evidence/classification. Never claims
success before the corresponding database writes commit -- submission
enqueues the real `lineage.replay` job and returns 202 only after the
`ReplayRun` row and job are both committed; GET endpoints only ever return
already-committed state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.errors import NotFoundError
from ...persistence.service.models import ReplayRecord, ReplayRun
from ...persistence.service.repositories import (
    ArtifactRepository,
    CandidateRepository,
    DerivationRepository,
    ReplayRepository,
)
from ...replay.base import approved_runner_names
from ..deps import ProblemDetail, get_cas, get_config, get_engine, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record

router = APIRouter(prefix="/api/v1/replay", tags=["replay"])


def _run_to_dict(row: ReplayRun) -> dict[str, Any]:
    return {
        "replay_run_id": row.replay_run_id,
        "candidate_id": str(row.candidate_id),
        "ancestor_artifact_id": row.ancestor_artifact_id,
        "descendant_artifact_id": row.descendant_artifact_id,
        "target_derivation_id": row.target_derivation_id,
        "runner_name": row.runner_name,
        "repetitions_requested": row.repetitions_requested,
        "status": row.status,
        "job_id": row.job_id,
        "checkpoint_repetition": row.checkpoint_repetition,
        "verdict": row.verdict,
        "fidelity_overall": row.fidelity_overall,
        "effect_estimate": row.effect_estimate,
        "created_by_actor": row.created_by_actor,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "status_url": f"/api/v1/replay/runs/{row.replay_run_id}",
        "events_url": f"/api/v1/events/stream?job_id={row.job_id}" if row.job_id else None,
    }


def _record_to_dict(row: ReplayRecord) -> dict[str, Any]:
    return {
        "replay_id": row.replay_id,
        "repetition_index": row.repetition_index,
        "intervention_kind": row.intervention_kind,
        "runner_id": row.runner_id,
        "verdict": row.verdict,
        "payload": row.payload_json,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _require_same_tenant(auth, run: ReplayRun) -> None:
    """Tenant isolation for this milestone: an API key's `actor` is treated
    as its tenant. A key may only read a replay run it created, unless it
    carries the `admin` scope. (There is no broader multi-tenant partitioning
    elsewhere in this schema yet -- see docs/adr/0012-service-replay-evidence.md.)
    """

    if auth is None:  # auth disabled (dev/Lite-style single-tenant mode)
        return
    if "admin" in auth.scopes:
        return
    if auth.actor != run.created_by_actor:
        raise ProblemDetail(403, "Forbidden", "cross-tenant access: this replay run belongs to a different actor")


class SubmitReplayRequest(BaseModel):
    candidate_id: str = Field(..., examples=["1"])
    runner_name: str = Field(default="deterministic-fixture", examples=["deterministic-fixture"])
    repetitions: int = Field(default=1, ge=1, le=100)


@router.post("/runs", status_code=202)
def submit_replay_run(
    body: SubmitReplayRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    engine: Engine = Depends(get_engine),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("replay")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"

    if body.runner_name not in approved_runner_names():
        raise ProblemDetail(
            422, "Unprocessable Entity", f"runner {body.runner_name!r} is not approved: {approved_runner_names()}"
        )

    candidates = CandidateRepository(session)
    try:
        candidate = candidates.get_candidate(int(body.candidate_id))
    except (ValueError, NotFoundError):
        raise ProblemDetail(404, "Not Found", f"no such candidate: {body.candidate_id}") from None

    ancestor_artifact_id = candidate.target_artifact_id
    descendant_artifact_id = candidate.candidate_artifact_id

    artifacts = ArtifactRepository(session, cas)
    for artifact_id in (ancestor_artifact_id, descendant_artifact_id):
        try:
            artifact = artifacts.get(artifact_id)
        except NotFoundError as exc:
            raise ProblemDetail(404, "Not Found", str(exc)) from None
        if not artifacts.cas.verify_integrity(artifact.digest_hex):
            raise ProblemDetail(
                422,
                "Unprocessable Entity",
                f"missing or corrupted CAS content for artifact {artifact_id!r} (digest {artifact.digest_hex!r})",
            )

    derivations = DerivationRepository(session, artifacts)
    target_derivation_id: Optional[str] = None
    try:
        deriv = derivations.get_derivation_for_output(descendant_artifact_id)
        target_derivation_id = deriv.derivation_id
    except NotFoundError:
        # No recorded derivation to reconstruct from -- accepted, not
        # rejected: the run is created and will resolve to
        # UNRESOLVED_MISSING_INPUTS once processed (invariant 2).
        pass

    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, default=str).encode()).hexdigest()
    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="replay.submit", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None and outcome.response_reference is not None:
            replays = ReplayRepository(session)
            run = replays.get_run(outcome.response_reference)
            return {**_run_to_dict(run), "idempotent_replay": True}

    request_key = (
        f"idem:{idempotency_key}"
        if idempotency_key
        else hashlib.sha256(
            json.dumps(
                {
                    "candidate_id": body.candidate_id,
                    "runner_name": body.runner_name,
                    "repetitions": body.repetitions,
                    "target_derivation_id": target_derivation_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )

    replays = ReplayRepository(session)
    replay_run_id = f"replay-run-{uuid.uuid4()}"
    run = replays.create_run(
        replay_run_id=replay_run_id,
        request_key=request_key,
        candidate_id=candidate.id,
        ancestor_artifact_id=ancestor_artifact_id,
        descendant_artifact_id=descendant_artifact_id,
        target_derivation_id=target_derivation_id,
        runner_name=body.runner_name,
        repetitions_requested=body.repetitions,
        actor=actor,
        request_id=idempotency_key,
    )
    is_new_run = run.job_id is None and run.status == "queued"

    if is_new_run:
        from ...jobs.queue import JobQueue

        queue = JobQueue(engine)
        job_id = queue.enqueue(
            "lineage.replay",
            {
                "database_url": config.database_url,
                "cas_root": config.resolved_cas_root,
                "replay_run_id": run.replay_run_id,
                "actor": actor,
                "request_id": idempotency_key,
            },
            idempotency_key=f"job-for-{run.replay_run_id}",
            actor_id=actor,
        )
        replays.set_run_job(run.replay_run_id, job_id)
        run = replays.get_run(run.replay_run_id)

    if idempotency_key:
        record(
            session, key=idempotency_key, actor=actor, scope="replay.submit", request_digest=digest, response_reference=run.replay_run_id
        )

    return {**_run_to_dict(run), "idempotent_replay": not is_new_run}


@router.get("/runs/{replay_run_id}")
def get_replay_run(
    replay_run_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    replays = ReplayRepository(session)
    try:
        run = replays.get_run(replay_run_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    _require_same_tenant(auth, run)
    return _run_to_dict(run)


@router.get("/runs/{replay_run_id}/evidence")
def get_replay_run_evidence(
    replay_run_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    replays = ReplayRepository(session)
    try:
        run = replays.get_run(replay_run_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    _require_same_tenant(auth, run)
    repetitions = replays.list_repetitions(replay_run_id)
    return {"replay_run_id": replay_run_id, "repetitions": [_record_to_dict(r) for r in repetitions]}


@router.get("/runs/{replay_run_id}/classification")
def get_replay_run_classification(
    replay_run_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    replays = ReplayRepository(session)
    try:
        run = replays.get_run(replay_run_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    _require_same_tenant(auth, run)
    if run.status not in ("completed", "failed", "cancelled"):
        raise ProblemDetail(409, "Conflict", f"replay run {replay_run_id} has not finished yet (status={run.status})")
    return {
        "replay_run_id": replay_run_id,
        "status": run.status,
        "verdict": run.verdict,
        "fidelity_overall": run.fidelity_overall,
        "effect_estimate": run.effect_estimate,
        "is_calibrated_probability": False,
    }
