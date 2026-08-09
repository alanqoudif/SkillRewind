"""Service-mode quarantine read API (Phase C2.3). Quarantine is keyed by
`artifact_id` in the schema (one active entry per artifact); that is also
the stable identifier this API exposes as `quarantine_id`, since introducing
a second synthetic ID for the same row would be a distinction without a
difference. Release only ever happens via `skillrewind.quarantine.service.
release_quarantine` (verified successor publication or an explicit
waiver) -- this router is read-only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.errors import NotFoundError
from ...persistence.service.workspace import ServiceWorkspace
from ..deps import ProblemDetail, get_cas, get_config, get_session, require_scope

router = APIRouter(prefix="/api/v1", tags=["quarantine"])


@router.get("/quarantine")
def list_quarantine(
    session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    entries = ws.revocations.list_quarantine()
    return {"items": [{**e, "quarantine_id": e["artifact_id"]} for e in entries]}


@router.get("/quarantine/{quarantine_id}")
def get_quarantine_entry(
    quarantine_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    entries = {e["artifact_id"]: e for e in ws.revocations.list_quarantine()}
    entry = entries.get(quarantine_id)
    if entry is None:
        raise ProblemDetail(404, "Not Found", f"no active quarantine entry for {quarantine_id!r}")
    return {**entry, "quarantine_id": entry["artifact_id"]}


@router.get("/artifacts/{artifact_id:path}/quarantine")
def get_artifact_quarantine(
    artifact_id: str, session: Session = Depends(get_session), cas: ContentAddressedStore = Depends(get_cas), config=Depends(get_config),
    auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        ws.artifacts.get(artifact_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    is_quarantined = ws.revocations.is_quarantined(artifact_id)
    entries = {e["artifact_id"]: e for e in ws.revocations.list_quarantine()}
    return {"artifact_id": artifact_id, "quarantined": is_quarantined, "entry": entries.get(artifact_id)}
