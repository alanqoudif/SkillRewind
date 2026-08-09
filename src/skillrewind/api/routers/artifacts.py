"""Artifact ingest/retrieval, backed by the real Service-mode SQLAlchemy
repository + CAS (`skillrewind.persistence.service.repositories.ArtifactRepository`).

Upload accepts a raw request body (not JSON/base64, so large content streams
without a 33% base64 blow-up) with `kind`/`logical_name`/`mime_type` as query
parameters -- a deliberately simple real mechanism, not multipart, for this
increment. Content is bounded by `config.max_object_bytes` before it is ever
written to the CAS.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.errors import NotFoundError
from ...persistence.service.repositories import ArtifactRepository
from ..auth import request_digest as compute_request_digest
from ..deps import ProblemDetail, get_cas, get_config, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

_MAX_INLINE_METADATA_BYTES = 16 * 1024


def _artifact_to_dict(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "digest_hex": artifact.digest_hex,
        "kind": artifact.kind,
        "logical_name": artifact.logical_name,
        "mime_type": artifact.mime_type,
        "byte_size": artifact.byte_size,
        "status": artifact.status,
        "creator": artifact.creator,
        "created_at": artifact.created_at.isoformat(),
        "metadata": artifact.metadata_json,
    }


@router.post("", status_code=201)
async def ingest_artifact(
    request: Request,
    kind: str = Query(...),
    logical_name: str = Query(...),
    mime_type: str = Query(default="application/octet-stream"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("ingest")),
) -> dict[str, Any]:
    content = await request.body()
    if len(content) > config.max_object_bytes:
        raise ProblemDetail(413, "Payload Too Large", f"content exceeds max_object_bytes={config.max_object_bytes}")

    actor = auth.actor if auth is not None else "dev-mode"
    digest = compute_request_digest("POST", f"/api/v1/artifacts?kind={kind}&logical_name={logical_name}", content)

    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="artifacts.ingest", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None and outcome.response_reference is not None:
            repo = ArtifactRepository(session, cas)
            try:
                return _artifact_to_dict(repo.get(outcome.response_reference))
            except NotFoundError:
                pass  # fall through and re-ingest if the prior result somehow vanished

    repo = ArtifactRepository(session, cas)
    try:
        artifact = repo.ingest(content, kind=kind, logical_name=logical_name, mime_type=mime_type, creator=actor)
    except ValueError as exc:
        raise ProblemDetail(422, "Unprocessable Entity", str(exc)) from None

    if idempotency_key:
        record(session, key=idempotency_key, actor=actor, scope="artifacts.ingest", request_digest=digest, response_reference=artifact.artifact_id)
    return _artifact_to_dict(artifact)


@router.get("")
def list_artifacts(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = ArtifactRepository(session, cas)
    rows = repo.list(limit=limit, cursor=cursor, kind=kind)
    next_cursor = ArtifactRepository.cursor_for(rows[-1]) if len(rows) == limit and rows else None
    return {"items": [_artifact_to_dict(r) for r in rows], "next_cursor": next_cursor}


@router.get("/{artifact_id:path}/content")
def get_artifact_content(
    artifact_id: str,
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    _auth=Depends(require_scope("read")),
) -> Response:
    # Registered before the plain `/{artifact_id:path}` route below: Starlette
    # matches routes in registration order, and since `:path` is a greedy
    # `.*`, a generic route registered first would swallow the `/content`
    # suffix into `artifact_id` and this route would never be reached.
    repo = ArtifactRepository(session, cas)
    try:
        artifact = repo.get(artifact_id)
        content = repo.get_content(artifact_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    return Response(
        content=content,
        media_type=artifact.mime_type,
        headers={"Content-Disposition": "attachment"},
    )


@router.get("/{artifact_id:path}")
def get_artifact(
    artifact_id: str,
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = ArtifactRepository(session, cas)
    try:
        return _artifact_to_dict(repo.get(artifact_id))
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
