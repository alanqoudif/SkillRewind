"""Domain enumerations shared across SkillRewind subsystems."""

from __future__ import annotations

from enum import Enum


class ArtifactKind(str, Enum):
    AGENT_SKILL = "agent-skill"
    MEMORY = "memory"
    TRAJECTORY = "trajectory"
    PROMPT_PATCH = "prompt-patch"
    SOURCE_CODE = "source-code"
    CONFIGURATION = "configuration"
    TEMPLATE = "template"
    WORKFLOW = "workflow"
    TOOL_RESULT = "tool-result"
    TASK_SNAPSHOT = "task-snapshot"
    WORKSPACE_SNAPSHOT = "workspace-snapshot"
    ENVIRONMENT_SNAPSHOT = "environment-snapshot"
    VALIDATION_REPORT = "validation-report"
    REPLAY_RUN = "replay-run"
    ATTESTATION = "attestation"
    OTHER = "other"


class LifecycleStatus(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class RelationType(str, Enum):
    USED_AS_INPUT = "used-as-input"
    CONTEXT_EXPOSURE = "context-exposure"
    MEMORY_REFERENCE = "memory-reference"
    REBUILT_FROM = "rebuilt-from"
    REPLACES = "replaces"
    CLEAN_SUPPORT = "clean-support"
    RECORDED_INFLUENCE = "recorded-influence"
    HIDDEN_INFLUENCE = "hidden-influence"
    # Service-mode derivation-input relation types (Phase C2.1). Distinct
    # names from the pre-existing ones above for backward compatibility;
    # DIRECT_INPUT/RETRIEVED_MEMORY/etc. are what derivation-capture callers
    # use, the older names remain for Lite-mode/v0.2 callers.
    DIRECT_INPUT = "direct-input"
    RETRIEVED_MEMORY = "retrieved-memory"
    DISTILLED_FROM = "distilled-from"
    GENERATED_FROM_TRACE = "generated-from-trace"
    IMPORTED_DEPENDENCY = "imported-dependency"


#: Relation types accepted for a derivation's parent-input edges (master spec
#: Phase C2.1 section 1). ``context-exposure`` is shared with the pre-existing
#: v0.2 enum member of the same value.
DERIVATION_INPUT_RELATIONS = frozenset(
    {
        RelationType.DIRECT_INPUT.value,
        RelationType.CONTEXT_EXPOSURE.value,
        RelationType.RETRIEVED_MEMORY.value,
        RelationType.DISTILLED_FROM.value,
        RelationType.GENERATED_FROM_TRACE.value,
        RelationType.IMPORTED_DEPENDENCY.value,
    }
)


class EvidenceClass(str, Enum):
    RECORDED = "recorded"
    INFERRED = "inferred"
    REPLAY_CONFIRMED = "replay-confirmed"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class RevocationPolicy(str, Enum):
    FORENSIC = "forensic"
    BALANCED = "balanced"
    STRICT = "strict"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RevocationState(str, Enum):
    REQUESTED = "requested"
    BARRIER_APPLIED = "barrier-applied"
    CANDIDATE_RECOVERY = "candidate-recovery"
    REPLAY_SELECTION = "replay-selection"
    REPLAYING = "replaying"
    QUARANTINE_APPLIED = "quarantine-applied"
    REBUILD_PLANNING = "rebuild-planning"
    REBUILDING = "rebuilding"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    COMPLETED_WITH_UNRESOLVED = "completed-with-unresolved"
    FAILED = "failed"
    CANCELLED_BEFORE_BARRIER = "cancelled-before-barrier"


#: Transitions allowed out of each revocation state. Enforced by the state
#: machine in ``skillrewind.revocation.state_machine``.
ALLOWED_TRANSITIONS: dict[RevocationState, tuple[RevocationState, ...]] = {
    RevocationState.REQUESTED: (
        RevocationState.BARRIER_APPLIED,
        RevocationState.CANCELLED_BEFORE_BARRIER,
        RevocationState.CANDIDATE_RECOVERY,  # forensic mode skips the barrier
        RevocationState.FAILED,
    ),
    RevocationState.BARRIER_APPLIED: (
        RevocationState.CANDIDATE_RECOVERY,
        RevocationState.FAILED,
    ),
    RevocationState.CANDIDATE_RECOVERY: (
        RevocationState.REPLAY_SELECTION,
        RevocationState.QUARANTINE_APPLIED,
        RevocationState.COMPLETED,
        RevocationState.COMPLETED_WITH_UNRESOLVED,
        RevocationState.FAILED,
    ),
    RevocationState.REPLAY_SELECTION: (RevocationState.REPLAYING, RevocationState.FAILED),
    RevocationState.REPLAYING: (RevocationState.QUARANTINE_APPLIED, RevocationState.FAILED),
    RevocationState.QUARANTINE_APPLIED: (
        RevocationState.REBUILD_PLANNING,
        RevocationState.COMPLETED,
        RevocationState.COMPLETED_WITH_UNRESOLVED,
        RevocationState.FAILED,
    ),
    RevocationState.REBUILD_PLANNING: (RevocationState.REBUILDING, RevocationState.FAILED),
    RevocationState.REBUILDING: (RevocationState.VERIFYING, RevocationState.FAILED),
    RevocationState.VERIFYING: (
        RevocationState.COMPLETED,
        RevocationState.COMPLETED_WITH_UNRESOLVED,
        RevocationState.FAILED,
    ),
    RevocationState.COMPLETED: (),
    RevocationState.COMPLETED_WITH_UNRESOLVED: (),
    RevocationState.FAILED: (),
    RevocationState.CANCELLED_BEFORE_BARRIER: (),
}

#: States reached only after the barrier has made revoked roots non-servable.
POST_BARRIER_STATES = frozenset(
    {
        RevocationState.BARRIER_APPLIED,
        RevocationState.CANDIDATE_RECOVERY,
        RevocationState.REPLAY_SELECTION,
        RevocationState.REPLAYING,
        RevocationState.QUARANTINE_APPLIED,
        RevocationState.REBUILD_PLANNING,
        RevocationState.REBUILDING,
        RevocationState.VERIFYING,
        RevocationState.COMPLETED,
        RevocationState.COMPLETED_WITH_UNRESOLVED,
    }
)


class ReplayVerdict(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNRESOLVED_FIDELITY = "unresolved-fidelity"
    UNRESOLVED_REPETITIONS = "unresolved-repetitions"
    UNRESOLVED_BUDGET = "unresolved-budget"
    UNRESOLVED_RUNNER_FAILURE = "unresolved-runner-failure"
    #: Reconstruction inputs (e.g. the candidate's derivation) do not exist,
    #: so no runner could even be invoked. Added for Service-mode replay
    #: (Phase C2.2); like every other UNRESOLVED_* member, this must never be
    #: conflated with REJECTED -- missing inputs prove nothing about influence.
    UNRESOLVED_MISSING_INPUTS = "unresolved-missing-inputs"


class InterventionKind(str, Enum):
    PRESENT = "present"
    WITHHELD = "withheld"
    CLEAN_CONTROL = "clean-control"
    IRRELEVANT_CONTROL = "irrelevant-control"
