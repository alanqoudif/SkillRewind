"""Service-mode workspace adapter (Phase C2.3).

`ServiceWorkspace` duck-types the same public surface as the Lite-mode
`skillrewind.workspace.Workspace` (`.artifacts`, `.derivations`, `.edges`,
`.replays`, `.revocations`, `.waivers`, `.audit`, `.cas`, `.config`,
`.ingest_artifact()`, `.resolve_alias()`) but is backed by the real
SQLAlchemy Service-mode schema and CAS instead of a local SQLite file.

This is the reuse boundary the milestone spec asks for: the already-tested
revocation/barrier/quarantine/waiver/rebuild/verification/attestation
*orchestration* functions in `skillrewind.revocation.service`,
`skillrewind.revocation.barrier`, `skillrewind.quarantine.*`,
`skillrewind.rebuild.*`, `skillrewind.verification.suites`, and
`skillrewind.attestation.builder` only ever call `workspace.<repo>.<method>`
-- none of them import the concrete `Workspace` class. Passing a
`ServiceWorkspace` instead runs the exact same, unmodified domain logic
against the Service-mode database, CAS, and audit trail, rather than a
second, parallel implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...config import SkillRewindConfig
from ...domain.enums import (
    ArtifactKind,
    LifecycleStatus,
    RevocationPolicy,
    RevocationState,
    Severity,
)
from ...domain.errors import NotFoundError
from ...domain.ids import build_artifact_id
from ...domain.models import Artifact as DomainArtifact
from ...domain.models import Derivation as DomainDerivation
from ...domain.models import ReplayRecord as DomainReplayRecord
from ...domain.models import RevocationEvent as DomainRevocationEvent
from ...domain.models import Waiver as DomainWaiver
from .models import Artifact as ArtifactRow
from .models import Derivation as DerivationRow
from .models import DerivationInput as DerivationInputRow
from .models import QuarantineEntry as QuarantineRow
from .models import ReplayRecord as ReplayRecordRow
from .models import RevocationEvent as RevocationEventRow
from .models import RevocationTransition as RevocationTransitionRow
from .models import Waiver as WaiverRow
from .repositories import _SCHEME_BY_KIND, ArtifactRepository, LineageRepository, _now, _parse_ts, record_audit_event


def _to_domain_artifact(row: ArtifactRow) -> DomainArtifact:
    return DomainArtifact(
        artifact_id=row.artifact_id,
        digest_hex=row.digest_hex,
        kind=ArtifactKind(row.kind),
        logical_name=row.logical_name,
        mime_type=row.mime_type,
        byte_size=row.byte_size,
        created_at=row.created_at.isoformat() if row.created_at else "",
        storage_ref=row.storage_ref,
        status=LifecycleStatus(row.status),
        creator=row.creator,
        metadata=dict(row.metadata_json or {}),
        schema_version=row.schema_version,
        supersedes=row.supersedes,
        superseded_by=row.superseded_by,
        alias=row.alias,
    )


def build_domain_derivation(row: DerivationRow, inputs: list[DerivationInputRow]) -> DomainDerivation:
    payload = row.payload_json or {}
    return DomainDerivation(
        derivation_id=row.derivation_id,
        recipe=row.recipe,
        recipe_version=row.recipe_version,
        target_artifact_id=row.target_artifact_id,
        recorded_inputs=[i.parent_artifact_id for i in inputs] or list(payload.get("recorded_inputs", [])),
        candidate_context_pool=list(payload.get("candidate_context_pool", [])),
        task_snapshot=payload.get("task_snapshot", {}),
        tool_calls=list(payload.get("tool_calls", [])),
        model_id=payload.get("model_id"),
        seed=payload.get("seed"),
        started_at=row.started_at.isoformat() if row.started_at else None,
        ended_at=row.ended_at.isoformat() if row.ended_at else None,
    )


class _ServiceArtifacts:
    def __init__(self, session: Session) -> None:
        self._repo = ArtifactRepository(session, cas=None)  # type: ignore[arg-type]
        self.session = session

    def get(self, artifact_id: str) -> DomainArtifact:
        return _to_domain_artifact(self._repo.get(artifact_id))

    def list(self, *, limit: int = 100, **_ignored: Any) -> list[DomainArtifact]:
        return [_to_domain_artifact(r) for r in self._repo.list_unbounded(limit=limit)]

    def find_by_alias(self, alias: str) -> Optional[DomainArtifact]:
        row = self._repo.find_by_alias(alias)
        return _to_domain_artifact(row) if row is not None else None

    def set_status(self, artifact_id: str, status: LifecycleStatus) -> None:
        self._repo.set_status(artifact_id, status.value)

    def set_alias(self, artifact_id: str, alias: str) -> None:
        self._repo.set_alias(artifact_id, alias)

    def clear_alias(self, alias: str) -> None:
        self._repo.clear_alias(alias)

    def set_superseded_by(self, artifact_id: str, successor_id: str) -> None:
        self._repo.set_superseded_by(artifact_id, successor_id)


class _ServiceDerivations:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, derivation_id: str) -> DomainDerivation:
        row = self.session.get(DerivationRow, derivation_id)
        if row is None:
            raise NotFoundError(f"derivation not found: {derivation_id}")
        inputs = list(
            self.session.execute(
                select(DerivationInputRow).where(DerivationInputRow.derivation_id == derivation_id)
            ).scalars()
        )
        return build_domain_derivation(row, inputs)

    def find_by_target(self, target_artifact_id: str) -> Optional[DomainDerivation]:
        row = self.session.execute(
            select(DerivationRow)
            .where(DerivationRow.target_artifact_id == target_artifact_id)
            .order_by(DerivationRow.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        inputs = list(
            self.session.execute(
                select(DerivationInputRow).where(DerivationInputRow.derivation_id == row.derivation_id)
            ).scalars()
        )
        return build_domain_derivation(row, inputs)

    def upsert(self, derivation: DomainDerivation) -> None:
        """Used by the reused Lite `rebuild_artifact()` to record the
        synthetic derivation for a freshly rebuilt successor artifact. Its
        `recorded_inputs`/`candidate_context_pool` are the frozen clean
        support set from the rebuild plan -- persisted into `payload_json`
        here; the corresponding `edges` rows are written separately by
        `rebuild_artifact()` itself via `workspace.edges.upsert`."""

        existing = self.session.get(DerivationRow, derivation.derivation_id)
        payload = {
            "recorded_inputs": derivation.recorded_inputs,
            "candidate_context_pool": derivation.candidate_context_pool,
            "task_snapshot": derivation.task_snapshot,
            "tool_calls": derivation.tool_calls,
            "model_id": derivation.model_id,
            "seed": derivation.seed,
        }
        if existing is None:
            row = DerivationRow(
                derivation_id=derivation.derivation_id,
                recipe=derivation.recipe,
                recipe_version=derivation.recipe_version,
                target_artifact_id=derivation.target_artifact_id,
                payload_json=payload,
                started_at=_parse_ts(derivation.started_at),
                ended_at=_parse_ts(derivation.ended_at),
            )
            self.session.add(row)
        else:
            existing.payload_json = payload
            existing.target_artifact_id = derivation.target_artifact_id
            existing.ended_at = _parse_ts(derivation.ended_at)
        self.session.commit()


class _ServiceReplays:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, record: DomainReplayRecord) -> None:
        row = ReplayRecordRow(
            replay_id=record.replay_id,
            target_derivation_id=record.target_derivation_id,
            candidate_ancestor_id=record.candidate_ancestor_id,
            intervention_kind=record.intervention_kind,
            runner_id=record.model_identity,
            verdict=record.verdict.value if record.verdict else None,
            payload_json=record.to_dict(),
            replay_run_id=None,
            repetition_index=None,
        )
        self.session.add(row)
        self.session.commit()

    def get(self, replay_id: str) -> dict:
        row = self.session.execute(
            select(ReplayRecordRow).where(ReplayRecordRow.replay_id == replay_id)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"replay record not found: {replay_id}")
        return dict(row.payload_json or {})


def _row_to_event(row: RevocationEventRow) -> DomainRevocationEvent:
    data = dict(row.payload_json)
    data["severity"] = Severity(data["severity"])
    data["policy"] = RevocationPolicy(data["policy"])
    data["state"] = RevocationState(data["state"])
    return DomainRevocationEvent(**data)


class _ServiceRevocations:
    """Mirrors `skillrewind.persistence.repositories.RevocationRepository`'s
    interface exactly (Phase C2.3 reuse boundary), backed by the
    `revocation_events` / `revocation_transitions` / `quarantine` tables that
    already exist in the Service-mode schema. `payload_json` is the sole
    source of truth for a domain event, mirroring how Lite mode treats its
    own `payload_json` column -- the indexed `state`/`policy`/`severity`
    columns are denormalized for querying only.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, event: DomainRevocationEvent) -> None:
        row = RevocationEventRow(
            event_id=event.event_id,
            idempotency_key=event.idempotency_key,
            state=event.state.value,
            policy=event.policy.value,
            severity=event.severity.value,
            actor=event.actor,
            payload_json=event.to_dict(),
            created_at=_parse_ts(event.created_at) or _now(),
            completed_at=_parse_ts(event.completed_at),
        )
        self.session.add(row)
        self.session.commit()

    def update(self, event: DomainRevocationEvent) -> None:
        row = self.session.get(RevocationEventRow, event.event_id)
        if row is None:
            raise NotFoundError(f"revocation event not found: {event.event_id}")
        row.state = event.state.value
        row.payload_json = event.to_dict()
        row.completed_at = _parse_ts(event.completed_at)
        self.session.commit()

    def record_transition(self, event_id: str, from_state: Optional[str], to_state: str, at: str) -> None:
        self.session.add(
            RevocationTransitionRow(event_id=event_id, from_state=from_state, to_state=to_state, at=_parse_ts(at) or _now())
        )
        self.session.commit()

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[DomainRevocationEvent]:
        row = self.session.execute(
            select(RevocationEventRow).where(RevocationEventRow.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        return _row_to_event(row) if row is not None else None

    def get(self, event_id: str) -> DomainRevocationEvent:
        row = self.session.get(RevocationEventRow, event_id)
        if row is None:
            raise NotFoundError(f"revocation event not found: {event_id}")
        return _row_to_event(row)

    def list_all(self) -> list[DomainRevocationEvent]:
        rows = self.session.execute(select(RevocationEventRow).order_by(RevocationEventRow.created_at.asc())).scalars()
        return [_row_to_event(r) for r in rows]

    def add_quarantine(self, artifact_id: str, revocation_event_id: str, reason: str, created_at: str) -> None:
        row = self.session.get(QuarantineRow, artifact_id)
        if row is None:
            row = QuarantineRow(
                artifact_id=artifact_id,
                revocation_event_id=revocation_event_id,
                reason=reason,
                active=True,
                created_at=_parse_ts(created_at) or _now(),
            )
            self.session.add(row)
        else:
            # Re-quarantine (or first-time-active) an artifact that already
            # has quarantine history -- update in place rather than
            # inserting a second row (artifact_id is the primary key) so
            # history is never silently duplicated.
            row.revocation_event_id = revocation_event_id
            row.reason = reason
            row.active = True
        self.session.commit()

    def remove_quarantine(self, artifact_id: str) -> None:
        """Marks the entry inactive rather than deleting it -- quarantine
        history must never be silently discarded (master spec section 8:
        'Never silently delete quarantine history')."""

        row = self.session.get(QuarantineRow, artifact_id)
        if row is not None:
            row.active = False
            self.session.commit()

    def is_quarantined(self, artifact_id: str) -> bool:
        row = self.session.get(QuarantineRow, artifact_id)
        return row is not None and row.active

    def list_quarantine(self) -> list[dict]:
        rows = self.session.execute(
            select(QuarantineRow).where(QuarantineRow.active.is_(True)).order_by(QuarantineRow.created_at.asc())
        ).scalars()
        return [
            {
                "artifact_id": r.artifact_id,
                "revocation_event_id": r.revocation_event_id,
                "reason": r.reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


class _ServiceWaivers:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, waiver: DomainWaiver) -> None:
        row = WaiverRow(
            waiver_id=waiver.waiver_id,
            artifact_id=waiver.artifact_id,
            actor=waiver.actor,
            reason=waiver.reason,
            scope=waiver.scope,
            created_at=_parse_ts(waiver.created_at) or _now(),
            expires_at=_parse_ts(waiver.expires_at),
            revocation_event_id=waiver.revocation_event_id,
            revoked=waiver.revoked,
        )
        self.session.add(row)
        self.session.commit()

    def get(self, waiver_id: str) -> DomainWaiver:
        row = self.session.get(WaiverRow, waiver_id)
        if row is None:
            raise NotFoundError(f"waiver not found: {waiver_id}")
        return self._to_domain(row)

    def _to_domain(self, row: WaiverRow) -> DomainWaiver:
        return DomainWaiver(
            waiver_id=row.waiver_id,
            artifact_id=row.artifact_id,
            actor=row.actor,
            reason=row.reason,
            scope=row.scope,
            created_at=row.created_at.isoformat() if row.created_at else "",
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
            revocation_event_id=row.revocation_event_id,
            revoked=row.revoked,
        )

    def revoke(self, waiver_id: str) -> None:
        row = self.session.get(WaiverRow, waiver_id)
        if row is not None:
            row.revoked = True
            self.session.commit()

    def active_for_artifact(self, artifact_id: str, *, now: str) -> list[DomainWaiver]:
        now_dt = _parse_ts(now) or _now()
        stmt = select(WaiverRow).where(WaiverRow.artifact_id == artifact_id, WaiverRow.revoked.is_(False))
        result = []
        for row in self.session.execute(stmt).scalars():
            expires = row.expires_at
            if expires is not None and expires.tzinfo is None:
                # SQLite drops tzinfo on round-trip even though the column is
                # declared timezone-aware; normalize to UTC before comparing
                # rather than raising on a naive/aware mismatch.
                expires = expires.replace(tzinfo=timezone.utc)
            if expires is not None and expires < now_dt:
                continue
            result.append(self._to_domain(row))
        return result

    def list_for_artifact(self, artifact_id: str) -> list[DomainWaiver]:
        stmt = select(WaiverRow).where(WaiverRow.artifact_id == artifact_id).order_by(WaiverRow.created_at.desc())
        return [self._to_domain(r) for r in self.session.execute(stmt).scalars()]


class _ServiceAudit:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, event_type: str, actor: str, payload: dict[str, Any]) -> str:
        return record_audit_event(self.session, event_type=event_type, actor=actor, payload=payload)

    def head_hash(self) -> str:
        from .models import AuditEvent

        row = self.session.execute(select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)).scalar_one_or_none()
        return row.event_hash if row is not None else "0" * 64


class ServiceWorkspace:
    """Service-mode stand-in for `skillrewind.workspace.Workspace`. See
    module docstring for the reuse rationale."""

    def __init__(self, session: Session, cas: ContentAddressedStore, config: SkillRewindConfig) -> None:
        self.session = session
        self.cas = cas
        self.config = config
        self.artifacts = _ServiceArtifacts(session)
        self.derivations = _ServiceDerivations(session)
        self.edges = LineageRepository(session)
        self.replays = _ServiceReplays(session)
        self.revocations = _ServiceRevocations(session)
        self.waivers = _ServiceWaivers(session)
        self.audit = _ServiceAudit(session)

    def ingest_artifact(
        self,
        content: bytes,
        *,
        kind: ArtifactKind,
        logical_name: str,
        mime_type: str = "application/octet-stream",
        creator: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        alias: Optional[str] = None,
    ) -> DomainArtifact:
        obj = self.cas.put_bytes(content)
        kind_value = kind.value if isinstance(kind, ArtifactKind) else kind
        scheme = _SCHEME_BY_KIND.get(kind_value, "artifact")
        artifact_id = build_artifact_id(scheme, logical_name, obj.digest_hex)

        existing = self.session.get(ArtifactRow, artifact_id)
        if existing is not None:
            return _to_domain_artifact(existing)

        row = ArtifactRow(
            artifact_id=artifact_id,
            digest_hex=obj.digest_hex,
            kind=kind_value,
            logical_name=logical_name,
            mime_type=mime_type,
            byte_size=obj.size_bytes,
            created_at=datetime.now(timezone.utc),
            storage_ref=f"cas://{obj.digest_hex}",
            status="active",
            creator=creator,
            metadata_json=metadata or {},
            alias=alias,
        )
        self.session.add(row)
        self.session.commit()
        record_audit_event(
            self.session,
            event_type="artifact.ingested",
            actor=creator or self.config.actor,
            payload={"artifact_id": artifact_id, "kind": kind_value, "byte_size": obj.size_bytes},
            entity_id=artifact_id,
        )
        return _to_domain_artifact(row)

    def resolve_alias(self, alias: str) -> Optional[DomainArtifact]:
        """Side-effect-free serving-resolution lookup: never returns a
        revoked artifact; returns a quarantined artifact only if it
        currently has an active, unexpired, unrevoked "serving"-scoped
        waiver (evaluated dynamically on every call)."""

        artifact = self.artifacts.find_by_alias(alias)
        if artifact is None:
            return None
        if artifact.status == LifecycleStatus.REVOKED:
            return None
        if artifact.status not in (LifecycleStatus.ACTIVE, LifecycleStatus.QUARANTINED):
            return None
        if self.revocations.is_quarantined(artifact.artifact_id):
            from ...quarantine.waivers import SERVING_SCOPES, has_active_waiver

            if not has_active_waiver(self, artifact.artifact_id, scopes=SERVING_SCOPES):
                return None
        return artifact
