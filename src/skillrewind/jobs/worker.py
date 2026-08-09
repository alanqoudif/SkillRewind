"""Worker loop: claims jobs, dispatches to a handler registry, honors cancellation.

Handlers are plain callables registered by job `kind`. A handler receives a
`JobContext` (job_id, payload, and cooperative helpers for heartbeat/
cancellation/progress) and either returns a JSON-serializable result
reference or raises `RetryableJobError`/`PermanentJobError`. Any other
exception is treated as retryable (the conservative default -- master spec
7.2's "retryable versus permanent error taxonomy" defaults unknown failures
to retryable rather than silently dropping work).
"""

from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .errors import PermanentJobError, RetryableJobError
from .queue import JobQueue

HandlerFunc = Callable[["JobContext"], Optional[str]]

_REGISTRY: dict[str, HandlerFunc] = {}


def register_handler(kind: str) -> Callable[[HandlerFunc], HandlerFunc]:
    def _decorator(fn: HandlerFunc) -> HandlerFunc:
        _REGISTRY[kind] = fn
        return fn

    return _decorator


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


@dataclass
class JobContext:
    job_id: str
    kind: str
    payload: dict[str, Any]
    queue: JobQueue
    worker_id: str
    lease_seconds: int

    def heartbeat(
        self, *, current: Optional[int] = None, total: Optional[int] = None, message: Optional[str] = None
    ) -> bool:
        return self.queue.heartbeat(
            self.job_id,
            self.worker_id,
            lease_seconds=self.lease_seconds,
            progress_current=current,
            progress_total=total,
            progress_message=message,
        )

    def check_cancelled(self) -> None:
        if self.queue.is_cancellation_requested(self.job_id):
            raise PermanentJobError("cancelled")


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        *,
        worker_id: Optional[str] = None,
        kinds: Optional[list[str]] = None,
        lease_seconds: int = 60,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self.queue = queue
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.kinds = kinds
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval

    def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if a job was processed."""

        self.queue.requeue_after_backoff()
        self.queue.reap_expired_leases()
        job_id = self.queue.claim(self.worker_id, kinds=self.kinds, lease_seconds=self.lease_seconds)
        if job_id is None:
            return False
        self._process(job_id)
        return True

    def run(
        self,
        *,
        poll_interval: float = 1.0,
        max_iterations: Optional[int] = None,
        stop_predicate: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Poll loop. `stop_predicate`, if given, is checked between iterations
        for graceful shutdown (spec 7.2: "graceful shutdown that stops
        claiming and releases or safely expires work" -- releasing happens
        naturally via lease expiry once this process stops heartbeating)."""

        processed = 0
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            if stop_predicate is not None and stop_predicate():
                break
            did_work = self.run_once()
            if did_work:
                processed += 1
            else:
                time.sleep(poll_interval)
        return processed

    def _process(self, job_id: str) -> None:
        job = self.queue.get(job_id)
        if job is None:
            return
        handler = _REGISTRY.get(job["kind"])
        if handler is None:
            self.queue.fail(
                job_id,
                self.worker_id,
                error_code="unknown_kind",
                error_message=f"no handler registered for job kind {job['kind']!r}",
                retryable=False,
            )
            return

        ctx = JobContext(
            job_id=job_id,
            kind=job["kind"],
            payload=job["payload"],
            queue=self.queue,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        try:
            result_reference = handler(ctx)
            self.queue.complete(job_id, self.worker_id, result_reference=result_reference)
        except PermanentJobError as exc:
            self.queue.fail(
                job_id, self.worker_id, error_code=exc.error_code, error_message=str(exc), retryable=False
            )
        except RetryableJobError as exc:
            self.queue.fail(job_id, self.worker_id, error_code=exc.error_code, error_message=str(exc), retryable=True)
        except Exception as exc:  # unknown failures default to retryable, never silently dropped
            self.queue.fail(
                job_id,
                self.worker_id,
                error_code="unhandled",
                error_message=f"{exc}\n{traceback.format_exc()[-2000:]}",
                retryable=True,
            )
