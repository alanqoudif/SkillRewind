"""Artifact ingest/retrieval, backed by the real Service-mode SQLAlchemy
repository + CAS (`skillrewind.persistence.service.repositories.ArtifactRepository`).

Upload accepts a raw request body (not JSON/base64, so large content streams
without a 33% base64 blow-up) with `kind`/`logical_name`/`mime_type` as query
parameters -- a deliberately simple real mechanism, not multipart, for this
increment. Content is bounded by `config.max_object_bytes` before it is ever
written to the CAS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.errors import NotFoundError
from ...persistence.service.models import QuarantineEntry, Waiver
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
    x_metadata: Optional[str] = Header(default=None, alias="X-Metadata"),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("ingest")),
) -> dict[str, Any]:
    content = await request.body()
    if len(content) > config.max_object_bytes:
        raise ProblemDetail(413, "Payload Too Large", f"content exceeds max_object_bytes={config.max_object_bytes}")

    metadata: Optional[dict[str, Any]] = None
    if x_metadata is not None:
        if len(x_metadata.encode("utf-8")) > _MAX_INLINE_METADATA_BYTES:
            raise ProblemDetail(413, "Payload Too Large", f"X-Metadata exceeds {_MAX_INLINE_METADATA_BYTES} bytes")
        try:
            import json as _json

            metadata = _json.loads(x_metadata)
        except ValueError as exc:
            raise ProblemDetail(422, "Unprocessable Entity", f"X-Metadata is not valid JSON: {exc}") from None
        if not isinstance(metadata, dict):
            raise ProblemDetail(422, "Unprocessable Entity", "X-Metadata must be a JSON object")

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
        artifact = repo.ingest(content, kind=kind, logical_name=logical_name, mime_type=mime_type, creator=actor, metadata=metadata)
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


@router.get("/{artifact_id:path}/resolve")
def resolve_artifact(
    artifact_id: str,
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    """Serving resolution (Phase C2.1 section 7): is `artifact_id` eligible
    for use? Side-effect free (read-only) and never resolves a revoked or
    quarantined artifact as allowed merely because an *inferred* candidate
    score exists elsewhere in the graph -- this handler never even queries
    `candidate_scores`. A verified active successor (`superseded_by`) is
    surfaced when one exists; an unrevoked waiver on a revoked/quarantined
    artifact resolves to `allowed-by-waiver`, recording which waiver/policy
    drove the decision.
    """

    repo = ArtifactRepository(session, cas)
    try:
        artifact = repo.get(artifact_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None

    now = datetime.now(timezone.utc)
    active_waiver = session.execute(
        select(Waiver).where(
            Waiver.artifact_id == artifact_id,
            Waiver.revoked.is_(False),
            (Waiver.expires_at.is_(None)) | (Waiver.expires_at > now),
        )
    ).scalar_one_or_none()

    successor_id: Optional[str] = None
    if artifact.status == "superseded" and artifact.superseded_by:
        cursor: Optional[str] = artifact.superseded_by
        seen = {artifact_id}
        while cursor and cursor not in seen:
            seen.add(cursor)
            try:
                successor = repo.get(cursor)
            except NotFoundError:
                break
            if successor.status == "active":
                successor_id = cursor
                break
            cursor = successor.superseded_by

    quarantine = session.execute(
        select(QuarantineEntry).where(QuarantineEntry.artifact_id == artifact_id, QuarantineEntry.active.is_(True))
    ).scalar_one_or_none()

    if artifact.status == "active":
        resolution, policy, evidence = "active", "default-allow-active", "artifact.status == active"
    elif active_waiver is not None:
        resolution = "allowed-by-waiver"
        policy = "waiver-override"
        evidence = f"active waiver {active_waiver.waiver_id!r}: {active_waiver.reason}"
    elif artifact.status == "revoked":
        resolution, policy, evidence = "revoked", "default-deny-revoked", "artifact.status == revoked"
    elif artifact.status == "superseded":
        # Checked before the quarantine branch below: a superseded artifact's
        # own quarantine-entry history is preserved (never deleted -- master
        # spec section 8) but is no longer the *active* reason this artifact
        # is unavailable; supersession is the more specific, terminal fact.
        resolution, policy, evidence = "superseded", "default-deny-superseded", "artifact.status == superseded"
    elif artifact.status == "quarantined" or quarantine is not None:
        resolution = "quarantined"
        policy = "default-deny-quarantined"
        evidence = (
            f"active quarantine entry, revocation_event_id={quarantine.revocation_event_id!r}"
            if quarantine is not None
            else "artifact.status == quarantined"
        )
    else:
        resolution, policy, evidence = "unavailable", "default-deny-unknown-status", f"artifact.status == {artifact.status!r}"

    return {
        "artifact_id": artifact_id,
        "resolution": resolution,
        "successor_artifact_id": successor_id,
        "policy": policy,
        "evidence": evidence,
    }


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
