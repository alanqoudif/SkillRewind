"""Standalone Service-mode rebuild-attempt API (Phase C2.4 gap A).

Rebuild attempts are already persisted (idempotently, per successor
artifact) by `skillrewind.revocation.attestation_service.
materialize_rebuild_records` into the `rebuild_plans` / `rebuild_attempts` /
`verification_reports` tables. This router only *reads* those rows -- it
never re-derives or fakes a response. A rebuild attempt whose plan predates
this router (e.g. a "completed-not-published"/"failed" attempt with
`plan_id="n/a"`) reports its plan as unavailable rather than a fabricated
empty plan.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...persistence.service.models import Artifact as ArtifactRow
from ...persistence.service.models import RebuildAttempt as RebuildAttemptRow
from ...persistence.service.models import RebuildPlan as RebuildPlanRow
from ...persistence.service.models import VerificationReport as VerificationReportRow
from ..deps import ProblemDetail, get_session, require_scope

router = APIRouter(prefix="/api/v1", tags=["rebuilds"])


def _get_attempt(session: Session, rebuild_id: str) -> RebuildAttemptRow:
    row = session.get(RebuildAttemptRow, rebuild_id)
    if row is None:
        raise ProblemDetail(404, "Not Found", f"rebuild attempt not found: {rebuild_id!r}")
    return row


def _get_plan(session: Session, plan_id: str) -> Optional[RebuildPlanRow]:
    if plan_id in (None, "n/a"):
        return None
    return session.get(RebuildPlanRow, plan_id)


def _output_artifact(session: Session, artifact_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not artifact_id:
        return None
    row = session.get(ArtifactRow, artifact_id)
    if row is None:
        return {"artifact_id": artifact_id, "status": "unavailable"}
    return {
        "artifact_id": row.artifact_id,
        "digest_hex": row.digest_hex,
        "kind": row.kind,
        "status": row.status,
        "storage_ref": row.storage_ref,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _attempt_to_dict(session: Session, attempt: RebuildAttemptRow) -> dict[str, Any]:
    plan = _get_plan(session, attempt.plan_id)
    failure_reason = None
    if attempt.status in ("failed", "completed-not-published"):
        report = session.get(VerificationReportRow, attempt.verification_report_id) if attempt.verification_report_id else None
        if report is not None:
            failure_reason = (report.payload_json or {}).get("status", attempt.status)
        else:
            failure_reason = attempt.status
    return {
        "rebuild_id": attempt.attempt_id,
        "status": attempt.status,
        "plan_id": attempt.plan_id if plan is not None else None,
        "source_artifact_id": plan.target_artifact_id if plan is not None else None,
        "revocation_event_id": plan.revocation_event_id if plan is not None else None,
        "recipe_digest": plan.plan_digest if plan is not None else None,
        "output_artifact": _output_artifact(session, attempt.successor_artifact_id),
        "verification_id": attempt.verification_report_id,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "failure_reason": failure_reason,
    }


@router.get("/rebuilds/{rebuild_id}")
def get_rebuild(
    rebuild_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    attempt = _get_attempt(session, rebuild_id)
    return _attempt_to_dict(session, attempt)


@router.get("/rebuilds/{rebuild_id}/support")
def get_rebuild_support(
    rebuild_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    attempt = _get_attempt(session, rebuild_id)
    plan = _get_plan(session, attempt.plan_id)
    if plan is None:
        return {"rebuild_id": rebuild_id, "clean_support": []}
    clean_support = (plan.payload_json or {}).get("plan", {}).get("clean_support", [])
    return {"rebuild_id": rebuild_id, "plan_id": plan.plan_id, "clean_support": clean_support}


@router.get("/rebuilds/{rebuild_id}/exclusions")
def get_rebuild_exclusions(
    rebuild_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    attempt = _get_attempt(session, rebuild_id)
    plan = _get_plan(session, attempt.plan_id)
    if plan is None:
        return {"rebuild_id": rebuild_id, "excluded_support": []}
    excluded = (plan.payload_json or {}).get("plan", {}).get("excluded_support", [])
    return {"rebuild_id": rebuild_id, "plan_id": plan.plan_id, "excluded_support": excluded}


@router.get("/rebuilds/{rebuild_id}/output")
def get_rebuild_output(
    rebuild_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    attempt = _get_attempt(session, rebuild_id)
    return {
        "rebuild_id": rebuild_id,
        "status": attempt.status,
        "output_artifact": _output_artifact(session, attempt.successor_artifact_id),
    }


@router.get("/rebuilds/{rebuild_id}/verification")
def get_rebuild_verification(
    rebuild_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    attempt = _get_attempt(session, rebuild_id)
    if not attempt.verification_report_id:
        raise ProblemDetail(404, "Not Found", f"rebuild attempt {rebuild_id!r} has no associated verification report")
    report = session.get(VerificationReportRow, attempt.verification_report_id)
    if report is None:
        raise ProblemDetail(404, "Not Found", f"verification report not found: {attempt.verification_report_id!r}")
    return {"verification_id": report.report_id, **(report.payload_json or {}), "status": report.status}


@router.get("/revocations/{revocation_id}/rebuilds")
def list_rebuilds_for_revocation(
    revocation_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    plan_ids = [
        row.plan_id
        for row in session.execute(
            select(RebuildPlanRow).where(RebuildPlanRow.revocation_event_id == revocation_id)
        ).scalars()
    ]
    if not plan_ids:
        return {"revocation_id": revocation_id, "rebuilds": []}
    attempts = session.execute(
        select(RebuildAttemptRow).where(RebuildAttemptRow.plan_id.in_(plan_ids))
    ).scalars()
    return {"revocation_id": revocation_id, "rebuilds": [_attempt_to_dict(session, a) for a in attempts]}
