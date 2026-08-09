"""GET /api/v1/events/stream -- persisted-event SSE with Last-Event-ID resume.

Polls the `job_events` table (populated by `skillrewind.jobs.JobQueue`) for
new rows rather than pushing events through an in-process pubsub, so a
resumed client with `Last-Event-ID` never misses or duplicates an event --
the event log itself is the source of truth (master spec 8.7).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import Engine
from sse_starlette.sse import EventSourceResponse

from ...jobs.queue import JobQueue
from ..deps import ProblemDetail, get_engine, require_scope

router = APIRouter(prefix="/api/v1/events", tags=["events"])

_TERMINAL = {"succeeded", "failed", "cancelled"}


@router.get("/stream")
async def stream_events(
    request: Request,
    job_id: str,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    poll_interval: float = 0.2,
    engine: Engine = Depends(get_engine),
    _auth=Depends(require_scope("read")),
) -> EventSourceResponse:
    queue = JobQueue(engine)
    if queue.get(job_id) is None:
        raise ProblemDetail(404, "Not Found", f"no such job: {job_id}")

    after_id = int(last_event_id) if last_event_id else 0

    async def _generator() -> AsyncIterator[dict]:
        nonlocal after_id
        local_queue = JobQueue(engine)
        heartbeat_counter = 0
        while True:
            if await request.is_disconnected():
                break
            events = local_queue.events(job_id, after_event_id=after_id)
            for event in events:
                after_id = event["event_id"]
                yield {
                    "id": str(event["event_id"]),
                    "event": event["event_type"],
                    "data": json.dumps(event["payload"]),
                }
            job = local_queue.get(job_id)
            if job is not None and job["status"] in _TERMINAL and not events:
                break
            heartbeat_counter += 1
            if heartbeat_counter % 25 == 0:
                yield {"comment": "heartbeat"}
            await asyncio.sleep(poll_interval)

    return EventSourceResponse(_generator())
