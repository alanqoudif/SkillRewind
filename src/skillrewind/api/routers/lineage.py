"""Recorded lineage query API (Phase C2.1 section 3).

`descendants`/`ancestors` are hard-wired to `evidence_classes=("recorded",)`
in `LineageRepository` -- this is the critical invariant (see
`tests/integration/test_service_lineage.py`): recorded closure must never
silently include inferred, replay-confirmed, replay-rejected, or unresolved
edges. `graph` is the only endpoint that exposes other evidence classes, and
it labels every edge with its evidence class explicitly rather than
collapsing them into one generic dependency type.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...persistence.service.repositories import LineageRepository
from ..deps import ProblemDetail, get_session, require_scope

router = APIRouter(prefix="/api/v1/lineage", tags=["lineage"])


def _split(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


@router.get("/{artifact_id:path}/descendants")
def get_descendants(
    artifact_id: str,
    include_roots: bool = Query(default=True),
    max_depth: Optional[int] = Query(default=None, ge=1, le=1000),
    relation_types: Optional[str] = Query(default=None, description="Comma-separated relation types"),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = LineageRepository(session)
    try:
        result = repo.descendants(
            [artifact_id], include_roots=include_roots, max_depth=max_depth, relation_types=_split(relation_types)
        )
    except ValueError as exc:
        raise ProblemDetail(422, "Unprocessable Entity", str(exc)) from None
    return {"artifact_id": artifact_id, "evidence_class": "recorded", "items": result}


@router.get("/{artifact_id:path}/ancestors")
def get_ancestors(
    artifact_id: str,
    include_roots: bool = Query(default=True),
    max_depth: Optional[int] = Query(default=None, ge=1, le=1000),
    relation_types: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = LineageRepository(session)
    try:
        result = repo.ancestors(
            [artifact_id], include_roots=include_roots, max_depth=max_depth, relation_types=_split(relation_types)
        )
    except ValueError as exc:
        raise ProblemDetail(422, "Unprocessable Entity", str(exc)) from None
    return {"artifact_id": artifact_id, "evidence_class": "recorded", "items": result}


@router.get("/path")
def get_path(
    source: str = Query(...),
    target: str = Query(...),
    evidence_classes: Optional[str] = Query(default=None),
    relation_types: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = LineageRepository(session)
    result = repo.path(source, target, evidence_classes=_split(evidence_classes), relation_types=_split(relation_types))
    if result is None:
        raise ProblemDetail(404, "Not Found", f"no path found between {source!r} and {target!r}")
    return result


@router.get("/{artifact_id:path}/graph")
def get_graph(
    artifact_id: str,
    evidence_classes: Optional[str] = Query(default=None),
    relation_types: Optional[str] = Query(default=None),
    max_depth: Optional[int] = Query(default=None, ge=1, le=1000),
    session: Session = Depends(get_session),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    repo = LineageRepository(session)
    return repo.graph(
        artifact_id, evidence_classes=_split(evidence_classes), relation_types=_split(relation_types), max_depth=max_depth
    )


@router.get("/{artifact_id:path}/cycles")
def get_cycles(
    artifact_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    repo = LineageRepository(session)
    return {"artifact_id": artifact_id, "has_cycle": repo.has_cycle_through(artifact_id)}
