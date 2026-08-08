"""Typed domain models.

These are plain dataclasses rather than a heavier ORM/validation framework.
This is a deliberate scope reduction for the local/CLI vertical slice
implemented in this session (see ``docs/adr/0003-persistence.md``): no
service-mode Postgres/SQLAlchemy layer is implemented, so the extra
indirection of a full ORM model layer was not justified. Values are still
validated at the boundaries that construct them (CAS, ID parsing, CLI/SDK
input parsing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .enums import (
    ArtifactKind,
    EvidenceClass,
    LifecycleStatus,
    RelationType,
    ReplayVerdict,
    RevocationPolicy,
    RevocationState,
    Severity,
)


@dataclass(slots=True)
class Artifact:
    artifact_id: str
    digest_hex: str
    kind: ArtifactKind
    logical_name: str
    mime_type: str
    byte_size: int
    created_at: str
    storage_ref: str
    status: LifecycleStatus = LifecycleStatus.ACTIVE
    creator: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "0.2"
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    alias: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "digest_hex": self.digest_hex,
            "kind": self.kind.value,
            "logical_name": self.logical_name,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "created_at": self.created_at,
            "storage_ref": self.storage_ref,
            "status": self.status.value,
            "creator": self.creator,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "alias": self.alias,
        }


@dataclass(slots=True)
class Derivation:
    derivation_id: str
    recipe: str
    recipe_version: str
    target_artifact_id: Optional[str]
    recorded_inputs: list[str] = field(default_factory=list)
    context_exposures: list[str] = field(default_factory=list)
    candidate_context_pool: list[str] = field(default_factory=list)
    model_provider: Optional[str] = None
    model_id: Optional[str] = None
    decoding_config_digest: Optional[str] = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    task_snapshot: dict[str, Any] = field(default_factory=dict)
    workspace_snapshot: dict[str, Any] = field(default_factory=dict)
    environment_digest: Optional[str] = None
    seed: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    validation_result: Optional[dict[str, Any]] = None
    replayable: bool = True
    replay_limitations: list[str] = field(default_factory=list)
    status: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "derivation_id": self.derivation_id,
            "recipe": self.recipe,
            "recipe_version": self.recipe_version,
            "target_artifact_id": self.target_artifact_id,
            "recorded_inputs": self.recorded_inputs,
            "context_exposures": self.context_exposures,
            "candidate_context_pool": self.candidate_context_pool,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "decoding_config_digest": self.decoding_config_digest,
            "tool_calls": self.tool_calls,
            "task_snapshot": self.task_snapshot,
            "workspace_snapshot": self.workspace_snapshot,
            "environment_digest": self.environment_digest,
            "seed": self.seed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "validation_result": self.validation_result,
            "replayable": self.replayable,
            "replay_limitations": self.replay_limitations,
            "status": self.status,
        }


@dataclass(slots=True)
class InfluenceEdge:
    source: str
    target: str
    relation: RelationType
    evidence_class: EvidenceClass
    status: str = "active"
    confidence: Optional[float] = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    intervention: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    scorer_version: Optional[str] = None
    invalidation_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation.value,
            "evidence_class": self.evidence_class.value,
            "status": self.status,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "intervention": self.intervention,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scorer_version": self.scorer_version,
            "invalidation_reason": self.invalidation_reason,
        }


@dataclass(slots=True)
class ReplayRecord:
    replay_id: str
    target_derivation_id: str
    candidate_ancestor_id: str
    intervention_kind: str
    model_identity: Optional[str]
    environment_identity: Optional[str]
    seed: Optional[int]
    repetitions: int
    normalized_outputs: dict[str, Any]
    target_behavior_vector: dict[str, Any]
    utility_metrics: dict[str, Any]
    effect_estimate: Optional[float]
    fidelity: dict[str, float]
    verdict: Optional[ReplayVerdict]
    cost: dict[str, Any] = field(default_factory=dict)
    logs_digest: Optional[str] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id,
            "target_derivation_id": self.target_derivation_id,
            "candidate_ancestor_id": self.candidate_ancestor_id,
            "intervention_kind": self.intervention_kind,
            "model_identity": self.model_identity,
            "environment_identity": self.environment_identity,
            "seed": self.seed,
            "repetitions": self.repetitions,
            "normalized_outputs": self.normalized_outputs,
            "target_behavior_vector": self.target_behavior_vector,
            "utility_metrics": self.utility_metrics,
            "effect_estimate": self.effect_estimate,
            "fidelity": self.fidelity,
            "verdict": self.verdict.value if self.verdict else None,
            "cost": self.cost,
            "logs_digest": self.logs_digest,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class RevocationEvent:
    event_id: str
    roots: list[str]
    reason: str
    severity: Severity
    policy: RevocationPolicy
    actor: str
    idempotency_key: str
    state: RevocationState = RevocationState.REQUESTED
    thresholds: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    recorded_closure: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    replay_decisions: list[dict[str, Any]] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    rebuilt: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    attestation_id: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    cost: dict[str, Any] = field(default_factory=lambda: {"replay_calls": 0, "wall_clock_seconds": 0})

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "roots": self.roots,
            "reason": self.reason,
            "severity": self.severity.value,
            "policy": self.policy.value,
            "actor": self.actor,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "thresholds": self.thresholds,
            "budget": self.budget,
            "recorded_closure": self.recorded_closure,
            "candidates": self.candidates,
            "replay_decisions": self.replay_decisions,
            "quarantined": self.quarantined,
            "rebuilt": self.rebuilt,
            "unresolved": self.unresolved,
            "attestation_id": self.attestation_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "cost": self.cost,
        }


@dataclass(slots=True)
class Waiver:
    waiver_id: str
    artifact_id: str
    actor: str
    reason: str
    scope: str
    created_at: str
    expires_at: Optional[str] = None
    revocation_event_id: Optional[str] = None
    revoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "waiver_id": self.waiver_id,
            "artifact_id": self.artifact_id,
            "actor": self.actor,
            "reason": self.reason,
            "scope": self.scope,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revocation_event_id": self.revocation_event_id,
            "revoked": self.revoked,
        }
