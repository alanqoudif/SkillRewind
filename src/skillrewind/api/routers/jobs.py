"""GET /api/v1/jobs, GET/POST .../jobs/{id}[/cancel|/retry]."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ...jobs.queue import InvalidJobStateError, JobNotFoundError, JobQueue
from ...persistence.service.models import Job
from ..deps import ProblemDetail, get_engine, get_session, require_scope

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _queue(engine: Engine) -> JobQueue:
    return JobQueue(engine)


@router.get("")
def list_jobs(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    engine: Engine = Depends(get_engine),
    _auth=Depends(require_scope("read")),
) -> dict[str, Any]:
    queue = _queue(engine)
    items = queue.list_jobs(status=status, kind=kind, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{job_id}")
def get_job(job_id: str, engine: Engine = Depends(get_engine), _auth=Depends(require_scope("read"))) -> dict[str, Any]:
    queue = _queue(engine)
    job = queue.get(job_id)
    if job is None:
        raise ProblemDetail(404, "Not Found", f"no such job: {job_id}")
    return job


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, engine: Engine = Depends(get_engine), _auth=Depends(require_scope("revoke"))) -> dict[str, Any]:
    queue = _queue(engine)
    try:
        queue.request_cancellation(job_id)
    except JobNotFoundError:
        raise ProblemDetail(404, "Not Found", f"no such job: {job_id}") from None
    except InvalidJobStateError as exc:
        raise ProblemDetail(409, "Conflict", str(exc)) from None
    return {"job_id": job_id, "cancellation_requested": True}


@router.post("/{job_id}/retry")
def retry_job(job_id: str, session: Session = Depends(get_session), _auth=Depends(require_scope("revoke"))) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise ProblemDetail(404, "Not Found", f"no such job: {job_id}")
    if job.status not in {"failed", "cancelled"}:
        raise ProblemDetail(409, "Conflict", f"job {job_id} is not retryable from status {job.status}")
    job.status = "queued"
    job.attempt_count = 0
    job.error_code = None
    job.sanitized_error = None
    job.cancellation_requested_at = None
    job.scheduled_at = None
    session.commit()
    return {"job_id": job_id, "status": "queued"}
