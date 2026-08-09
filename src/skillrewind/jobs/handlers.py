"""Real job handlers. Each wraps an existing, already-tested entry point rather
than reimplementing domain logic (master spec 7.4: "A handler must call
existing domain services, not duplicate their logic").

`revocation.execute` wraps `skillrewind.revocation.service.run_revocation`,
which was made checkpoint-resumable specifically so it is safe to run through
this job layer (see `docs/adr/0010-job-handler-scope.md` for the gap this
closes, and `tests/integration/test_revocation_resumability.py` for the
crash/resume proof at the service layer). A crash between job attempts
resumes from the event's persisted state rather than restarting or
duplicating quarantine/rebuild/audit side effects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..domain.errors import NotFoundError
from ..revocation.service import run_revocation
from ..workspace import Workspace
from .errors import PermanentJobError, RetryableJobError
from .worker import JobContext, register_handler

_ALLOWED_PRESETS = {"smoke", "ci", "research", "paper"}
_ALLOWED_METHODS = {"delete-root", "recorded-closure", "static-multitrace", "exhaustive-replay"}


@register_handler("benchmark.run")
def run_benchmark_job(ctx: JobContext) -> str:
    """Runs RewindBench generate -> run -> score -> report via the existing,
    tested `skillrewind.bench.cli` module (the same code path `make
    bench-smoke` uses) as a subprocess -- never a shell, never a user-
    supplied executable path, only a fixed, allowlisted module invocation.

    Idempotent: if `summary.json` already exists in this job's run
    directory, a prior attempt already finished the work and this handler
    returns immediately without recomputing anything, so a worker crash and
    restart never runs the benchmark twice.
    """

    preset = ctx.payload.get("preset", "smoke")
    seed = int(ctx.payload.get("seed", 42))
    method = ctx.payload.get("method", "static-multitrace")
    output_root = Path(ctx.payload.get("output_root", ".runs/jobs"))

    if preset not in _ALLOWED_PRESETS:
        raise PermanentJobError(f"preset {preset!r} not in allowlist {sorted(_ALLOWED_PRESETS)}")
    if method not in _ALLOWED_METHODS:
        raise PermanentJobError(f"method {method!r} not in allowlist {sorted(_ALLOWED_METHODS)}")

    run_dir = output_root / ctx.job_id
    cases_dir = run_dir / "cases"
    result_dir = run_dir / "run"
    summary_path = result_dir / "summary.json"

    if summary_path.is_file():
        ctx.heartbeat(current=4, total=4, message="already completed (idempotent resume)")
        return str(summary_path)

    steps = [
        ["generate", "--preset", preset, "--seed", str(seed), "--output", str(cases_dir)],
        ["run", "--method", method, "--cases", str(cases_dir), "--output", str(result_dir)],
        ["score", "--run", str(result_dir)],
        ["report", "--run", str(result_dir), "--format", "markdown"],
    ]
    for index, step_args in enumerate(steps, start=1):
        ctx.check_cancelled()
        ctx.heartbeat(current=index - 1, total=len(steps), message=f"running: {step_args[0]}")
        result = subprocess.run(
            [sys.executable, "-m", "skillrewind.bench.cli", *step_args],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RetryableJobError(f"bench step {step_args[0]!r} failed: {result.stderr.strip()[-500:]}")
        if step_args[0] == "score":
            result_dir.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(result.stdout, encoding="utf-8")

    ctx.heartbeat(current=len(steps), total=len(steps), message="completed")
    return str(summary_path)


def parse_summary(result_reference: str) -> dict:
    return json.loads(Path(result_reference).read_text(encoding="utf-8"))


@register_handler("revocation.execute")
def run_revocation_job(ctx: JobContext) -> str:
    """Runs (or resumes) a revocation event to completion via the real,
    checkpoint-resumable `run_revocation` service call against a Lite-mode
    workspace on disk. Never duplicates quarantine/rebuild/attestation
    side effects across a crash-and-retry: `run_revocation` itself refuses to
    redo work already recorded on the persisted event, and a call on an
    already-terminal event is a complete no-op.
    """

    workspace_dir = ctx.payload.get("workspace_dir")
    event_id = ctx.payload.get("event_id")
    if not workspace_dir or not event_id:
        raise PermanentJobError("revocation.execute requires 'workspace_dir' and 'event_id'")

    ctx.check_cancelled()
    ws = Workspace.open(workspace_dir)
    try:
        try:
            event = ws.revocations.get(event_id)
        except NotFoundError as exc:
            raise PermanentJobError(f"unknown revocation event_id {event_id!r}: {exc}") from exc

        ctx.heartbeat(current=0, total=1, message=f"resuming revocation {event_id} from state={event.state.value}")
        result = run_revocation(ws, event)
        ctx.heartbeat(current=1, total=1, message=f"revocation {event_id} finished in state={result.state.value}")
        return f"{event_id}:{result.state.value}"
    finally:
        ws.close()
