"""SQLAlchemy 2.0 declarative models for SkillRewind Service mode.

Service mode (PostgreSQL, or SQLite for local dev/testing of this layer) is
additive to and independent of Lite mode's raw-``sqlite3`` schema in
``skillrewind.persistence.database``. See ``docs/adr/0009-service-mode-persistence.md``.

Column choices favor portability between SQLite (used in this repository's own
migration tests, since no live PostgreSQL server is available in this
environment — see ``docs/completion-matrix-v0.3.md``) and PostgreSQL (the real
Service-mode target): ``JSON`` rather than ``JSONB``, explicit ``String``
lengths avoided in favor of ``Text`` where content is unbounded, and no
dialect-specific constructs (no ``ARRAY``, no ``ENUM`` type — enums are
plain-string columns validated at the application boundary, mirroring how
Lite mode already treats its ``TEXT`` enum columns).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Installation(Base):
    """Single-row-per-deployment metadata for a Service-mode installation."""

    __tablename__ = "installations"

    installation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="skillrewind")
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Artifact(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    digest_hex: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    creator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="0.2")
    supersedes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(512), nullable=True)
    alias: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_artifacts_digest", "digest_hex"),
        Index("ix_artifacts_kind", "kind"),
        Index("ix_artifacts_status", "status"),
        Index("ix_artifacts_logical_name", "logical_name"),
        Index("ix_artifacts_alias", "alias"),
        Index("ix_artifacts_created_at", "created_at"),
    )


class AliasHistory(Base):
    """Append-only record of alias -> artifact resolution changes (never mutated)."""

    __tablename__ = "alias_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    previous_artifact_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_alias_history_alias", "alias", "changed_at"),)


class Derivation(Base):
    __tablename__ = "derivations"

    derivation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    recipe: Mapped[str] = mapped_column(String(255), nullable=False)
    recipe_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_artifact_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_derivations_target", "target_artifact_id"),
        Index("ix_derivations_started", "started_at"),
    )


class InfluenceEdge(Base):
    __tablename__ = "edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_class: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scorer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "target", "relation", name="uq_edge_triple"),
        Index("ix_edges_source", "source"),
        Index("ix_edges_target", "target"),
        Index("ix_edges_evidence", "evidence_class"),
        Index("ix_edges_status", "status"),
    )


class CandidateScore(Base):
    """Static-evidence candidate scores. Never a substitute for replay-confirmed evidence."""

    __tablename__ = "candidate_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    candidate_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False)
    calibrated_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_artifact_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    strict_negative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    feature_breakdown_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scorer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_class: Mapped[str] = mapped_column(String(32), nullable=False, default="inferred")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    explanation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    inclusion_reasons_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("run_id", "candidate_artifact_id", name="uq_candidate_score_run_candidate"),
        Index("ix_candidate_scores_target", "target_artifact_id"),
        Index("ix_candidate_scores_candidate", "candidate_artifact_id"),
        Index("ix_candidate_scores_run", "run_id"),
    )


class CandidateScoringRun(Base):
    """A single invocation of candidate recovery against a root artifact.

    Keyed for idempotency by ``request_key`` (derived from root + artifact
    snapshot digest + scorer version + config digest, or an explicit
    Idempotency-Key) so re-submitting the same recovery request never creates
    a second run or duplicate candidates.
    """

    __tablename__ = "candidate_scoring_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    root_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    candidate_scope_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    feature_config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    threshold_config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scorer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidates_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_candidate_runs_root", "root_artifact_id"),
        Index("ix_candidate_runs_status", "status"),
    )


class DerivationInput(Base):
    """A recorded parent artifact of a derivation, with the exact relation type.

    Materialized into a corresponding ``recorded`` row in ``edges`` once the
    derivation's output artifact is known (``ArtifactRepository``/
    ``DerivationRepository.set_output``) -- this table is the derivation-scoped
    view, ``edges`` is the graph-traversal view; they are kept in lockstep by
    the repository, never written independently.
    """

    __tablename__ = "derivation_inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    derivation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("derivation_id", "parent_artifact_id", "relation", name="uq_derivation_input"),
        Index("ix_derivation_inputs_derivation", "derivation_id"),
        Index("ix_derivation_inputs_parent", "parent_artifact_id"),
    )


class FeatureObservation(Base):
    """A single feature-family similarity observation between two artifacts,
    persisted so extractor version / config digest are auditable and the
    computation need not be repeated for the same (pair, extractor, config)."""

    __tablename__ = "feature_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    target_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    feature_family: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint(
            "source_artifact_id",
            "target_artifact_id",
            "feature_family",
            "extractor_version",
            "config_digest",
            name="uq_feature_observation",
        ),
        Index("ix_feature_observations_pair", "source_artifact_id", "target_artifact_id"),
    )


class ReplayRecord(Base):
    __tablename__ = "replay_records"

    replay_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    target_derivation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_ancestor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    intervention_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_replay_target", "target_derivation_id"),
        Index("ix_replay_candidate", "candidate_ancestor_id"),
        Index("ix_replay_status", "verdict"),
    )


class RevocationEvent(Base):
    __tablename__ = "revocation_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_revocation_state", "state"),
        Index("ix_revocation_created", "created_at"),
    )


class RevocationTransition(Base):
    __tablename__ = "revocation_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_transitions_event", "event_id"),)


class QuarantineEntry(Base):
    __tablename__ = "quarantine"

    artifact_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    revocation_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_quarantine_active", "active"),)


class Waiver(Base):
    __tablename__ = "waivers"

    waiver_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_waivers_artifact", "artifact_id"),)


class RebuildPlan(Base):
    __tablename__ = "rebuild_plans"

    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revocation_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RebuildAttempt(Base):
    __tablename__ = "rebuild_attempts"

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    successor_artifact_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    verification_report_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_rebuild_attempts_plan", "plan_id"),)


class VerificationReport(Base):
    __tablename__ = "verification_reports"

    report_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Attestation(Base):
    __tablename__ = "attestations"

    attestation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEvent(Base):
    """Mirrors the hash-chained audit log for Service-mode queryability.

    The chain-of-record remains the append-only log written by
    ``skillrewind.audit.log`` (or its Service-mode equivalent); this table is
    an indexed, queryable projection, not a second source of truth.
    """

    __tablename__ = "audit_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_audit_events_type", "event_type"),)


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_api_keys_status", "status"),
        Index("ix_api_keys_prefix", "prefix"),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_idempotency_scope", "operation_scope"),)


class Job(Base):
    """Durable job-queue row. See ``docs/adr/0004-durable-jobs-deferred.md`` — this
    table implements the schema that ADR anticipated; the worker/claim logic that
    consumes it is Phase B, not yet built as of this commit."""

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sanitized_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_jobs_status_priority", "status", "priority"),
        Index("ix_jobs_lease_expires", "lease_expires_at"),
        Index("ix_jobs_scheduled", "scheduled_at"),
    )


class JobEvent(Base):
    """Persisted progress/event stream for a job, consumed by SSE (Phase C)."""

    __tablename__ = "job_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_job_events_job", "job_id", "event_id"),)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    preset: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_benchmark_runs_status", "status"),
        Index("ix_benchmark_runs_created", "created_at"),
    )
