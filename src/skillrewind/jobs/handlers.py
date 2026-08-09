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

from ..cas.local import LocalCAS
from ..config import SkillRewindConfig
from ..domain.enums import EvidenceClass, ReplayVerdict
from ..domain.errors import NotFoundError
from ..lineage.service_recovery import run_candidate_recovery
from ..persistence.service.engine import build_engine
from ..replay.service_mode import VERDICT_TO_EVIDENCE_CLASS, build_domain_derivation, run_service_replay
from ..revocation.service import run_revocation
from ..verification.suites import VerificationSuite
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
    checkpoint-resumable `run_revocation` service call.

    Two payload shapes are accepted, both dispatching to the *same*
    `run_revocation` orchestration (master spec Phase C2.3 section 17: "do
    not create unnecessary job fragmentation"):

    - Lite mode: `{"workspace_dir": ..., "event_id": ...}` -- opens a
      Lite-mode `Workspace` on disk, as before.
    - Service mode: `{"database_url": ..., "cas_root": ..., "event_id": ...}`
      -- opens a `ServiceWorkspace` (Phase C2.3) backed by the real
      SQLAlchemy database/CAS, so a revocation submitted through the
      Service-mode API is executed against Service-mode persistence, never
      a separate Lite-mode workspace.

    Never duplicates quarantine/rebuild/attestation side effects across a
    crash-and-retry: `run_revocation` itself refuses to redo work already
    recorded on the persisted event, and a call on an already-terminal event
    is a complete no-op. In Service mode, after `run_revocation` returns,
    newly-produced rebuild/verification results are additionally
    materialized into the queryable `rebuild_attempts`/`verification_reports`
    tables (idempotently -- see `_materialize_rebuild_records`), and, if
    requested, a bounded attestation is built and optionally signed.
    """

    event_id = ctx.payload.get("event_id")
    if not event_id:
        raise PermanentJobError("revocation.execute requires 'event_id'")

    database_url = ctx.payload.get("database_url")
    cas_root = ctx.payload.get("cas_root")
    workspace_dir = ctx.payload.get("workspace_dir")

    suite_cfg = ctx.payload.get("verification_suite") or {}
    suite = VerificationSuite(
        suite_id=suite_cfg.get("suite_id", "default"),
        version=suite_cfg.get("version", "0.1.0"),
        canary_keys=tuple(suite_cfg.get("canary_keys", ())),
        utility_retention_threshold=suite_cfg.get("utility_retention_threshold", 0.0),
    )
    attempt_rebuild = bool(ctx.payload.get("attempt_rebuild", True))
    attestation_requested = bool(ctx.payload.get("attestation_requested", False))
    sign_requested = bool(ctx.payload.get("sign_requested", False))

    ctx.check_cancelled()

    if database_url and cas_root:
        from sqlalchemy.orm import Session

        from ..persistence.service.workspace import ServiceWorkspace

        engine = build_engine(database_url)
        cas = LocalCAS(cas_root)
        config = SkillRewindConfig(
            mode="service",
            database_url=database_url,
            cas_root=cas_root,
            attestation_signing_key_path=ctx.payload.get("attestation_signing_key_path", ""),
            attestation_public_key_path=ctx.payload.get("attestation_public_key_path", ""),
        )
        session = Session(engine)
        try:
            ws = ServiceWorkspace(session, cas, config)
            try:
                event = ws.revocations.get(event_id)
            except NotFoundError as exc:
                raise PermanentJobError(f"unknown revocation event_id {event_id!r}: {exc}") from exc

            ctx.heartbeat(current=0, total=1, message=f"resuming revocation {event_id} from state={event.state.value}")
            result = run_revocation(ws, event, verification_suite=suite, attempt_rebuild=attempt_rebuild)

            from ..revocation.attestation_service import materialize_rebuild_records

            materialize_rebuild_records(session, result)

            if attestation_requested and result.state.value.startswith("completed"):
                from ..revocation.attestation_service import build_and_persist_attestation

                build_and_persist_attestation(ws, session, config, result, sign=sign_requested)

            ctx.heartbeat(current=1, total=1, message=f"revocation {event_id} finished in state={result.state.value}")
            return f"{event_id}:{result.state.value}"
        finally:
            session.close()
            engine.dispose()

    if not workspace_dir:
        raise PermanentJobError("revocation.execute requires either ('database_url' and 'cas_root') or 'workspace_dir'")

    lite_ws = Workspace.open(workspace_dir)
    try:
        try:
            event = lite_ws.revocations.get(event_id)
        except NotFoundError as exc:
            raise PermanentJobError(f"unknown revocation event_id {event_id!r}: {exc}") from exc

        ctx.heartbeat(current=0, total=1, message=f"resuming revocation {event_id} from state={event.state.value}")
        result = run_revocation(lite_ws, event)
        ctx.heartbeat(current=1, total=1, message=f"revocation {event_id} finished in state={result.state.value}")
        return f"{event_id}:{result.state.value}"
    finally:
        lite_ws.close()


@register_handler("lineage.recover")
def run_lineage_recover_job(ctx: JobContext) -> str:
    """Runs (or resumes) Service-mode candidate recovery for an already-
    created `CandidateScoringRun` (see `POST /api/v1/lineage/recovery-runs`)
    via `skillrewind.lineage.service_recovery.run_candidate_recovery`.

    Checkpoint-resumable: candidates already persisted for this run are
    skipped on resume (idempotent insert in `CandidateRepository`), so a
    crash between candidates and a naive retry never duplicates candidates
    or audit events. Cancellation is checked between candidates -- a safe
    checkpoint -- via the job queue's cancellation flag. Exception details
    are redacted before persistence by the worker's failure path (see
    `JobQueue.fail`, which runs every error message through the same
    `Redactor` used elsewhere in this package).
    """

    from sqlalchemy.orm import Session

    database_url = ctx.payload.get("database_url")
    cas_root = ctx.payload.get("cas_root")
    run_id = ctx.payload.get("run_id")
    if not database_url or not cas_root or not run_id:
        raise PermanentJobError("lineage.recover requires 'database_url', 'cas_root', and 'run_id'")

    ctx.check_cancelled()
    engine = build_engine(database_url)
    cas = LocalCAS(cas_root)
    config = SkillRewindConfig(mode="service", database_url=database_url, cas_root=cas_root)
    session = Session(engine)
    try:
        from ..persistence.service.repositories import CandidateRepository

        try:
            CandidateRepository(session).get_run(run_id)
        except NotFoundError as exc:
            raise PermanentJobError(f"unknown recovery run_id {run_id!r}: {exc}") from exc

        def _on_progress(progress) -> None:
            ctx.heartbeat(
                current=progress.considered,
                total=progress.total,
                message=f"considered {progress.considered}/{progress.total}, found {progress.found}",
            )

        def _should_cancel() -> bool:
            return ctx.queue.is_cancellation_requested(ctx.job_id)

        run_candidate_recovery(
            session,
            cas,
            config,
            run_id=run_id,
            actor=ctx.payload.get("actor", "worker"),
            request_id=ctx.payload.get("request_id"),
            on_progress=_on_progress,
            should_cancel=_should_cancel,
        )
        return run_id
    finally:
        session.close()
        engine.dispose()


@register_handler("lineage.replay")
def run_lineage_replay_job(ctx: JobContext) -> str:
    """Runs (or resumes) a Service-mode paired-replay run (see
    `POST /api/v1/replay/runs`) via `skillrewind.replay.service_mode.
    run_service_replay`, which itself reuses the existing intervention/
    comparator/fidelity/statistics/runner-registry code unchanged.

    Checkpoint-resumable: repetitions already persisted for this run are
    reconstructed from their stored payload rather than re-executed (see
    `run_service_replay`'s `already_done` parameter), so a crash between
    repetitions and a naive retry never re-runs a repetition or duplicates
    its evidence/audit events. Final classification (`ReplayRepository.
    finalize_run`) commits the run status, the candidate's denormalized
    replay pointer, and any promoted `edges` row in one transaction, so a
    run is never observably `completed` with a missing promotion write.
    """

    from sqlalchemy.orm import Session

    database_url = ctx.payload.get("database_url")
    cas_root = ctx.payload.get("cas_root")
    replay_run_id = ctx.payload.get("replay_run_id")
    if not database_url or not cas_root or not replay_run_id:
        raise PermanentJobError("lineage.replay requires 'database_url', 'cas_root', and 'replay_run_id'")

    ctx.check_cancelled()
    engine = build_engine(database_url)
    config = SkillRewindConfig(mode="service", database_url=database_url, cas_root=cas_root)
    session = Session(engine)
    try:
        from ..persistence.service.repositories import DerivationRepository, ReplayRepository

        replays = ReplayRepository(session)
        try:
            run = replays.get_run(replay_run_id)
        except NotFoundError as exc:
            raise PermanentJobError(f"unknown replay_run_id {replay_run_id!r}: {exc}") from exc

        if run.status in ("completed", "failed", "cancelled"):
            # Already terminal (e.g. a naive retry of a completed job) --
            # a complete no-op, never re-finalized or re-audited.
            return replay_run_id

        if run.target_derivation_id is None:
            # The candidate's descendant had no recorded derivation at
            # submission time -- reconstruction inputs simply do not exist.
            # Invariant: this must classify as UNRESOLVED, never REJECTED
            # (missing inputs prove nothing about influence), and it is a
            # complete no-op on retry via the terminal-status check above.
            replays.finalize_run(
                replay_run_id,
                status="completed",
                verdict=ReplayVerdict.UNRESOLVED_MISSING_INPUTS.value,
                fidelity_overall=0.0,
                effect_estimate=None,
                error_message="candidate's descendant has no recorded derivation to reconstruct",
                promoted_evidence_class=EvidenceClass.UNRESOLVED.value,
                actor=ctx.payload.get("actor", "worker"),
                request_id=ctx.payload.get("request_id"),
            )
            return replay_run_id

        derivations = DerivationRepository(session, None)  # type: ignore[arg-type]
        try:
            deriv_row = derivations.get(run.target_derivation_id)
        except NotFoundError as exc:
            raise PermanentJobError(f"derivation {run.target_derivation_id!r} vanished: {exc}") from exc
        domain_derivation = build_domain_derivation(deriv_row, derivations.list_inputs(run.target_derivation_id))

        already_done = {
            r.repetition_index: r.payload_json
            for r in replays.list_repetitions(replay_run_id)
            if r.repetition_index is not None
        }

        def _persist(index: int, replay_id: str, verdict: str | None, payload: dict) -> None:
            replays.add_repetition_record(
                replay_run_id=replay_run_id,
                repetition_index=index,
                replay_id=replay_id,
                target_derivation_id=run.target_derivation_id,
                candidate_ancestor_id=run.ancestor_artifact_id,
                intervention_kind="present-withheld-pair",
                runner_id=run.runner_name,
                verdict=verdict,
                payload=payload,
            )

        def _checkpoint(index: int) -> None:
            replays.update_checkpoint(replay_run_id, checkpoint_repetition=index)
            ctx.heartbeat(current=index, total=run.repetitions_requested, message=f"completed repetition {index}/{run.repetitions_requested}")

        def _should_cancel() -> bool:
            return ctx.queue.is_cancellation_requested(ctx.job_id)

        result = run_service_replay(
            domain_derivation=domain_derivation,
            ancestor_artifact_id=run.ancestor_artifact_id,
            runner_name=run.runner_name,
            repetitions_requested=run.repetitions_requested,
            already_done=already_done,
            config=config,
            persist_repetition=_persist,
            checkpoint=_checkpoint,
            should_cancel=_should_cancel,
        )

        if result.cancelled:
            replays.finalize_run(replay_run_id, status="cancelled", actor=ctx.payload.get("actor", "worker"))
            return replay_run_id

        promoted_class = VERDICT_TO_EVIDENCE_CLASS.get(result.verdict) if result.verdict else None
        replays.finalize_run(
            replay_run_id,
            status="completed",
            verdict=result.verdict.value if result.verdict else None,
            fidelity_overall=result.fidelity_overall,
            effect_estimate=result.effect_estimate,
            error_message=result.error_message,
            promoted_evidence_class=promoted_class,
            actor=ctx.payload.get("actor", "worker"),
            request_id=ctx.payload.get("request_id"),
        )
        return replay_run_id
    finally:
        session.close()
        engine.dispose()
