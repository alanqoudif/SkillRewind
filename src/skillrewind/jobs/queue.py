"""Database-backed durable job queue over the Service-mode `jobs`/`job_events` tables.

Implements the semantics required by master spec section 7.2: transactional
idempotent enqueue, leased claiming, heartbeat/lease-extension, abandoned-
lease recovery, bounded exponential backoff with jitter, a retryable/
permanent error taxonomy, cancellation checkpoints, and persisted progress
events (consumed by SSE once Phase C's API exists).

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` on PostgreSQL, which
gives correct multi-worker exclusivity. SQLite has no row-level locking, so
on SQLite this queue is documented and tested as single-worker-safe only --
see ``is_multi_worker_safe`` -- consistent with Lite mode never claiming
multi-process guarantees it cannot provide (master spec section 7.2:
"SQLite single-worker support without pretending to provide multi-process
guarantees it cannot provide").
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from ..capture.redaction import Redactor
from ..persistence.service.models import Job, JobEvent
from .clock import Clock, SystemClock

_redactor = Redactor()

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass
class BackoffPolicy:
    base_seconds: float = 2.0
    max_seconds: float = 300.0
    jitter_seconds: float = 1.0

    def delay_for(self, attempt: int, *, rng: random.Random) -> float:
        exp = min(self.max_seconds, self.base_seconds * (2 ** max(0, attempt - 1)))
        return exp + rng.uniform(0, self.jitter_seconds)


class JobNotFoundError(Exception):
    pass


class InvalidJobStateError(Exception):
    pass


class JobQueue:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Optional[Clock] = None,
        backoff: Optional[BackoffPolicy] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._engine = engine
        self._clock = clock or SystemClock()
        self._backoff = backoff or BackoffPolicy()
        self._rng = rng or random.Random()

    @property
    def is_multi_worker_safe(self) -> bool:
        return self._engine.dialect.name != "sqlite"

    @property
    def engine(self) -> Engine:
        return self._engine

    # -- enqueue ---------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        priority: int = 0,
        max_attempts: int = 5,
        actor_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> str:
        """Enqueue a job. Returns the job_id. Reusing the same idempotency_key
        for the same kind returns the existing job_id rather than duplicating
        work -- this is the "idempotent enqueue" requirement, not the
        API-layer Idempotency-Key header (that is Phase C's concern and reuses
        this same guarantee)."""

        with Session(self._engine) as session:
            if idempotency_key:
                existing = session.execute(
                    select(Job).where(Job.idempotency_key == idempotency_key)
                ).scalar_one_or_none()
                if existing is not None:
                    return existing.job_id

            job_id = f"job-{uuid.uuid4()}"
            job = Job(
                job_id=job_id,
                kind=kind,
                payload_json=payload,
                status="queued",
                priority=priority,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                actor_id=actor_id,
                trace_id=trace_id,
                created_at=self._clock.now(),
            )
            session.add(job)
            session.commit()
            self._emit(session, job_id, "job.enqueued", {"kind": kind})
            session.commit()
            return job_id

    # -- claiming ----------------------------------------------------------

    def claim(self, worker_id: str, *, kinds: Optional[list[str]] = None, lease_seconds: int = 60) -> Optional[str]:
        """Claim the highest-priority, oldest eligible queued job.

        On PostgreSQL this uses `FOR UPDATE SKIP LOCKED` so concurrent workers
        never claim the same row. On SQLite the same statement shape is used,
        but SQLite has no row-level locking -- see `is_multi_worker_safe`.
        """

        now = self._clock.now()
        with Session(self._engine) as session:
            stmt = select(Job).where(Job.status == "queued")
            if kinds:
                stmt = stmt.where(Job.kind.in_(kinds))
            stmt = stmt.where((Job.scheduled_at.is_(None)) | (Job.scheduled_at <= now))
            stmt = stmt.order_by(Job.priority.desc(), Job.created_at.asc()).limit(1)
            if self._engine.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            job = session.execute(stmt).scalar_one_or_none()
            if job is None:
                return None

            job.status = "leased"
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            job.attempt_count += 1
            session.commit()
            self._emit(session, job.job_id, "job.claimed", {"worker_id": worker_id, "attempt": job.attempt_count})
            session.commit()
            return job.job_id

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        progress_current: Optional[int] = None,
        progress_total: Optional[int] = None,
        progress_message: Optional[str] = None,
    ) -> bool:
        """Extend a lease and optionally record progress. Returns False if the
        job is no longer owned by this worker (e.g. its lease already expired
        and was reaped) so the caller can abort cooperatively."""

        now = self._clock.now()
        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None or job.lease_owner != worker_id or job.status != "leased":
                return False
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            if progress_current is not None:
                job.progress_current = progress_current
            if progress_total is not None:
                job.progress_total = progress_total
            if progress_message is not None:
                job.progress_message = progress_message
            session.commit()
            if progress_message is not None or progress_current is not None:
                self._emit(
                    session,
                    job_id,
                    "job.progress",
                    {"current": job.progress_current, "total": job.progress_total, "message": job.progress_message},
                )
                session.commit()
            return True

    def is_cancellation_requested(self, job_id: str) -> bool:

        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            return job is not None and job.cancellation_requested_at is not None

    def request_cancellation(self, job_id: str) -> None:

        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status in TERMINAL_STATUSES:
                raise InvalidJobStateError(f"job {job_id} already in terminal status {job.status}")
            job.cancellation_requested_at = self._clock.now()
            session.commit()
            self._emit(session, job_id, "job.cancellation_requested", {})
            session.commit()

    def complete(self, job_id: str, worker_id: str, *, result_reference: Optional[str] = None) -> None:

        now = self._clock.now()
        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            job.status = "succeeded"
            job.completed_at = now
            job.result_reference = result_reference
            job.lease_owner = None
            job.lease_expires_at = None
            session.commit()
            self._emit(session, job_id, "job.succeeded", {"result_reference": result_reference})
            session.commit()

    def fail(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> str:
        """Record a failure. Returns the resulting status ('retry_wait' or 'failed')."""

        now = self._clock.now()
        sanitized = _redactor.redact(error_message)
        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            job.error_code = error_code
            job.sanitized_error = sanitized
            job.lease_owner = None
            job.lease_expires_at = None
            if retryable and job.attempt_count < job.max_attempts:
                delay = self._backoff.delay_for(job.attempt_count, rng=self._rng)
                job.status = "retry_wait"
                job.scheduled_at = now + timedelta(seconds=delay)
                new_status = "retry_wait"
            else:
                job.status = "failed"
                job.completed_at = now
                new_status = "failed"
            session.commit()
            self._emit(
                session,
                job_id,
                "job.failed" if new_status == "failed" else "job.retry_scheduled",
                {"error_code": error_code, "sanitized_error": sanitized, "attempt": job.attempt_count},
            )
            session.commit()
            return new_status

    def requeue_after_backoff(self) -> int:
        """Move `retry_wait` jobs whose `scheduled_at` has passed back to `queued`."""

        now = self._clock.now()
        with Session(self._engine) as session:
            result = session.execute(
                update(Job).where(Job.status == "retry_wait").where(Job.scheduled_at <= now).values(status="queued")
            )
            session.commit()
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def reap_expired_leases(self) -> int:
        """Requeue jobs whose lease expired without a heartbeat (worker crash recovery)."""

        now = self._clock.now()
        with Session(self._engine) as session:
            expired = (
                session.execute(select(Job).where(Job.status == "leased").where(Job.lease_expires_at < now))
                .scalars()
                .all()
            )
            for job in expired:
                job.status = "queued"
                job.lease_owner = None
                job.lease_expires_at = None
                self._emit(session, job.job_id, "job.lease_expired_requeued", {})
            session.commit()
            return len(expired)

    def get(self, job_id: str) -> Optional[dict[str, Any]]:

        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            return _job_to_dict(job) if job else None

    def list_jobs(
        self, *, status: Optional[str] = None, kind: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:

        with Session(self._engine) as session:
            stmt = select(Job)
            if status:
                stmt = stmt.where(Job.status == status)
            if kind:
                stmt = stmt.where(Job.kind == kind)
            stmt = stmt.order_by(Job.created_at.desc()).limit(limit).offset(offset)
            return [_job_to_dict(j) for j in session.execute(stmt).scalars().all()]

    def events(self, job_id: str, *, after_event_id: int = 0) -> list[dict[str, Any]]:

        with Session(self._engine) as session:
            stmt = (
                select(JobEvent)
                .where(JobEvent.job_id == job_id)
                .where(JobEvent.event_id > after_event_id)
                .order_by(JobEvent.event_id.asc())
            )
            return [
                {
                    "event_id": e.event_id,
                    "job_id": e.job_id,
                    "event_type": e.event_type,
                    "payload": e.payload_json,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in session.execute(stmt).scalars().all()
            ]

    def _emit(self, session: Session, job_id: str, event_type: str, payload: dict[str, Any]) -> None:

        session.add(
            JobEvent(
                job_id=job_id,
                event_type=event_type,
                payload_json=_redactor.redact(payload),
                created_at=self._clock.now(),
            )
        )


def _job_to_dict(job: Any) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status,
        "priority": job.priority,
        "payload": job.payload_json,
        "idempotency_key": job.idempotency_key,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "progress_message": job.progress_message,
        "result_reference": job.result_reference,
        "error_code": job.error_code,
        "sanitized_error": job.sanitized_error,
        "cancellation_requested_at": job.cancellation_requested_at.isoformat()
        if job.cancellation_requested_at
        else None,
    }


def _content_digest(payload: dict[str, Any]) -> str:
    from ..canonical.json import canonical_bytes

    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
