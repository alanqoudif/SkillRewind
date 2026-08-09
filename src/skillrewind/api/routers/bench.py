"""POST/GET /api/v1/bench/runs[...]. Enqueues the real benchmark.run job handler."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ...jobs.handlers import parse_summary
from ...jobs.queue import JobQueue
from ..deps import ProblemDetail, get_engine, get_session, require_scope
from ..idempotency import IdempotencyConflict, check, record

router = APIRouter(prefix="/api/v1/bench", tags=["bench"])

_ALLOWED_PRESETS = {"smoke", "ci", "research", "paper"}


class CreateBenchRunRequest(BaseModel):
    preset: str = "smoke"
    seed: int = 42
    method: str = "static-multitrace"


@router.post("/runs", status_code=202)
def create_bench_run(
    body: CreateBenchRunRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    engine: Engine = Depends(get_engine),
    auth=Depends(require_scope("bench")),
) -> dict[str, Any]:
    if body.preset not in _ALLOWED_PRESETS:
        raise ProblemDetail(422, "Unprocessable Entity", f"preset must be one of {sorted(_ALLOWED_PRESETS)}")

    actor = auth.actor if auth is not None else "dev-mode"
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()

    if idempotency_key:
        try:
            outcome = check(session, key=idempotency_key, actor=actor, scope="bench.create_run", request_digest=digest)
        except IdempotencyConflict as exc:
            raise ProblemDetail(409, "Conflict", str(exc)) from None
        if outcome is not None:
            return {"job_id": outcome.response_reference, "status": "accepted", "idempotent_replay": True}

    queue = JobQueue(engine)
    job_id = queue.enqueue(
        "benchmark.run",
        {"preset": body.preset, "seed": body.seed, "method": body.method},
        actor_id=actor,
    )
    if idempotency_key:
        record(session, key=idempotency_key, actor=actor, scope="bench.create_run", request_digest=digest, response_reference=job_id)
    return {"job_id": job_id, "status": "accepted", "idempotent_replay": False}


@router.get("/runs")
def list_bench_runs(engine: Engine = Depends(get_engine), _auth=Depends(require_scope("read"))) -> dict[str, Any]:
    queue = JobQueue(engine)
    items = queue.list_jobs(kind="benchmark.run", limit=100)
    return {"items": items}


@router.get("/runs/{job_id}")
def get_bench_run(job_id: str, engine: Engine = Depends(get_engine), _auth=Depends(require_scope("read"))) -> dict[str, Any]:
    queue = JobQueue(engine)
    job = queue.get(job_id)
    if job is None or job["kind"] != "benchmark.run":
        raise ProblemDetail(404, "Not Found", f"no such benchmark run: {job_id}")
    return job


@router.get("/runs/{job_id}/report")
def get_bench_run_report(
    job_id: str, engine: Engine = Depends(get_engine), _auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    queue = JobQueue(engine)
    job = queue.get(job_id)
    if job is None or job["kind"] != "benchmark.run":
        raise ProblemDetail(404, "Not Found", f"no such benchmark run: {job_id}")
    if job["status"] != "succeeded" or not job["result_reference"]:
        raise ProblemDetail(409, "Conflict", f"benchmark run {job_id} has not completed yet (status={job['status']})")
    return parse_summary(job["result_reference"])
