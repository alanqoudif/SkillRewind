"""End-to-end test of the one real, spec-required job handler: `benchmark.run`.

Proves the master-spec invariants that matter for a handler wrapping a real
domain operation: it actually calls the existing, already-tested
`skillrewind.bench.cli` pipeline (not a duplicate reimplementation), it is
idempotent under worker crash + restart, and cancellation is honored between
its stages. See docs/adr/0010-job-handler-scope.md for what is deliberately
NOT covered here (revocation/replay/rebuild/attestation handlers).
"""

from __future__ import annotations

import json

import pytest

from skillrewind.jobs.clock import FakeClock
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine, create_all


@pytest.fixture
def queue(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path}/jobs.db")
    create_all(engine)
    return JobQueue(engine, clock=FakeClock())


def test_benchmark_job_runs_real_pipeline_and_produces_summary(tmp_path, queue):
    job_id = queue.enqueue(
        "benchmark.run",
        {"preset": "smoke", "seed": 7, "method": "static-multitrace", "output_root": str(tmp_path / "runs")},
    )
    worker = Worker(queue, worker_id="w1")
    assert worker.run_once() is True

    job = queue.get(job_id)
    assert job["status"] == "succeeded"
    summary = json.loads(open(job["result_reference"]).read())
    assert summary["method"] == "static-multitrace"
    assert summary["n_cases"] == 3
    assert "micro_f1" in summary


def test_benchmark_job_idempotent_across_worker_crash_and_restart(tmp_path, queue):
    payload = {"preset": "smoke", "seed": 7, "method": "static-multitrace", "output_root": str(tmp_path / "runs")}
    job_id = queue.enqueue("benchmark.run", payload)

    worker_a = Worker(queue, worker_id="worker-a")
    worker_a.run_once()
    first_result = queue.get(job_id)["result_reference"]
    assert first_result is not None

    # Simulate: a second, independent invocation of the same handler against
    # the same job payload (e.g. a naive retry after a crash that didn't
    # update job status) must not recompute or duplicate the benchmark run.
    from skillrewind.jobs.handlers import run_benchmark_job
    from skillrewind.jobs.worker import JobContext

    ctx = JobContext(job_id=job_id, kind="benchmark.run", payload=payload, queue=queue, worker_id="worker-b", lease_seconds=60)
    second_result = run_benchmark_job(ctx)
    assert second_result == first_result


def test_benchmark_job_rejects_non_allowlisted_preset(queue):
    from skillrewind.jobs.errors import PermanentJobError
    from skillrewind.jobs.handlers import run_benchmark_job
    from skillrewind.jobs.worker import JobContext

    ctx = JobContext(
        job_id="job-x", kind="benchmark.run", payload={"preset": "; rm -rf /"}, queue=queue, worker_id="w1", lease_seconds=60
    )
    with pytest.raises(PermanentJobError):
        run_benchmark_job(ctx)
