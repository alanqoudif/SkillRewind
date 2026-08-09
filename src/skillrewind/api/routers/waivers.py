"""Service-mode waiver API (Phase C2.3): audited, time-bounded, scoped
exceptions to quarantine-based blocking. Reuses
`skillrewind.quarantine.waivers.create_waiver`/`revoke_waiver` unchanged --
this router only adds request validation (non-empty reason, finite expiry,
bounded duration unless `admin`, no waiver for a directly revoked root) and
HTTP/idempotency plumbing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.enums import LifecycleStatus
from ...domain.errors import NotFoundError, PolicyViolationError
from ...domain.models import Waiver
from ...persistence.service.workspace import ServiceWorkspace
from ...quarantine.waivers import create_waiver as create_waiver_domain
from ...quarantine.waivers import revoke_waiver as revoke_waiver_domain
from ..deps import ProblemDetail, get_cas, get_config, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record

router = APIRouter(prefix="/api/v1", tags=["waivers"])

_DEFAULT_MAX_DURATION_DAYS = 30
_ADMIN_MAX_DURATION_DAYS = 3650


def _waiver_to_dict(waiver: Waiver) -> dict[str, Any]:
    return {
        "waiver_id": waiver.waiver_id,
        "artifact_id": waiver.artifact_id,
        "actor": waiver.actor,
        "reason": waiver.reason,
        "scope": waiver.scope,
        "created_at": waiver.created_at,
        "expires_at": waiver.expires_at,
        "revocation_event_id": waiver.revocation_event_id,
        "revoked": waiver.revoked,
    }


class CreateWaiverRequest(BaseModel):
    artifact_id: str
    reason: str = Field(..., min_length=1)
    scope: str = Field(default="quarantine-release")
    expires_at: Optional[str] = None  # ISO-8601; required unless caller has `admin` scope
    revocation_event_id: Optional[str] = None


@router.post("/waivers", status_code=201)
def create_waiver_endpoint(
    body: CreateWaiverRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("waive")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"
    is_admin = auth is not None and "admin" in auth.scopes

    if not body.reason.strip():
        raise ProblemDetail(422, "Unprocessable Entity", "waiver reason must be a non-empty, bounded string")
    if len(body.reason) > 2000:
        raise ProblemDetail(422, "Unprocessable Entity", "waiver reason exceeds the 2000-character bound")

    now = datetime.now(timezone.utc)
    if body.expires_at is None and not is_admin:
        raise ProblemDetail(422, "Unprocessable Entity", "expires_at is required (waivers default to a finite expiry)")
    expires_dt: Optional[datetime] = None
    if body.expires_at is not None:
        try:
            expires_dt = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise ProblemDetail(422, "Unprocessable Entity", f"invalid expires_at: {body.expires_at!r}") from None
        if expires_dt <= now:
            raise ProblemDetail(422, "Unprocessable Entity", "expires_at must be in the future")
        max_duration = timedelta(days=_ADMIN_MAX_DURATION_DAYS if is_admin else _DEFAULT_MAX_DURATION_DAYS)
        if expires_dt - now > max_duration:
            raise ProblemDetail(
                422,
                "Unprocessable Entity",
                f"expiry exceeds the maximum allowed duration ({max_duration.days} days) for this key's scope",
            )

    ws = ServiceWorkspace(session, cas, config)
    try:
        artifact = ws.artifacts.get(body.artifact_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None

    if artifact.status == LifecycleStatus.REVOKED and not artifact.superseded_by:
        raise ProblemDetail(
            409,
            "Conflict",
            f"artifact {body.artifact_id!r} is directly revoked; a directly revoked root is not "
            "waiver-eligible by default (safe default -- see docs/adr)",
        )

    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, default=str).encode()).hexdigest()
    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="waiver.create", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None and outcome.response_reference is not None:
            waiver = ws.waivers.get(outcome.response_reference)
            return {**_waiver_to_dict(waiver), "idempotent_replay": True}

    try:
        waiver = create_waiver_domain(
            ws,
            body.artifact_id,
            actor=actor,
            reason=body.reason,
            scope=body.scope,
            expires_at=expires_dt.isoformat().replace("+00:00", "Z") if expires_dt else None,
            revocation_event_id=body.revocation_event_id,
            config=config,
        )
    except PolicyViolationError as exc:
        raise ProblemDetail(403, "Forbidden", str(exc)) from None

    if idempotency_key:
        record(session, key=idempotency_key, actor=actor, scope="waiver.create", request_digest=digest, response_reference=waiver.waiver_id)

    return {**_waiver_to_dict(waiver), "idempotent_replay": False}


@router.get("/waivers/{waiver_id}")
def get_waiver(
    waiver_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        waiver = ws.waivers.get(waiver_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    return _waiver_to_dict(waiver)


@router.get("/artifacts/{artifact_id:path}/waivers")
def list_artifact_waivers(
    artifact_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    return {"artifact_id": artifact_id, "waivers": [_waiver_to_dict(w) for w in ws.waivers.list_for_artifact(artifact_id)]}


@router.post("/waivers/{waiver_id}/revoke")
def revoke_waiver_endpoint(
    waiver_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("waive")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"
    ws = ServiceWorkspace(session, cas, config)
    try:
        ws.waivers.get(waiver_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    revoke_waiver_domain(ws, waiver_id, actor=actor)
    return _waiver_to_dict(ws.waivers.get(waiver_id))
