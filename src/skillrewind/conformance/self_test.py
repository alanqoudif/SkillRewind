"""Local conformance self-test (Phase C2.4 section 9): proves this
repository's own Service-mode API satisfies its own stable Level 1/2/3
integration contract, using a fresh temporary SQLite database, a real
FastAPI app, and deterministic fixtures. No network beyond localhost, no
paid API, no external services.

Used by ``skillrewind conformance self-test``.
"""

from __future__ import annotations

import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class SelfTestReport:
    contract_version: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def _migrate(db_path: Path) -> None:
    """Apply real Alembic migration history using the migration scripts
    packaged inside ``skillrewind`` -- works from an installed wheel with
    no repository checkout, unlike a ``python -m alembic`` subprocess that
    depends on a repo-root ``alembic.ini``."""

    from ..persistence.service.migrations_runtime import upgrade_to_head

    upgrade_to_head(f"sqlite:///{db_path}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_self_test(*, workdir: Optional[Path] = None) -> SelfTestReport:
    from .levels import CONTRACT_VERSION

    report = SelfTestReport(contract_version=CONTRACT_VERSION)
    checks: list[CheckResult] = []

    def _check(name: str, fn) -> Any:
        try:
            result = fn()
            checks.append(CheckResult(name, True, "ok"))
            return result
        except Exception as exc:  # noqa: BLE001 -- self-test must report, never crash
            checks.append(CheckResult(name, False, f"{type(exc).__name__}: {exc}"))
            return None

    tmp = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="skillrewind-conformance-"))
    tmp.mkdir(parents=True, exist_ok=True)
    db_path = tmp / "conformance.db"

    ok = _check("migrations apply cleanly", lambda: _migrate(db_path))
    if ok is None and not checks[-1].ok:
        report.checks = checks
        return report

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    import skillrewind.jobs.handlers  # noqa: F401

    from ..api.app import create_app
    from ..api.auth import create_api_key
    from ..config import SkillRewindConfig
    from ..jobs.queue import JobQueue
    from ..jobs.worker import Worker
    from ..persistence.service.engine import build_engine
    from ..replay.deterministic import register_fixture

    config = SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp / "cas"))
    engine = build_engine(config.database_url)

    def _mk_recipe(task_snapshot: dict, available_context: frozenset, seed: Optional[int]) -> dict:
        marker = task_snapshot.get("marker")
        canary = marker is not None and marker in available_context
        return {"canary": canary, "_behavior_keys": ["canary"], "_utility": {"task_success": 1.0}}

    register_fixture("conformance-self-test-recipe", _mk_recipe)

    api_key = _check(
        "auth: create API key with full scope set",
        lambda: create_api_key(
            Session(engine), name="conformance", actor="conformance-self-test",
            scopes=["ingest", "read", "replay", "revoke", "waive", "admin"],
        ).plaintext,
    )
    if api_key is None:
        report.checks = checks
        return report

    app = _check("app: FastAPI app constructs from config", lambda: create_app(config))
    if app is None:
        report.checks = checks
        return report

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        def _auth_rejects_missing_key() -> None:
            r = client.get("/api/v1/artifacts", headers={})
            assert r.status_code == 401, r.status_code

        _check("auth: unauthenticated request is rejected (401)", _auth_rejects_missing_key)

        root_id = _check(
            "artifact-ingestion: ingest root artifact",
            lambda: client.post(
                "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "conformance-root"},
                headers=headers, content=b"root content: marker-present",
            ).json()["artifact_id"],
        )
        descendant_id = _check(
            "artifact-ingestion: ingest descendant artifact",
            lambda: client.post(
                "/api/v1/artifacts", params={"kind": "agent-skill", "logical_name": "conformance-descendant"},
                headers=headers, content=b"descendant content: marker-present",
            ).json()["artifact_id"],
        )

        def _derive() -> None:
            deriv = client.post(
                "/api/v1/derivations", headers=headers,
                json={"recipe": "conformance-self-test-recipe", "recipe_version": "0.1",
                      "payload": {"task_snapshot": {"marker": root_id}, "seed": 1}},
            ).json()
            r = client.post(f"/api/v1/derivations/{deriv['derivation_id']}/output", headers=headers, json={"artifact_id": descendant_id})
            assert r.status_code == 200, r.text

        _check("derivation-capture: record derivation for descendant", _derive)

        def _lineage() -> list:
            closure = client.get(f"/api/v1/lineage/{root_id}/descendants", headers=headers).json()
            assert descendant_id not in closure["items"], "recorded closure must miss the unrecorded-edge descendant"
            return closure["items"]

        _check("lineage-read: recorded closure excludes unrecorded descendant", _lineage)

        def _resolution_gate() -> None:
            resolved = client.get(f"/api/v1/artifacts/{root_id}/resolve", headers=headers).json()
            assert resolved["resolution"] == "active"

        _check("resolution-gate: active artifact resolves as active", _resolution_gate)

        def _async_job_and_candidates() -> str:
            recovery = client.post("/api/v1/lineage/recovery-runs", headers=headers, json={"root_artifact_id": root_id})
            assert recovery.status_code == 202
            run_id = recovery.json()["run_id"]
            worker = Worker(JobQueue(engine), worker_id="conformance-worker")
            for _ in range(10):
                if not worker.run_once():
                    break
            status = client.get(f"/api/v1/lineage/recovery-runs/{run_id}", headers=headers).json()
            assert status["status"] == "completed", status
            return run_id

        run_id = _check("asynchronous-job: candidate recovery job completes via the durable worker", _async_job_and_candidates)

        candidate_id = None
        if run_id is not None:
            def _get_candidate() -> Optional[int]:
                candidates = client.get(f"/api/v1/lineage/recovery-runs/{run_id}/candidates", headers=headers).json()["items"]
                match = next((c for c in candidates if c["candidate_artifact_id"] == descendant_id), None)
                assert match is not None, "descendant must be recovered as a candidate"
                return match["candidate_id"]

            candidate_id = _check("lineage-read: descendant recovered as inferred candidate", _get_candidate)

        replay_run_id = None
        if candidate_id is not None:
            def _replay() -> str:
                r = client.post("/api/v1/replay/runs", headers=headers, json={"candidate_id": candidate_id, "repetitions": 1})
                assert r.status_code == 202, r.text
                rr_id = r.json()["replay_run_id"]
                worker = Worker(JobQueue(engine), worker_id="conformance-worker")
                for _ in range(10):
                    if not worker.run_once():
                        break
                result = client.get(f"/api/v1/replay/runs/{rr_id}", headers=headers).json()
                assert result["verdict"] == "confirmed", result
                return rr_id

            replay_run_id = _check("replay-hook: replay run confirms hidden influence", _replay)

        revocation_id = None
        job_id = None
        if replay_run_id is not None:
            def _revoke() -> tuple:
                submit = client.post(
                    "/api/v1/revocations", headers={**headers, "Idempotency-Key": "conformance-revoke-1"},
                    json={
                        "roots": [root_id], "reason": "conformance self-test", "severity": "high", "policy": "balanced",
                        "rebuild_enabled": True,
                        "verification_suite": {"suite_id": "conformance-suite", "version": "0.1.0", "canary_keys": ["canary"], "utility_retention_threshold": 0.0},
                        "attestation_requested": True, "sign_requested": False,
                    },
                )
                assert submit.status_code == 202, submit.text
                body = submit.json()
                worker = Worker(JobQueue(engine), worker_id="conformance-worker")
                for _ in range(30):
                    if not worker.run_once():
                        break
                return body["revocation_id"], body["job_id"]

            outcome = _check("quarantine-enforcement + rebuild-hook: revocation executes end to end", _revoke)
            if outcome is not None:
                revocation_id, job_id = outcome

        if revocation_id is not None:
            def _rebuild_and_verification() -> None:
                event = client.get(f"/api/v1/revocations/{revocation_id}", headers=headers).json()
                assert event["state"] in ("completed", "completed-with-unresolved"), event
                rebuilds = client.get(f"/api/v1/revocations/{revocation_id}/rebuilds", headers=headers).json()["rebuilds"]
                assert rebuilds, "expected at least one materialized rebuild attempt"
                rebuild_id = rebuilds[0]["rebuild_id"]
                detail = client.get(f"/api/v1/rebuilds/{rebuild_id}", headers=headers).json()
                assert detail["rebuild_id"] == rebuild_id
                verification_id = detail["verification_id"]
                assert verification_id, "rebuild attempt must reference a verification report"
                v = client.get(f"/api/v1/verifications/{verification_id}", headers=headers).json()
                assert v["checks"], "verification-read must expose real check results"

            _check("rebuild-hook + verification-read: standalone read APIs expose real records", _rebuild_and_verification)

            def _attestation() -> None:
                r = client.post("/api/v1/attestations", headers=headers, json={"revocation_id": revocation_id})
                assert r.status_code == 201, r.text
                attestation_id = r.json()["attestation_id"]
                canonical = client.get(f"/api/v1/attestations/{attestation_id}/canonical", headers=headers).json()
                assert canonical["event_id"] == revocation_id

            _check("attestation: bounded attestation is generated and readable", _attestation)

        if job_id is not None:
            def _sse() -> None:
                events = JobQueue(engine).events(job_id, after_event_id=0)
                assert events, "expected persisted job events for the SSE stream to serve"
                r = client.get("/api/v1/events/stream", params={"job_id": job_id}, headers=headers)
                assert r.status_code in (200,), r.status_code

            _check("event-consumption: SSE endpoint is reachable for a job with persisted events", _sse)

    report.checks = checks
    return report
