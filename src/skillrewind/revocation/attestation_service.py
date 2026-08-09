"""Service-mode materialization of rebuild/verification/attestation records
(Phase C2.3).

The reused Lite `run_revocation` orchestration (via `ServiceWorkspace`)
already writes its results as real Service-mode persistence: artifact
status, quarantine rows, derivation/edge rows, and the `RevocationEvent`
itself (with its `rebuilt`/`unresolved` lists) all land in the SQLAlchemy
database. This module additionally *projects* those results into the
dedicated, independently-queryable `rebuild_plans` / `rebuild_attempts` /
`verification_reports` / `attestations` tables, so each has a stable
resource ID reachable from its own API endpoint (master spec section 18) --
without duplicating the rebuild/verification/attestation logic itself, which
stays in `skillrewind.rebuild.*` / `skillrewind.verification.suites` /
`skillrewind.attestation.builder`.

Both functions here are idempotent by construction: re-running them against
an event/session that already has the corresponding rows is a no-op for
those rows, so a crashed-and-resumed job never creates duplicate successors,
verification reports, attestations, or signatures (master spec section 17 /
20).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..attestation.builder import build_attestation
from ..attestation.signing import sign_attestation
from ..config import SkillRewindConfig
from ..domain.models import RevocationEvent
from ..persistence.service.models import Artifact as ArtifactRow
from ..persistence.service.models import Attestation as AttestationRow
from ..persistence.service.models import RebuildAttempt as RebuildAttemptRow
from ..persistence.service.models import RebuildPlan as RebuildPlanRow
from ..persistence.service.models import VerificationReport as VerificationReportRow
from ..persistence.service.repositories import record_audit_event
from ..workspace_protocol import WorkspaceLike


def materialize_rebuild_records(session: Session, event: RevocationEvent) -> None:
    """Projects `event.rebuilt` (successful rebuild+verify) and the
    verification-failure entries of `event.unresolved` into
    `rebuild_plans`/`rebuild_attempts`/`verification_reports` rows.
    Idempotent on `successor_artifact_id` (rebuilt) / on
    `(target_artifact_id, reason)` (unresolved-verification-failed): a
    resumed job attempt that re-observes an already-materialized entry
    inserts nothing new.
    """

    for entry in event.rebuilt:
        successor = entry.get("successor")
        original = entry.get("original")
        if not successor or not original:
            continue
        existing = session.execute(
            select(RebuildAttemptRow).where(RebuildAttemptRow.successor_artifact_id == successor)
        ).scalar_one_or_none()
        if existing is not None:
            continue

        verification = entry.get("verification") or {}
        report_row = VerificationReportRow(
            report_id=f"verify-{uuid.uuid4()}",
            artifact_id=successor,
            status=verification.get("status", "unknown"),
            payload_json=verification,
        )
        session.add(report_row)

        plan_digest = None
        artifact_row = session.get(ArtifactRow, successor)
        if artifact_row is not None:
            plan_digest = ((artifact_row.metadata_json or {}).get("rebuild") or {}).get("plan", {}).get("plan_digest")

        plan_row = RebuildPlanRow(
            plan_id=f"plan-{uuid.uuid4()}",
            revocation_event_id=event.event_id,
            target_artifact_id=original,
            plan_digest=plan_digest or "unknown",
            payload_json=(artifact_row.metadata_json.get("rebuild", {}) if artifact_row is not None else {}),
        )
        session.add(plan_row)

        session.add(
            RebuildAttemptRow(
                attempt_id=f"attempt-{uuid.uuid4()}",
                plan_id=plan_row.plan_id,
                status="succeeded" if verification.get("status") == "pass" else "completed-not-published",
                successor_artifact_id=successor,
                verification_report_id=report_row.report_id,
            )
        )
        session.commit()

    for item in event.unresolved:
        if item.get("reason") != "verification-failed":
            continue
        target = item.get("target")
        if not target:
            continue
        verification = item.get("verification") or {}
        existing_report = session.execute(
            select(VerificationReportRow).where(
                VerificationReportRow.artifact_id == target, VerificationReportRow.status == verification.get("status", "unknown")
            )
        ).scalar_one_or_none()
        if existing_report is not None:
            continue
        report_row = VerificationReportRow(
            report_id=f"verify-{uuid.uuid4()}",
            artifact_id=target,
            status=verification.get("status", "failed"),
            payload_json=verification,
        )
        session.add(report_row)
        session.add(
            RebuildAttemptRow(
                attempt_id=f"attempt-{uuid.uuid4()}",
                plan_id="n/a",
                status="failed",
                successor_artifact_id=target,
                verification_report_id=report_row.report_id,
            )
        )
        session.commit()


def build_and_persist_attestation(
    ws: WorkspaceLike,
    session: Session,
    config: SkillRewindConfig,
    event: RevocationEvent,
    *,
    sign: bool,
) -> AttestationRow:
    """Builds (once) and, if requested, signs (once) a bounded attestation
    for `event`. Both steps are checkpoint-safe: a resumed call that finds an
    already-built attestation reuses it rather than rebuilding, and a
    resumed call that finds an already-signed attestation reuses that
    signature rather than re-signing."""

    existing = session.execute(select(AttestationRow).where(AttestationRow.event_id == event.event_id)).scalar_one_or_none()

    if existing is None:
        payload = build_attestation(ws, event)
        row = AttestationRow(
            attestation_id=f"attestation-{uuid.uuid4()}",
            event_id=event.event_id,
            content_digest=payload["content_digest"],
            payload_json=payload,
            signature_json=None,
        )
        session.add(row)
        session.commit()
        record_audit_event(
            session,
            event_type="attestation.built",
            actor=config.actor,
            payload={"attestation_id": row.attestation_id, "event_id": event.event_id, "content_digest": payload["content_digest"]},
            entity_id=row.attestation_id,
        )
        existing = row

    if sign and existing.signature_json is None:
        if not config.attestation_signing_key_path:
            record_audit_event(
                session,
                event_type="attestation.sign_failed",
                actor=config.actor,
                payload={"attestation_id": existing.attestation_id, "reason": "no signing key configured"},
                entity_id=existing.attestation_id,
            )
            return existing
        signed = sign_attestation(existing.payload_json, config.attestation_signing_key_path)
        existing.payload_json = signed
        existing.signature_json = signed["signature"]
        session.commit()
        record_audit_event(
            session,
            event_type="attestation.signed",
            actor=config.actor,
            payload={
                "attestation_id": existing.attestation_id,
                "public_key_fingerprint": signed["signature"]["public_key_hex"][:16],
            },
            entity_id=existing.attestation_id,
        )

    return existing
