"""Service-mode revocation API (Phase C2.3): preview -> submit -> monitor ->
cancel. Submission enqueues the real `revocation.execute` job against
Service-mode persistence (via `ServiceWorkspace`) and returns 202 only after
the `RevocationEvent` row and job are both committed. Preview never mutates
state -- see `skillrewind.revocation.service.build_revocation_preview`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.enums import RevocationPolicy, RevocationState, Severity
from ...domain.errors import InvalidStateTransitionError, NotFoundError
from ...domain.models import RevocationEvent
from ...persistence.service.workspace import ServiceWorkspace
from ...revocation.service import build_revocation_preview, request_revocation
from ...revocation.state_machine import transition
from ..deps import ProblemDetail, get_cas, get_config, get_engine, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record

router = APIRouter(prefix="/api/v1/revocations", tags=["revocations"])


def _event_to_dict(event: RevocationEvent) -> dict[str, Any]:
    return {
        "revocation_id": event.event_id,
        "roots": event.roots,
        "reason": event.reason,
        "severity": event.severity.value,
        "policy": event.policy.value,
        "actor": event.actor,
        "state": event.state.value,
        "recorded_closure": event.recorded_closure,
        "candidates": event.candidates,
        "replay_decisions": event.replay_decisions,
        "quarantined": event.quarantined,
        "rebuilt": event.rebuilt,
        "unresolved": event.unresolved,
        "created_at": event.created_at,
        "completed_at": event.completed_at,
        "status_url": f"/api/v1/revocations/{event.event_id}",
    }


class PreviewRequest(BaseModel):
    roots: list[str] = Field(..., min_length=1)
    policy: str = Field(default="balanced")
    manual_targets: list[dict] = Field(default_factory=list)


@router.post("/preview")
def preview_revocation(
    body: PreviewRequest,
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    try:
        policy = RevocationPolicy(body.policy)
    except ValueError:
        raise ProblemDetail(422, "Unprocessable Entity", f"unknown policy: {body.policy!r}") from None

    ws = ServiceWorkspace(session, cas, config)
    for root in body.roots:
        try:
            ws.artifacts.get(root)
        except NotFoundError as exc:
            raise ProblemDetail(404, "Not Found", str(exc)) from None

    preview = build_revocation_preview(ws, roots=body.roots, policy=policy, manual_targets=body.manual_targets)
    return preview.to_dict()


class SubmitRevocationRequest(BaseModel):
    roots: list[str] = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    severity: str = Field(default="high")
    policy: str = Field(default="balanced")
    rebuild_enabled: bool = Field(default=True)
    verification_suite: Optional[dict] = None
    attestation_requested: bool = Field(default=False)
    sign_requested: bool = Field(default=False)
    replay_selection: str = Field(default="active")
    max_replay_calls: Optional[int] = None


@router.post("", status_code=202)
def submit_revocation(
    body: SubmitRevocationRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    engine: Engine = Depends(get_engine),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("revoke")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"

    try:
        policy = RevocationPolicy(body.policy)
        severity = Severity(body.severity)
    except ValueError as exc:
        raise ProblemDetail(422, "Unprocessable Entity", str(exc)) from None

    ws = ServiceWorkspace(session, cas, config)
    for root in body.roots:
        try:
            ws.artifacts.get(root)
        except NotFoundError as exc:
            raise ProblemDetail(404, "Not Found", str(exc)) from None

    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, default=str).encode()).hexdigest()
    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="revocation.submit", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None and outcome.response_reference is not None:
            event = ws.revocations.get(outcome.response_reference)
            return {**_event_to_dict(event), "idempotent_replay": True}

    event_idempotency_key = idempotency_key or (
        "auto-"
        + hashlib.sha256(
            json.dumps(
                {"roots": sorted(body.roots), "reason": body.reason, "severity": body.severity, "policy": body.policy},
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )

    is_new_event = ws.revocations.get_by_idempotency_key(event_idempotency_key) is None
    event = request_revocation(
        ws,
        roots=body.roots,
        reason=body.reason,
        severity=severity,
        policy=policy,
        actor=actor,
        idempotency_key=event_idempotency_key,
        budget={"replay_calls": body.max_replay_calls} if body.max_replay_calls else None,
    )

    from ...jobs.queue import JobQueue

    queue = JobQueue(engine)
    job_id: Optional[str] = None
    if is_new_event:
        job_id = queue.enqueue(
            "revocation.execute",
            {
                "database_url": config.database_url,
                "cas_root": config.resolved_cas_root,
                "event_id": event.event_id,
                "attempt_rebuild": body.rebuild_enabled,
                "verification_suite": body.verification_suite,
                "attestation_requested": body.attestation_requested,
                "sign_requested": body.sign_requested,
                "attestation_signing_key_path": config.attestation_signing_key_path,
                "attestation_public_key_path": config.attestation_public_key_path,
                "actor": actor,
            },
            idempotency_key=f"job-for-{event.event_id}",
            actor_id=actor,
        )

    if idempotency_key:
        record(
            session, key=idempotency_key, actor=actor, scope="revocation.submit", request_digest=digest, response_reference=event.event_id
        )

    return {
        **_event_to_dict(event),
        "job_id": job_id,
        "events_url": f"/api/v1/events/stream?job_id={job_id}" if job_id else None,
        "idempotent_replay": not is_new_event,
    }


@router.get("/{revocation_id}")
def get_revocation(
    revocation_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        event = ws.revocations.get(revocation_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    return _event_to_dict(event)


@router.get("/{revocation_id}/targets")
def get_revocation_targets(
    revocation_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        event = ws.revocations.get(revocation_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    targets = [{"artifact_id": r, "action": "revoke", "evidence_class": "recorded"} for r in event.roots]
    targets += [
        {"artifact_id": a, "action": "quarantine", "evidence_class": "recorded-or-replay-confirmed"}
        for a in event.quarantined
    ]
    return {"revocation_id": revocation_id, "targets": targets}


@router.get("/{revocation_id}/preview")
def get_revocation_preview(
    revocation_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        event = ws.revocations.get(revocation_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    preview = build_revocation_preview(ws, roots=event.roots, policy=event.policy)
    return preview.to_dict()


@router.post("/{revocation_id}/cancel")
def cancel_revocation(
    revocation_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("revoke")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        event = ws.revocations.get(revocation_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    try:
        event = transition(ws.revocations, event, RevocationState.CANCELLED_BEFORE_BARRIER)
    except InvalidStateTransitionError as exc:
        raise ProblemDetail(
            409,
            "Conflict",
            f"revocation {revocation_id} cannot be cancelled from state {event.state.value} "
            "(barrier-first invariant: once the barrier has applied, cancellation never automatically "
            "removes it -- see docs/adr for barrier-first semantics)",
        ) from exc
    return _event_to_dict(event)
