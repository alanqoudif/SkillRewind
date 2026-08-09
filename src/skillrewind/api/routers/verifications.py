"""Standalone Service-mode verification API (Phase C2.4 gap B).

Reads the real, persisted `verification_reports` rows written by
`skillrewind.revocation.attestation_service.materialize_rebuild_records`.
Safety / utility / integrity are never collapsed into one opaque boolean:
each is derived from the report's `checks` list by `check_type`, and each
sub-view still returns the individual check results plus the overall
`status`, never a single pass/fail bit.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...persistence.service.models import VerificationReport as VerificationReportRow
from ..deps import ProblemDetail, get_session, require_scope

router = APIRouter(prefix="/api/v1", tags=["verifications"])

# check_type -> which axis it belongs to. Unclassified check types are
# reported under "integrity" (structural/provenance checks) by default
# rather than silently dropped.
_SAFETY_CHECK_TYPES = {"canary-absent"}
_UTILITY_CHECK_TYPES = {"utility-retention-threshold"}
_INTEGRITY_CHECK_TYPES = {"predecessor-closure"}


def _get_report(session: Session, verification_id: str) -> VerificationReportRow:
    row = session.get(VerificationReportRow, verification_id)
    if row is None:
        raise ProblemDetail(404, "Not Found", f"verification report not found: {verification_id!r}")
    return row


def _report_to_dict(report: VerificationReportRow) -> dict[str, Any]:
    payload = dict(report.payload_json or {})
    return {
        "verification_id": report.report_id,
        "artifact_id": report.artifact_id,
        "status": report.status,
        "suite_id": payload.get("suite_id"),
        "suite_version": payload.get("suite_version"),
        "checks": payload.get("checks", []),
        "clean_utility_score": payload.get("clean_utility_score"),
        "retained_utility_ratio": payload.get("retained_utility_ratio"),
        "limitations": payload.get("limitations", []),
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }


def _checks_by_axis(report: VerificationReportRow, check_types: set[str]) -> list[dict[str, Any]]:
    checks = (report.payload_json or {}).get("checks", [])
    return [c for c in checks if c.get("check_type") in check_types]


def _axis_status(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "not-evaluated"
    return "pass" if all(c.get("passed") for c in checks) else "fail"


@router.get("/verifications/{verification_id}")
def get_verification(
    verification_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    return _report_to_dict(_get_report(session, verification_id))


@router.get("/verifications/{verification_id}/checks")
def get_verification_checks(
    verification_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    report = _get_report(session, verification_id)
    return {"verification_id": verification_id, "checks": (report.payload_json or {}).get("checks", [])}


@router.get("/verifications/{verification_id}/safety")
def get_verification_safety(
    verification_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    report = _get_report(session, verification_id)
    checks = _checks_by_axis(report, _SAFETY_CHECK_TYPES)
    return {"verification_id": verification_id, "axis": "safety", "status": _axis_status(checks), "checks": checks}


@router.get("/verifications/{verification_id}/utility")
def get_verification_utility(
    verification_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    report = _get_report(session, verification_id)
    checks = _checks_by_axis(report, _UTILITY_CHECK_TYPES)
    payload = report.payload_json or {}
    return {
        "verification_id": verification_id,
        "axis": "utility",
        "status": _axis_status(checks),
        "clean_utility_score": payload.get("clean_utility_score"),
        "retained_utility_ratio": payload.get("retained_utility_ratio"),
        "checks": checks,
    }


@router.get("/verifications/{verification_id}/integrity")
def get_verification_integrity(
    verification_id: str, session: Session = Depends(get_session), auth=Depends(require_scope("read"))
) -> dict[str, Any]:
    report = _get_report(session, verification_id)
    checks = _checks_by_axis(report, _INTEGRITY_CHECK_TYPES)
    return {"verification_id": verification_id, "axis": "integrity", "status": _axis_status(checks), "checks": checks}
