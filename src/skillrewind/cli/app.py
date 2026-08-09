"""SkillRewind CLI (v0.2 + v0.3 Service-mode additions).

Preserves the v0.1 ``closure``/``attest --edges`` recorded-only commands
(see :mod:`skillrewind.cli.legacy`, invoked here as a fallback for those two
subcommands) and adds the v0.2 workspace-backed command families. ``worker``
and ``jobs`` are real (Phase B, backed by ``skillrewind.jobs``) when the
``service`` extra is installed and ``database_url`` is configured; ``serve``
(the HTTP API, Phase C) is not implemented yet and prints a clear "not
implemented" message -- see ``STATUS.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from ..adapters.agent_skills import export_skill, ingest_skill_directory
from ..attestation import (
    build_attestation,
    generate_keypair,
    render_html,
    render_markdown,
    sign_attestation,
    verify_attestation,
)
from ..capture.importer import import_jsonl
from ..domain.enums import ArtifactKind, RevocationPolicy, Severity
from ..domain.errors import SkillRewindError
from ..domain.models import InfluenceEdge
from ..lineage.candidates import recover_candidates
from ..lineage.closure import build_graph, recorded_ancestors, recorded_descendants
from ..quarantine.service import list_quarantine
from ..quarantine.waivers import create_waiver, revoke_waiver
from ..rebuild.planner import plan_rebuild
from ..rebuild.service import rebuild_artifact
from ..replay.service import run_paired_replay
from ..revocation.service import request_revocation, run_revocation
from ..verification.suites import DEFAULT_POISONED_DESCENDANT_SUITE, run_suite
from ..workspace import Workspace

NOT_IMPLEMENTED = {
    "serve": "HTTP API service mode is not implemented yet (Phase C; see docs/completion-matrix-v0.3.md).",
}


def _write_json(value: Any, output: Optional[str]) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    if output is None or output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _open_ws(args: argparse.Namespace) -> Workspace:
    return Workspace.open(getattr(args, "workspace", ".skillrewind"))


def _cmd_init(args: argparse.Namespace) -> int:
    ws = Workspace.init(args.workspace)
    print(f"Initialized SkillRewind workspace at {args.workspace} (schema {ws.schema_version})")
    ws.close()
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"workspace": args.workspace, "checks": {}}
    ws_path = Path(args.workspace)
    report["checks"]["workspace_dir_exists"] = ws_path.is_dir()
    exit_code = 0
    try:
        ws = _open_ws(args)
        report["checks"]["database_connects"] = True
        report["checks"]["schema_version"] = ws.schema_version
        report["checks"]["cas_root_writable"] = Path(ws.config.resolved_cas_root).exists()
        report["checks"]["audit_chain_valid"] = ws.audit.verify().ok
        service_check = _check_service_mode(ws.config)
        report["checks"]["service_mode"] = service_check
        if ws.config.mode == "service" and not service_check.get("schema_current", False):
            exit_code = 1
        ws.close()
    except Exception as exc:  # doctor must never crash the process
        report["checks"]["database_connects"] = False
        report["checks"]["error"] = str(exc)
        exit_code = 1
    _write_json(report, args.output)
    return exit_code


def _check_service_mode(config: Any) -> dict[str, Any]:
    """Report Service-mode schema/database readiness without ever raising.

    Returns a dict, never partial state disguised as success. If ``mode`` is
    ``"lite"`` this reports that Service mode is simply not in use -- it is
    not an error for a Lite deployment to have no PostgreSQL configured.
    """

    if config.mode != "service":
        return {"applicable": False, "reason": "mode is 'lite'; service-mode checks skipped"}
    if not config.database_url:
        return {"applicable": True, "schema_current": False, "reason": "service mode requires database_url"}
    try:
        from ..persistence.service.engine import build_engine, schema_current
    except ModuleNotFoundError:
        return {
            "applicable": True,
            "schema_current": False,
            "reason": (
                "sqlalchemy/alembic/psycopg not installed -- install the 'service' extra: "
                "pip install 'skillrewind[service]'"
            ),
        }
    try:
        engine = build_engine(config.database_url)
        is_current, detail = schema_current(engine)
        return {"applicable": True, "schema_current": is_current, "reason": detail}
    except Exception as exc:  # never let a doctor check crash the process
        return {"applicable": True, "schema_current": False, "reason": f"connection failed: {exc}"}


class ServiceModeUnavailable(Exception):
    pass


def _open_job_queue(args: argparse.Namespace):
    """Build a JobQueue for the ``worker``/``jobs`` commands.

    Requires the ``service`` extra and a migrated database at
    ``database_url`` (from ``--database-url``, ``SKILLREWIND_DATABASE_URL``,
    or ``skillrewind.toml``) -- refuses to silently create/mutate schema, per
    master spec 6.1 ("No silent schema mutation in service mode").
    """

    from ..config import load_config

    config = load_config(overrides={"database_url": getattr(args, "database_url", None)})
    if not config.database_url:
        raise ServiceModeUnavailable(
            "no database_url configured. Pass --database-url, set SKILLREWIND_DATABASE_URL, "
            "or set database_url in skillrewind.toml."
        )
    try:
        from ..jobs.queue import JobQueue
        from ..persistence.service.engine import build_engine, schema_current
    except ModuleNotFoundError as exc:
        raise ServiceModeUnavailable(
            f"service extra not installed ({exc}). Run: pip install 'skillrewind[service]'"
        ) from exc

    engine = build_engine(config.database_url)
    is_current, detail = schema_current(engine)
    if not is_current:
        raise ServiceModeUnavailable(f"database schema is not current: {detail}. Run: make db-migrate")
    return JobQueue(engine)


def _cmd_worker_run(args: argparse.Namespace) -> int:
    from ..jobs.worker import Worker

    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind worker run: {exc}\n")
        return 3
    kinds = args.kinds.split(",") if args.kinds else None
    worker = Worker(queue, kinds=kinds, lease_seconds=args.lease_seconds)
    print(f"worker {worker.worker_id} starting (kinds={kinds or 'all'}, multi_worker_safe={queue.is_multi_worker_safe})")
    processed = worker.run(poll_interval=args.poll_interval, max_iterations=args.max_iterations)
    print(f"worker {worker.worker_id} stopped after processing {processed} job(s)")
    return 0


def _cmd_worker_once(args: argparse.Namespace) -> int:
    from ..jobs.worker import Worker

    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind worker once: {exc}\n")
        return 3
    kinds = args.kinds.split(",") if args.kinds else None
    worker = Worker(queue, kinds=kinds, lease_seconds=args.lease_seconds)
    did_work = worker.run_once()
    _write_json({"processed": did_work}, args.output)
    return 0


def _cmd_jobs_list(args: argparse.Namespace) -> int:
    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind jobs list: {exc}\n")
        return 3
    jobs = queue.list_jobs(status=args.status, kind=args.kind, limit=args.limit)
    _write_json(jobs, args.output)
    return 0


def _cmd_jobs_show(args: argparse.Namespace) -> int:
    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind jobs show: {exc}\n")
        return 3
    job = queue.get(args.job_id)
    if job is None:
        sys.stderr.write(f"job not found: {args.job_id}\n")
        return 1
    job["events"] = queue.events(args.job_id)
    _write_json(job, args.output)
    return 0


def _cmd_jobs_cancel(args: argparse.Namespace) -> int:
    from ..jobs.queue import InvalidJobStateError, JobNotFoundError

    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind jobs cancel: {exc}\n")
        return 3
    try:
        queue.request_cancellation(args.job_id)
    except (JobNotFoundError, InvalidJobStateError) as exc:
        sys.stderr.write(f"skillrewind jobs cancel: {exc}\n")
        return 1
    _write_json({"job_id": args.job_id, "cancellation_requested": True}, args.output)
    return 0


def _cmd_jobs_retry(args: argparse.Namespace) -> int:
    from sqlalchemy.orm import Session

    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind jobs retry: {exc}\n")
        return 3
    from ..persistence.service.models import Job

    with Session(queue.engine) as session:
        job = session.get(Job, args.job_id)
        if job is None:
            sys.stderr.write(f"job not found: {args.job_id}\n")
            return 1
        if job.status not in {"failed", "cancelled"}:
            sys.stderr.write(f"job {args.job_id} is not in a retryable status ({job.status})\n")
            return 1
        job.status = "queued"
        job.attempt_count = 0
        job.error_code = None
        job.sanitized_error = None
        job.cancellation_requested_at = None
        job.scheduled_at = None
        session.commit()
    _write_json({"job_id": args.job_id, "status": "queued"}, args.output)
    return 0


def _cmd_jobs_reap_expired(args: argparse.Namespace) -> int:
    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind jobs reap-expired: {exc}\n")
        return 3
    reaped = queue.reap_expired_leases()
    _write_json({"reaped": reaped}, args.output)
    return 0


def _cmd_jobs_enqueue(args: argparse.Namespace) -> int:
    try:
        queue = _open_job_queue(args)
    except ServiceModeUnavailable as exc:
        sys.stderr.write(f"skillrewind jobs enqueue: {exc}\n")
        return 3
    payload = json.loads(args.payload_json) if args.payload_json else {}
    job_id = queue.enqueue(
        args.kind, payload, idempotency_key=args.idempotency_key, priority=args.priority
    )
    _write_json({"job_id": job_id}, args.output)
    return 0


def _cmd_artifact_ingest(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    content = Path(args.file).read_bytes()
    artifact = ws.ingest_artifact(
        content, kind=ArtifactKind(args.kind), logical_name=args.name,
        mime_type=args.mime_type, alias=args.alias,
    )
    _write_json(artifact.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_artifact_ingest_skill(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    artifact = ingest_skill_directory(ws, args.path, alias=args.alias)
    _write_json(artifact.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_artifact_list(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    artifacts = ws.artifacts.list(kind=args.kind, status=args.status, limit=args.limit)
    _write_json([a.to_dict() for a in artifacts], args.output)
    ws.close()
    return 0


def _cmd_artifact_show(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    artifact = ws.artifacts.get(args.artifact_id)
    _write_json(artifact.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_artifact_export(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    out = export_skill(ws, args.artifact_id, args.output_dir)
    print(f"Exported {args.artifact_id} to {out}")
    ws.close()
    return 0


def _cmd_artifact_verify(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    artifact = ws.artifacts.get(args.artifact_id)
    ok = ws.cas.verify_integrity(artifact.digest_hex)
    _write_json({"artifact_id": args.artifact_id, "integrity_ok": ok}, args.output)
    ws.close()
    return 0 if ok else 1


def _cmd_capture_import_jsonl(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    summary = import_jsonl(ws, args.file)
    _write_json(
        {"derivations_created": summary.derivations_created, "edges_created": summary.edges_created, "warnings": summary.warnings},
        args.output,
    )
    ws.close()
    return 0


def _cmd_edge_add(args: argparse.Namespace) -> int:
    from ..domain.enums import EvidenceClass, RelationType
    from ..workspace import timestamp

    ws = _open_ws(args)
    now = timestamp()
    edge = InfluenceEdge(
        source=args.source, target=args.target, relation=RelationType(args.relation),
        evidence_class=EvidenceClass.RECORDED, created_at=now, updated_at=now,
    )
    ws.edges.upsert(edge)
    _write_json(edge.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_edge_list(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    edges = ws.edges.list_all(evidence_class=args.evidence_class, status=args.status)
    _write_json([e.to_dict() for e in edges], args.output)
    ws.close()
    return 0


def _cmd_closure(args: argparse.Namespace) -> int:
    if args.edges:
        # v0.1 backward-compatible file-based mode: no workspace involved.
        from .. import graph as legacy_graph

        g = legacy_graph.RecordedLineageGraph(legacy_graph.load_edges(args.edges))
        legacy_closure = g.descendants(args.root, include_roots=not args.exclude_roots)
        legacy_induced = g.induced_edges(legacy_closure)
        _write_json(
            {
                "mode": "recorded-only",
                "roots": sorted(set(args.root)),
                "artifacts": list(legacy_closure),
                "edges": [e.as_dict() for e in legacy_induced],
                "limitations": [
                    "This result traverses only supplied recorded edges.",
                    "It does not infer hidden influence or test causal effects.",
                ],
            },
            args.output,
        )
        return 0

    ws = _open_ws(args)
    closure = recorded_descendants(ws, args.root, include_roots=not args.exclude_roots)
    graph = build_graph(ws)
    induced = graph.induced_edges(closure)
    _write_json(
        {"roots": sorted(set(args.root)), "artifacts": list(closure), "edges": [e.to_dict() for e in induced]},
        args.output,
    )
    ws.close()
    return 0


def _cmd_ancestors(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    ancestors = recorded_ancestors(ws, args.root, include_roots=not args.exclude_roots)
    _write_json({"roots": sorted(set(args.root)), "artifacts": list(ancestors)}, args.output)
    ws.close()
    return 0


def _cmd_graph_export(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    graph = build_graph(ws)
    if args.format == "mermaid":
        text = graph.to_mermaid()
        if args.output and args.output != "-":
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        _write_json(graph.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_candidates(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    results = recover_candidates(ws, args.target, persist=not args.no_persist)
    _write_json([r.to_dict() for r in results], args.output)
    ws.close()
    return 0


def _cmd_replay_run(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    outcome = run_paired_replay(
        ws, args.candidate, args.derivation, runner_name=args.runner, repetitions=args.repetitions
    )
    _write_json(
        {"replay_id": outcome.replay_id, "verdict": outcome.verdict.value,
         "fidelity_overall": outcome.fidelity_overall, "changed_keys": list(outcome.changed_keys)},
        args.output,
    )
    ws.close()
    return 0


def _cmd_replay_show(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    record = ws.replays.get(args.replay_id)
    _write_json(record, args.output)
    ws.close()
    return 0


def _cmd_revoke_plan(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    closure = recorded_descendants(ws, args.root, include_roots=True)
    _write_json(
        {
            "dry_run": True, "roots": sorted(set(args.root)), "policy": args.policy,
            "recorded_closure": list(closure), "estimated_closure_size": len(closure),
            "note": "Dry-run plan: no serving-state changes were made.",
        },
        args.output,
    )
    ws.close()
    return 0


def _cmd_revoke_start(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    budget = {}
    if args.budget:
        for item in args.budget:
            key, _, value = item.partition("=")
            budget[key] = int(value) if value.isdigit() else value
    event = request_revocation(
        ws, roots=args.root, reason=args.reason, severity=Severity(args.severity),
        policy=RevocationPolicy(args.policy), actor=ws.config.actor,
        idempotency_key=args.idempotency_key or f"cli-{'-'.join(sorted(args.root))}-{args.reason[:32]}",
        budget=budget,
    )
    event = run_revocation(ws, event, max_replay_calls=budget.get("replay_calls"))
    _write_json(event.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_revoke_status(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    event = ws.revocations.get(args.event_id)
    _write_json(event.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_quarantine_list(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    _write_json(list_quarantine(ws), args.output)
    ws.close()
    return 0


def _cmd_waiver_create(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    waiver = create_waiver(ws, args.artifact_id, actor=ws.config.actor, reason=args.reason, expires_at=args.expires_at)
    _write_json(waiver.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_waiver_revoke(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    revoke_waiver(ws, args.waiver_id, actor=ws.config.actor)
    print(f"Waiver {args.waiver_id} revoked.")
    ws.close()
    return 0


def _cmd_rebuild_plan(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    plan = plan_rebuild(ws, args.artifact_id)
    _write_json(plan.to_dict(), args.output)
    ws.close()
    return 0


def _cmd_rebuild_start(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    result = rebuild_artifact(ws, args.artifact_id, revocation_event_id=args.revocation_event)
    _write_json(
        {"plan": result.plan.to_dict(), "new_artifact": result.new_artifact.to_dict(), "fixture_output": result.fixture_output},
        args.output,
    )
    ws.close()
    return 0


def _cmd_verify_run(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    artifact = ws.artifacts.get(args.artifact_id)
    fixture_output = artifact.metadata.get("probes", {})
    report = run_suite(ws, args.artifact_id, DEFAULT_POISONED_DESCENDANT_SUITE, fixture_output=fixture_output)
    _write_json(report.to_dict(), args.output)
    ws.close()
    return 0 if report.status == "pass" else 1


def _cmd_attest_generate(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    event = ws.revocations.get(args.event)
    attestation = build_attestation(ws, event)
    _write_json(attestation, args.output)
    ws.close()
    return 0


def _cmd_attest_render(args: argparse.Namespace) -> int:
    attestation = json.loads(Path(args.attestation).read_text(encoding="utf-8"))
    text = render_html(attestation) if args.format == "html" else render_markdown(attestation)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def _cmd_attest_keygen(args: argparse.Namespace) -> int:
    keys = generate_keypair(args.output_dir)
    print(f"Generated Ed25519 keypair.\n  private: {keys.private_key_path} (mode 0600 -- keep this secret and never commit it)\n  public:  {keys.public_key_path}")
    return 0


def _cmd_attest_sign(args: argparse.Namespace) -> int:
    attestation = json.loads(Path(args.attestation).read_text(encoding="utf-8"))
    signed = sign_attestation(attestation, args.key)
    _write_json(signed, args.attestation)
    print(f"Signed {args.attestation} in place.")
    return 0


def _cmd_attest_verify(args: argparse.Namespace) -> int:
    attestation = json.loads(Path(args.attestation).read_text(encoding="utf-8"))
    outcome = verify_attestation(attestation, public_key_path=args.public_key)
    _write_json({"digest_valid": outcome.digest_valid, "signature_valid": outcome.signature_valid, "ok": outcome.ok}, args.output)
    return 0 if outcome.ok else 1


def _cmd_audit_verify(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    result = ws.audit.verify()
    _write_json({"ok": result.ok, "checked": result.checked, "error": result.error}, args.output)
    ws.close()
    return 0 if result.ok else 1


def _cmd_audit_export(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    ws.audit.export(args.output)
    print(f"Exported audit log to {args.output}")
    ws.close()
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    ws = _open_ws(args)
    artifact = ws.resolve_alias(args.alias)
    if artifact is None:
        _write_json({"alias": args.alias, "resolved": None}, args.output)
        ws.close()
        return 1
    _write_json({"alias": args.alias, "resolved": artifact.to_dict()}, args.output)
    ws.close()
    return 0


def _cmd_attest_legacy(args: argparse.Namespace) -> int:
    """v0.1 backward-compatible recorded-only attestation: ``attest --edges ...``."""

    from .. import graph as legacy_graph
    from ..attestation import recorded_attestation

    g = legacy_graph.RecordedLineageGraph(legacy_graph.load_edges(args.edges))
    _write_json(recorded_attestation(g, args.root, reason=args.reason), args.output)
    return 0


def _cmd_not_implemented(name: str):
    def handler(args: argparse.Namespace) -> int:
        sys.stderr.write(f"skillrewind {name}: not implemented in this session. {NOT_IMPLEMENTED[name]}\n")
        return 3

    return handler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillrewind",
        description="SkillRewind: recovering hidden influence lineage for verified revocation.",
    )
    parser.add_argument("--workspace", default=".skillrewind", help="Workspace directory (default: .skillrewind)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str, configure=None):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--output", help="Output JSON path; defaults to stdout.")
        if configure:
            configure(p)
        p.set_defaults(func=handler)
        return p

    add("init", _cmd_init, "Initialize a SkillRewind workspace.")
    add("doctor", _cmd_doctor, "Validate configuration, storage, and audit chain health.")
    add("serve", _cmd_not_implemented("serve"), "(serve is not implemented yet -- Phase C)")

    def _worker_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--database-url", help="Overrides SKILLREWIND_DATABASE_URL / skillrewind.toml.")
        p.add_argument("--kinds", help="Comma-separated job kinds to claim; default is all registered kinds.")
        p.add_argument("--lease-seconds", type=int, default=60)

    def _worker_run_args(p: argparse.ArgumentParser) -> None:
        _worker_args(p)
        p.add_argument("--poll-interval", type=float, default=1.0)
        p.add_argument("--max-iterations", type=int, default=None)

    add("worker-run", _cmd_worker_run, "Run the durable job worker loop.", _worker_run_args)
    add("worker-once", _cmd_worker_once, "Claim and process at most one job, then exit.", _worker_args)

    add("jobs-list", _cmd_jobs_list, "List jobs.", lambda p: (
        p.add_argument("--database-url"), p.add_argument("--status"), p.add_argument("--kind"),
        p.add_argument("--limit", type=int, default=50),
    ))
    add("jobs-show", _cmd_jobs_show, "Show one job, including its persisted event stream.", lambda p: (
        p.add_argument("--database-url"), p.add_argument("job_id"),
    ))
    add("jobs-cancel", _cmd_jobs_cancel, "Request cancellation of a job.", lambda p: (
        p.add_argument("--database-url"), p.add_argument("job_id"),
    ))
    add("jobs-retry", _cmd_jobs_retry, "Requeue a failed or cancelled job.", lambda p: (
        p.add_argument("--database-url"), p.add_argument("job_id"),
    ))
    add("jobs-reap-expired", _cmd_jobs_reap_expired, "Requeue jobs whose lease expired without a heartbeat.",
        lambda p: p.add_argument("--database-url"))
    add("jobs-enqueue", _cmd_jobs_enqueue, "Enqueue a job (operator/testing use).", lambda p: (
        p.add_argument("--database-url"), p.add_argument("--kind", required=True),
        p.add_argument("--payload-json", default="{}"), p.add_argument("--idempotency-key"),
        p.add_argument("--priority", type=int, default=0),
    ))

    add("artifact-ingest", _cmd_artifact_ingest, "Ingest raw bytes as an immutable artifact.", lambda p: (
        p.add_argument("--file", required=True), p.add_argument("--kind", required=True),
        p.add_argument("--name", required=True), p.add_argument("--mime-type", default="application/octet-stream"),
        p.add_argument("--alias"),
    ))
    add("artifact-ingest-skill", _cmd_artifact_ingest_skill, "Ingest an Agent Skills directory.", lambda p: (
        p.add_argument("path"), p.add_argument("--alias"),
    ))
    add("artifact-list", _cmd_artifact_list, "List artifacts.", lambda p: (
        p.add_argument("--kind"), p.add_argument("--status"), p.add_argument("--limit", type=int, default=100),
    ))
    add("artifact-show", _cmd_artifact_show, "Show one artifact.", lambda p: p.add_argument("artifact_id"))
    add("artifact-export", _cmd_artifact_export, "Export an artifact back to an Agent Skills directory.", lambda p: (
        p.add_argument("artifact_id"), p.add_argument("--output-dir", required=True),
    ))
    add("artifact-verify", _cmd_artifact_verify, "Verify CAS integrity for an artifact.", lambda p: p.add_argument("artifact_id"))

    add("capture-import-jsonl", _cmd_capture_import_jsonl, "Batch import a generic JSONL trace file.", lambda p: p.add_argument("--file", required=True))

    add("edge-add", _cmd_edge_add, "Add a recorded influence edge.", lambda p: (
        p.add_argument("--source", required=True), p.add_argument("--target", required=True),
        p.add_argument("--relation", default="used-as-input"),
    ))
    add("edge-list", _cmd_edge_list, "List influence edges.", lambda p: (
        p.add_argument("--evidence-class"), p.add_argument("--status"),
    ))

    add("closure", _cmd_closure, "Compute the deterministic recorded closure (workspace-based, or file-based with --edges for v0.1 compatibility).", lambda p: (
        p.add_argument("--root", action="append", required=True), p.add_argument("--exclude-roots", action="store_true"),
        p.add_argument("--edges", help="v0.1-compatible mode: JSONL/JSON edge file, no workspace involved."),
    ))
    add("attest", _cmd_attest_legacy, "(v0.1 compatible) Emit a recorded-evidence attestation from an edge file.", lambda p: (
        p.add_argument("--edges", required=True), p.add_argument("--root", action="append", required=True),
        p.add_argument("--reason", default="Recorded-lineage forensic analysis"),
    ))
    add("ancestors", _cmd_ancestors, "Compute the deterministic recorded ancestor set.", lambda p: (
        p.add_argument("--root", action="append", required=True), p.add_argument("--exclude-roots", action="store_true"),
    ))
    add("graph-export", _cmd_graph_export, "Export the recorded lineage graph.", lambda p: p.add_argument("--format", choices=["json", "mermaid"], default="json"))

    add("candidates", _cmd_candidates, "Recover hidden-lineage candidates for a target artifact.", lambda p: (
        p.add_argument("--target", required=True), p.add_argument("--no-persist", action="store_true"),
    ))

    add("replay-run", _cmd_replay_run, "Run a paired present/withheld replay.", lambda p: (
        p.add_argument("--candidate", required=True), p.add_argument("--derivation", required=True),
        p.add_argument("--runner", default="deterministic-fixture"), p.add_argument("--repetitions", type=int, default=1),
    ))
    add("replay-show", _cmd_replay_show, "Show a stored replay record.", lambda p: p.add_argument("replay_id"))

    add("revoke-plan", _cmd_revoke_plan, "Dry-run revocation plan (no serving-state changes).", lambda p: (
        p.add_argument("--root", action="append", required=True), p.add_argument("--policy", default="balanced"),
    ))
    add("revoke-start", _cmd_revoke_start, "Start a revocation.", lambda p: (
        p.add_argument("--root", action="append", required=True), p.add_argument("--policy", required=True),
        p.add_argument("--reason", required=True), p.add_argument("--severity", required=True),
        p.add_argument("--budget", action="append"), p.add_argument("--idempotency-key"),
    ))
    add("revoke-status", _cmd_revoke_status, "Show revocation event status.", lambda p: p.add_argument("event_id"))

    add("quarantine-list", _cmd_quarantine_list, "List quarantined artifacts.")
    add("waiver-create", _cmd_waiver_create, "Create a waiver.", lambda p: (
        p.add_argument("artifact_id"), p.add_argument("--reason", required=True), p.add_argument("--expires-at"),
    ))
    add("waiver-revoke", _cmd_waiver_revoke, "Revoke a waiver.", lambda p: p.add_argument("waiver_id"))

    add("rebuild-plan", _cmd_rebuild_plan, "Plan a clean-room rebuild.", lambda p: p.add_argument("artifact_id"))
    add("rebuild-start", _cmd_rebuild_start, "Execute a clean-room rebuild.", lambda p: (
        p.add_argument("artifact_id"), p.add_argument("--revocation-event"),
    ))
    add("verify-run", _cmd_verify_run, "Run the default verification suite against an artifact.", lambda p: p.add_argument("artifact_id"))

    add("attest-generate", _cmd_attest_generate, "Generate a bounded revocation attestation.", lambda p: p.add_argument("--event", required=True))
    add("attest-render", _cmd_attest_render, "Render an attestation as Markdown/HTML.", lambda p: (
        p.add_argument("attestation"), p.add_argument("--format", choices=["markdown", "html"], default="markdown"),
    ))
    add("attest-keygen", _cmd_attest_keygen, "Generate an Ed25519 keypair.", lambda p: p.add_argument("--output-dir", default=".skillrewind/keys"))
    add("attest-sign", _cmd_attest_sign, "Sign an attestation file in place.", lambda p: (
        p.add_argument("attestation"), p.add_argument("--key", required=True),
    ))
    add("attest-verify", _cmd_attest_verify, "Verify an attestation's digest and optional signature.", lambda p: (
        p.add_argument("attestation"), p.add_argument("--public-key"),
    ))

    add("audit-verify", _cmd_audit_verify, "Verify the hash-chained audit log.")
    audit_export = sub.add_parser("audit-export", help="Export the audit log as JSONL.")
    audit_export.add_argument("--output", required=True)
    audit_export.set_defaults(func=_cmd_audit_export)

    add("resolve", _cmd_resolve, "Resolve a logical alias to its active artifact.", lambda p: p.add_argument("alias"))

    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SkillRewindError as exc:
        sys.stderr.write(f"skillrewind: error: {exc}\n")
        return 2


def main() -> None:
    raise SystemExit(run())
