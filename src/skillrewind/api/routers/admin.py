"""POST/GET/DELETE /api/v1/admin/api-keys, GET /api/v1/admin/diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import __version__
from ...config import SkillRewindConfig
from ...persistence.service.models import ApiKey
from ..auth import SCOPES, create_api_key
from ..deps import ProblemDetail, get_config, get_session, require_scope

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class CreateApiKeyRequest(BaseModel):
    name: str
    actor: str
    scopes: list[str]
    expires_at: Optional[datetime] = None


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    plaintext: str
    prefix: str
    name: str
    scopes: list[str]


class ApiKeyResponse(BaseModel):
    key_id: str
    prefix: str
    name: str
    actor: str
    scopes: list[str]
    status: str
    created_at: Optional[datetime]
    expires_at: Optional[datetime]
    last_used_at: Optional[datetime]


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=201)
def create_key(
    body: CreateApiKeyRequest,
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("admin")),
) -> ApiKeyCreatedResponse:
    invalid = set(body.scopes) - SCOPES
    if invalid:
        raise ProblemDetail(422, "Unprocessable Entity", f"unknown scopes: {sorted(invalid)}")
    created = create_api_key(session, name=body.name, actor=body.actor, scopes=body.scopes, expires_at=body.expires_at)
    return ApiKeyCreatedResponse(
        key_id=created.key_id, plaintext=created.plaintext, prefix=created.prefix, name=created.name, scopes=created.scopes
    )


@router.get("/api-keys", response_model=list[ApiKeyResponse])
def list_keys(session: Session = Depends(get_session), _auth=Depends(require_scope("admin"))) -> list[ApiKeyResponse]:
    rows = session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return [
        ApiKeyResponse(
            key_id=r.key_id,
            prefix=r.prefix,
            name=r.name,
            actor=r.actor,
            scopes=list(r.scopes_json),
            status=r.status,
            created_at=r.created_at,
            expires_at=r.expires_at,
            last_used_at=r.last_used_at,
        )
        for r in rows
    ]


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("admin"))) -> None:
    row = session.get(ApiKey, key_id)
    if row is None:
        raise ProblemDetail(404, "Not Found", f"no such API key: {key_id}")
    row.status = "revoked"
    session.commit()


@router.get("/diagnostics")
def diagnostics(
    config: SkillRewindConfig = Depends(get_config), _auth=Depends(require_scope("admin"))
) -> dict[str, Any]:
    """Non-secret configuration + version snapshot. Never includes key hashes,
    database credentials, or provider API keys (master spec 8.5 / 12.3)."""

    return {
        "skillrewind_version": __version__,
        "mode": config.mode,
        "api_auth_disabled": config.api_auth_disabled,
        "cors_allow_origins": config.cors_allow_origins,
        "rate_limit_capacity": config.rate_limit_capacity,
        "database_configured": bool(config.database_url),
    }
