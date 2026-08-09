"""Derivation capture API (Phase C2.1 section 2): POST /derivations,
POST .../inputs, POST .../output, and parent/child/derivation lookups by
artifact. Backed by the real `DerivationRepository`
(`skillrewind.persistence.service.repositories`) -- every write is
auditable (actor, request correlation ID, timestamp, stable entity id via
`record_audit_event`) and idempotent-replay-safe via the shared
`Idempotency-Key` mechanism used elsewhere in this API.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.enums import DERIVATION_INPUT_RELATIONS
from ...domain.errors import NotFoundError
from ...persistence.service.models import Derivation, DerivationInput
from ...persistence.service.repositories import ArtifactRepository, DerivationRepository
from ..deps import ProblemDetail, get_cas, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record

router = APIRouter(prefix="/api/v1", tags=["derivations"])


def _derivation_to_dict(row: Derivation) -> dict[str, Any]:
    return {
        "derivation_id": row.derivation_id,
        "recipe": row.recipe,
        "recipe_version": row.recipe_version,
        "target_artifact_id": row.target_artifact_id,
        "payload": row.payload_json,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }


def _input_to_dict(row: DerivationInput) -> dict[str, Any]:
    return {"parent_artifact_id": row.parent_artifact_id, "relation": row.relation}


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class CreateDerivationRequest(BaseModel):
    recipe: str = Field(..., examples=["skill-authoring-agent"])
    recipe_version: str = Field(..., examples=["1.0.0"])
    payload: dict[str, Any] = Field(default_factory=dict, examples=[{"model_id": "example-model", "tool_calls": []}])
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "recipe": "skill-authoring-agent",
                    "recipe_version": "1.0.0",
                    "payload": {"model_id": "example-model", "tool_calls": [{"tool": "write_file"}]},
                    "started_at": "2026-08-09T00:00:00Z",
                    "ended_at": "2026-08-09T00:05:00Z",
                }
            ]
        }
    }


class DerivationInputEntry(BaseModel):
    parent_artifact_id: str
    relation: str = Field(..., examples=["direct-input"])


class AddInputsRequest(BaseModel):
    inputs: list[DerivationInputEntry] = Field(
        ...,
        examples=[[{"parent_artifact_id": "skill://base@sha256:" + "0" * 64, "relation": "direct-input"}]],
    )


class SetOutputRequest(BaseModel):
    artifact_id: str


@router.post("/derivations", status_code=201)
def create_derivation(
    body: CreateDerivationRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    auth=Depends(require_scope("ingest")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"
    digest = _digest(body.model_dump())

    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="derivations.create", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None and outcome.response_reference is not None:
            try:
                repo = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
                return _derivation_to_dict(repo.get(outcome.response_reference))
            except NotFoundError:
                pass

    from datetime import datetime

    def _parse(ts: Optional[str]):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None

    repo = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
    derivation = repo.create(
        recipe=body.recipe,
        recipe_version=body.recipe_version,
        payload=body.payload,
        started_at=_parse(body.started_at),
        ended_at=_parse(body.ended_at),
        actor=actor,
        request_id=idempotency_key,
    )
    if idempotency_key:
        record(
            session,
            key=idempotency_key,
            actor=actor,
            scope="derivations.create",
            request_digest=digest,
            response_reference=derivation.derivation_id,
        )
    return _derivation_to_dict(derivation)


@router.get("/derivations/{derivation_id}")
def get_derivation(
    derivation_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    repo = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
    try:
        derivation = repo.get(derivation_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    result = _derivation_to_dict(derivation)
    result["inputs"] = [_input_to_dict(i) for i in repo.list_inputs(derivation_id)]
    return result


@router.post("/derivations/{derivation_id}/inputs", status_code=200)
def add_derivation_inputs(
    derivation_id: str,
    body: AddInputsRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    auth=Depends(require_scope("ingest")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"
    digest = _digest({"derivation_id": derivation_id, **body.model_dump()})

    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="derivations.add_inputs", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None:
            repo = DerivationRepository(session, ArtifactRepository(session, cas))
            return {"derivation_id": derivation_id, "inputs": [_input_to_dict(i) for i in repo.list_inputs(derivation_id)]}

    for entry in body.inputs:
        if entry.relation not in DERIVATION_INPUT_RELATIONS:
            raise ProblemDetail(
                422, "Unprocessable Entity", f"unknown relation {entry.relation!r}; must be one of {sorted(DERIVATION_INPUT_RELATIONS)}"
            )

    repo = DerivationRepository(session, ArtifactRepository(session, cas))
    try:
        repo.add_inputs(
            derivation_id,
            [(e.parent_artifact_id, e.relation) for e in body.inputs],
            actor=actor,
            request_id=idempotency_key,
        )
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    except ValueError as exc:
        raise ProblemDetail(422, "Unprocessable Entity", str(exc)) from None

    if idempotency_key:
        record(session, key=idempotency_key, actor=actor, scope="derivations.add_inputs", request_digest=digest, response_reference=None)
    return {"derivation_id": derivation_id, "inputs": [_input_to_dict(i) for i in repo.list_inputs(derivation_id)]}


@router.post("/derivations/{derivation_id}/output", status_code=200)
def set_derivation_output(
    derivation_id: str,
    body: SetOutputRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    cas: ContentAddressedStore = Depends(get_cas),
    auth=Depends(require_scope("ingest")),
) -> dict[str, Any]:
    actor = auth.actor if auth is not None else "dev-mode"
    digest = _digest({"derivation_id": derivation_id, **body.model_dump()})

    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="derivations.set_output", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None:
            repo = DerivationRepository(session, ArtifactRepository(session, cas))
            return _derivation_to_dict(repo.get(derivation_id))

    repo = DerivationRepository(session, ArtifactRepository(session, cas))
    try:
        derivation = repo.set_output(derivation_id, body.artifact_id, actor=actor, request_id=idempotency_key)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    except ValueError as exc:
        raise ProblemDetail(422, "Unprocessable Entity", str(exc)) from None

    if idempotency_key:
        record(session, key=idempotency_key, actor=actor, scope="derivations.set_output", request_digest=digest, response_reference=None)
    return _derivation_to_dict(derivation)


@router.get("/artifacts/{artifact_id:path}/derivation")
def get_artifact_derivation(
    artifact_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    repo = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
    try:
        derivation = repo.get_derivation_for_output(artifact_id)
    except NotFoundError as exc:
        raise ProblemDetail(404, "Not Found", str(exc)) from None
    result = _derivation_to_dict(derivation)
    result["inputs"] = [_input_to_dict(i) for i in repo.list_inputs(derivation.derivation_id)]
    return result


@router.get("/artifacts/{artifact_id:path}/parents")
def get_artifact_parents(
    artifact_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
    parents = repo.get_parents(artifact_id)
    return {"items": [_input_to_dict(p) for p in parents[:limit]], "next_cursor": None}


@router.get("/artifacts/{artifact_id:path}/children")
def get_artifact_children(
    artifact_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = DerivationRepository(session, ArtifactRepository(session, None))  # type: ignore[arg-type]
    children = repo.get_children(artifact_id)
    return {"items": children[:limit], "next_cursor": None}
