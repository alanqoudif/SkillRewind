"""Durable job queue + worker tests. Uses a deterministic FakeClock throughout
so lease expiry, backoff, and heartbeat timing are exact, not timing-flaky.

Covers the invariants required by master spec sections 7.5 and 18.1:
enqueue/claim, priority ordering, idempotent duplicate enqueue, heartbeat,
retryable-failure-then-success, permanent failure (dead letter), cancellation
between stages, worker crash + lease recovery, persisted progress-event
ordering, and secret redaction in errors.
"""

from __future__ import annotations

import random

import pytest

from skillrewind.jobs.clock import FakeClock
from skillrewind.jobs.errors import PermanentJobError, RetryableJobError
from skillrewind.jobs.queue import BackoffPolicy, JobQueue
from skillrewind.jobs.worker import JobContext, Worker, register_handler
from skillrewind.persistence.service.engine import build_engine, create_all


@pytest.fixture
def engine(tmp_path):
    e = build_engine(f"sqlite:///{tmp_path}/jobs.db")
    create_all(e)
    return e


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def queue(engine, clock):
    return JobQueue(engine, clock=clock, backoff=BackoffPolicy(base_seconds=10, jitter_seconds=0), rng=random.Random(0))


def test_enqueue_and_claim(queue):
    job_id = queue.enqueue("benchmark.run", {"preset": "smoke"})
    claimed = queue.claim("worker-1")
    assert claimed == job_id
    job = queue.get(job_id)
    assert job["status"] == "leased"
    assert job["lease_owner"] == "worker-1"
    assert job["attempt_count"] == 1


def test_claim_returns_none_when_empty(queue):
    assert queue.claim("worker-1") is None


def test_priority_ordering(queue):
    low = queue.enqueue("benchmark.run", {}, priority=0)
    high = queue.enqueue("benchmark.run", {}, priority=10)
    first = queue.claim("worker-1")
    assert first == high
    second = queue.claim("worker-1")
    assert second == low


def test_idempotent_enqueue_returns_same_job(queue):
    a = queue.enqueue("benchmark.run", {"preset": "smoke"}, idempotency_key="k1")
    b = queue.enqueue("benchmark.run", {"preset": "smoke"}, idempotency_key="k1")
    assert a == b
    assert len(queue.list_jobs()) == 1


def test_heartbeat_extends_lease_and_records_progress(queue, clock):
    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-1", lease_seconds=60)
    clock.advance(30)
    ok = queue.heartbeat(job_id, "worker-1", lease_seconds=60, progress_current=2, progress_total=4, progress_message="halfway")
    assert ok is True
    job = queue.get(job_id)
    assert job["progress_current"] == 2
    assert job["progress_message"] == "halfway"


def test_heartbeat_fails_for_wrong_owner(queue):
    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-1")
    assert queue.heartbeat(job_id, "worker-2") is False


def test_retryable_failure_then_success(queue, clock):
    job_id = queue.enqueue("benchmark.run", {}, max_attempts=3)
    queue.claim("worker-1")
    status = queue.fail(job_id, "worker-1", error_code="retryable", error_message="transient", retryable=True)
    assert status == "retry_wait"
    job = queue.get(job_id)
    assert job["status"] == "retry_wait"
    assert job["attempt_count"] == 1

    # Not due yet.
    assert queue.requeue_after_backoff() == 0
    clock.advance(100)
    assert queue.requeue_after_backoff() == 1

    claimed = queue.claim("worker-1")
    assert claimed == job_id
    queue.complete(job_id, "worker-1", result_reference="ok")
    assert queue.get(job_id)["status"] == "succeeded"


def test_permanent_failure_reaches_dead_letter(queue):
    job_id = queue.enqueue("benchmark.run", {}, max_attempts=3)
    queue.claim("worker-1")
    status = queue.fail(job_id, "worker-1", error_code="permanent", error_message="bad input", retryable=False)
    assert status == "failed"
    job = queue.get(job_id)
    assert job["status"] == "failed"
    assert job["completed_at"] is not None


def test_retries_exhausted_becomes_permanent(queue, clock):
    job_id = queue.enqueue("benchmark.run", {}, max_attempts=2)
    for _ in range(2):
        queue.claim("worker-1")
        status = queue.fail(job_id, "worker-1", error_code="retryable", error_message="x", retryable=True)
        clock.advance(1000)
        queue.requeue_after_backoff()
    assert status == "failed"


def test_cancellation_between_stages(queue):
    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-1")
    assert queue.is_cancellation_requested(job_id) is False
    queue.request_cancellation(job_id)
    assert queue.is_cancellation_requested(job_id) is True


def test_cancelling_terminal_job_raises(queue):
    from skillrewind.jobs.queue import InvalidJobStateError

    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-1")
    queue.complete(job_id, "worker-1")
    with pytest.raises(InvalidJobStateError):
        queue.request_cancellation(job_id)


def test_worker_crash_lease_recovery(queue, clock):
    """Simulates a worker dying mid-job: its lease expires without a
    heartbeat, and reap_expired_leases() makes the job claimable again."""

    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-dead", lease_seconds=30)
    assert queue.claim("worker-2") is None  # still leased

    clock.advance(31)  # lease has now expired
    reaped = queue.reap_expired_leases()
    assert reaped == 1
    assert queue.get(job_id)["status"] == "queued"

    claimed = queue.claim("worker-2")
    assert claimed == job_id


def test_sqlite_queue_reports_not_multi_worker_safe(queue):
    assert queue.is_multi_worker_safe is False


def test_progress_events_persisted_in_order(queue, clock):
    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-1")
    for i in range(3):
        clock.advance(1)
        queue.heartbeat(job_id, "worker-1", progress_current=i)
    events = queue.events(job_id)
    types = [e["event_type"] for e in events]
    assert types[0] == "job.enqueued"
    assert types[1] == "job.claimed"
    progress_events = [e for e in events if e["event_type"] == "job.progress"]
    assert [e["payload"]["current"] for e in progress_events] == [0, 1, 2]
    # event_id strictly increasing == persisted ordering, required for SSE resume in Phase C.
    ids = [e["event_id"] for e in events]
    assert ids == sorted(ids)


def test_secret_redacted_in_failure_and_event(queue):
    job_id = queue.enqueue("benchmark.run", {})
    queue.claim("worker-1")
    queue.fail(
        job_id,
        "worker-1",
        error_code="retryable",
        error_message="upstream call failed with Authorization: Bearer sk-abcdefghijklmnopqrstuvwx",
        retryable=True,
    )
    job = queue.get(job_id)
    assert "sk-abcdefghijklmnopqrstuvwx" not in job["sanitized_error"]
    assert "[REDACTED]" in job["sanitized_error"]


def test_list_jobs_filters_by_status_and_kind(queue):
    queue.enqueue("benchmark.run", {})
    other = queue.enqueue("other.kind", {})
    queue.claim("worker-1", kinds=["other.kind"])
    assert len(queue.list_jobs(status="queued")) == 1
    assert len(queue.list_jobs(kind="other.kind")) == 1
    assert queue.list_jobs(kind="other.kind")[0]["job_id"] == other


# -- Worker-level tests, using a fake in-process handler ----------------------


def test_worker_run_once_processes_and_completes(queue):
    calls = []

    @register_handler("test.echo")
    def _echo(ctx: JobContext) -> str:
        calls.append(ctx.payload)
        return "done"

    job_id = queue.enqueue("test.echo", {"x": 1})
    worker = Worker(queue, worker_id="w1")
    did_work = worker.run_once()
    assert did_work is True
    assert calls == [{"x": 1}]
    assert queue.get(job_id)["status"] == "succeeded"
    assert queue.get(job_id)["result_reference"] == "done"


def test_worker_retries_on_retryable_error_then_succeeds(queue, clock):
    attempts = {"n": 0}

    @register_handler("test.flaky")
    def _flaky(ctx: JobContext) -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RetryableJobError("try again")
        return "ok"

    job_id = queue.enqueue("test.flaky", {}, max_attempts=3)
    worker = Worker(queue, worker_id="w1")
    worker.run_once()
    assert queue.get(job_id)["status"] == "retry_wait"
    clock.advance(1000)
    worker.run_once()
    assert queue.get(job_id)["status"] == "succeeded"
    assert attempts["n"] == 2


def test_worker_permanent_error_does_not_retry(queue):
    @register_handler("test.permanent")
    def _perm(ctx: JobContext) -> str:
        raise PermanentJobError("nope")

    job_id = queue.enqueue("test.permanent", {}, max_attempts=5)
    worker = Worker(queue, worker_id="w1")
    worker.run_once()
    job = queue.get(job_id)
    assert job["status"] == "failed"
    assert job["attempt_count"] == 1


def test_worker_unknown_kind_fails_permanently(queue):
    job_id = queue.enqueue("no.such.kind", {})
    worker = Worker(queue, worker_id="w1")
    worker.run_once()
    job = queue.get(job_id)
    assert job["status"] == "failed"
    assert job["error_code"] == "unknown_kind"


def test_worker_cancellation_checkpoint_stops_handler(queue):
    @register_handler("test.cancellable")
    def _cancellable(ctx: JobContext) -> str:
        ctx.check_cancelled()
        return "should-not-reach"

    job_id = queue.enqueue("test.cancellable", {})
    queue.request_cancellation(job_id)  # cancellation can be requested before a worker ever claims it

    worker = Worker(queue, worker_id="w1")
    worker.run_once()
    job = queue.get(job_id)
    assert job["status"] == "failed"
    assert job["error_code"] == "permanent"
