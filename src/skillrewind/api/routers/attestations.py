"""Service-mode attestation API (Phase C2.3): build (reusing
`skillrewind.attestation.builder.build_attestation` unchanged), retrieve
canonical JSON / Markdown / HTML, sign (reusing
`skillrewind.attestation.signing.sign_attestation`), and verify (reusing
`skillrewind.attestation.verify.verify_attestation`). The private signing
key never appears in a request body -- it is read server-side from
`config.attestation_signing_key_path`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...attestation.render import render_html, render_markdown
from ...attestation.verify import verify_attestation
from ...cas.base import ContentAddressedStore
from ...domain.errors import NotFoundError
from ...persistence.service.models import Attestation as AttestationRow
from ...persistence.service.workspace import ServiceWorkspace
from ...revocation.attestation_service import build_and_persist_attestation
from ..deps import ProblemDetail, get_cas, get_config, get_session, require_scope

router = APIRouter(prefix="/api/v1/attestations", tags=["attestations"])


def _summary(row: AttestationRow) -> dict[str, Any]:
    return {
        "attestation_id": row.attestation_id,
        "event_id": row.event_id,
        "content_digest": row.content_digest,
        "signed": row.signature_json is not None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "canonical_url": f"/api/v1/attestations/{row.attestation_id}/canonical",
        "render_markdown_url": f"/api/v1/attestations/{row.attestation_id}/render?format=markdown",
        "render_html_url": f"/api/v1/attestations/{row.attestation_id}/render?format=html",
    }


class BuildAttestationRequest(BaseModel):
    revocation_id: str
    sign: bool = False


@router.post("", status_code=201)
def build_attestation_endpoint(
    body: BuildAttestationRequest,
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    config=Depends(get_config),
    auth=Depends(require_scope("revoke")),
) -> dict[str, Any]:
    ws = ServiceWorkspace(session, cas, config)
    try:
        event = ws.revocations.get(body.revocation_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    row = build_and_persist_attestation(ws, session, config, event, sign=body.sign)
    return _summary(row)


def _get_row(session: Session, attestation_id: str) -> AttestationRow:
    row = session.get(AttestationRow, attestation_id)
    if row is None:
        raise ProblemDetail(404, "Not Found", f"no such attestation: {attestation_id}")
    return row


@router.get("/{attestation_id}")
def get_attestation(
    attestation_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    return _summary(_get_row(session, attestation_id))


@router.get("/{attestation_id}/canonical")
def get_canonical(
    attestation_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> JSONResponse:
    row = _get_row(session, attestation_id)
    return JSONResponse(content=row.payload_json)


@router.get("/{attestation_id}/render")
def render_attestation(
    attestation_id: str, format: str = "markdown", session: Session = Depends(get_session), auth=Depends(require_scope("read"))
):
    row = _get_row(session, attestation_id)
    if format == "markdown":
        return PlainTextResponse(render_markdown(row.payload_json), media_type="text/markdown")
    if format == "html":
        return HTMLResponse(render_html(row.payload_json))
    raise ProblemDetail(422, "Unprocessable Entity", f"unsupported render format: {format!r} (use markdown|html)")


@router.post("/{attestation_id}/sign")
def sign_attestation_endpoint(
    attestation_id: str, session: Session = Depends(get_session), config=Depends(get_config), auth=Depends(require_scope("admin"))
) -> dict[str, Any]:
    row = _get_row(session, attestation_id)
    if row.signature_json is not None:
        return {**_summary(row), "already_signed": True}
    if not config.attestation_signing_key_path:
        raise ProblemDetail(409, "Conflict", "no attestation signing key configured on this server")
    from ...attestation.signing import sign_attestation
    from ...persistence.service.repositories import record_audit_event

    signed = sign_attestation(row.payload_json, config.attestation_signing_key_path)
    row.payload_json = signed
    row.signature_json = signed["signature"]
    session.commit()
    record_audit_event(
        session,
        event_type="attestation.signed",
        actor=auth.actor if auth is not None else config.actor,
        payload={"attestation_id": row.attestation_id, "public_key_fingerprint": signed["signature"]["public_key_hex"][:16]},
        entity_id=row.attestation_id,
    )
    return {**_summary(row), "already_signed": False}


@router.post("/{attestation_id}/verify")
def verify_attestation_endpoint(
    attestation_id: str, session: Session = Depends(get_session), config=Depends(get_config), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    row = _get_row(session, attestation_id)
    outcome = verify_attestation(row.payload_json, public_key_path=config.attestation_public_key_path or None)
    return {
        "attestation_id": attestation_id,
        "digest_valid": outcome.digest_valid,
        "signature_valid": outcome.signature_valid,
        "ok": outcome.ok,
    }
