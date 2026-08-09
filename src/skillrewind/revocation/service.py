"""Revocation orchestration: request -> barrier -> recover -> replay -> quarantine
-> rebuild -> verify -> finalize.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from ..canonical.json import sha256_hex
from ..config import SkillRewindConfig
from ..domain.enums import EvidenceClass, LifecycleStatus, RelationType, RevocationPolicy, RevocationState, Severity
from ..domain.errors import NotFoundError
from ..domain.models import RevocationEvent
from ..lineage.candidates import recover_candidates
from ..lineage.closure import recorded_descendants
from ..quarantine.service import quarantine_artifact
from ..rebuild.planner import plan_rebuild
from ..rebuild.service import publish_successor, rebuild_artifact
from ..replay.selector import Budget, ReplayCandidate, select_active, select_exhaustive
from ..replay.service import run_paired_replay
from ..verification.suites import VerificationSuite, run_suite
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike
from .barrier import apply_barrier
from .decisions import should_quarantine_unresolved
from .state_machine import transition

PREVIEW_POLICY_VERSION = "0.1"


def request_revocation(
    workspace: WorkspaceLike,
    *,
    roots: list[str],
    reason: str,
    severity: Severity,
    policy: RevocationPolicy,
    actor: str,
    idempotency_key: str,
    budget: Optional[dict] = None,
) -> RevocationEvent:
    """Create (or, if the idempotency key already exists, return) a revocation
    event and apply the barrier for balanced/strict policies. Rerunning the
    same request with the same idempotency key never duplicates state."""

    existing = workspace.revocations.get_by_idempotency_key(idempotency_key)
    if existing is not None:
        return existing

    event = RevocationEvent(
        event_id=f"revoke-{uuid.uuid4()}",
        roots=sorted(set(roots)),
        reason=reason,
        severity=severity,
        policy=policy,
        actor=actor,
        idempotency_key=idempotency_key,
        budget=budget or {},
        created_at=timestamp(),
    )
    workspace.revocations.insert(event)
    workspace.audit.append(
        "revocation.requested",
        actor,
        {"event_id": event.event_id, "roots": event.roots, "policy": policy.value, "severity": severity.value},
    )

    if policy == RevocationPolicy.FORENSIC:
        event = transition(workspace.revocations, event, RevocationState.CANDIDATE_RECOVERY)
    else:
        apply_barrier(workspace, event)
        event = transition(workspace.revocations, event, RevocationState.BARRIER_APPLIED)
        event = transition(workspace.revocations, event, RevocationState.CANDIDATE_RECOVERY)
    return event


@dataclass(frozen=True, slots=True)
class RevocationPreview:
    """Side-effect-free preview of what a revocation *would* do (master spec
    Phase C2.3 section 5). Computing this never writes artifact status,
    quarantine, evidence, or successor state -- it only reads the already-
    recorded closure and already-existing evidence edges."""

    roots: list[str]
    policy: RevocationPolicy
    policy_version: str
    recorded_descendants: list[str]
    replay_confirmed: list[dict]
    replay_rejected: list[dict]
    unresolved: list[dict]
    inferred_excluded: list[dict]
    already_revoked_or_quarantined: list[str]
    proposed_targets: list[dict]
    rebuild_available: dict[str, bool]
    unresolved_uncertainty: list[str]
    estimated_operation_counts: dict[str, int]
    preview_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": self.roots,
            "policy": self.policy.value,
            "policy_version": self.policy_version,
            "recorded_descendants": self.recorded_descendants,
            "replay_confirmed": self.replay_confirmed,
            "replay_rejected": self.replay_rejected,
            "unresolved": self.unresolved,
            "inferred_excluded": self.inferred_excluded,
            "already_revoked_or_quarantined": self.already_revoked_or_quarantined,
            "proposed_targets": self.proposed_targets,
            "rebuild_available": self.rebuild_available,
            "unresolved_uncertainty": self.unresolved_uncertainty,
            "estimated_operation_counts": self.estimated_operation_counts,
            "preview_digest": self.preview_digest,
        }


def build_revocation_preview(
    workspace: WorkspaceLike,
    *,
    roots: list[str],
    policy: RevocationPolicy,
    manual_targets: Optional[list[dict]] = None,
) -> RevocationPreview:
    """Computes (never persists) the preview of a revocation over `roots`
    under `policy`.

    Reads only already-recorded closure (`recorded_descendants`) and
    already-existing `hidden-influence` evidence edges -- it never calls
    `recover_candidates(..., persist=True)`, never runs replay, never writes
    artifact status/quarantine/successor state. Two calls against the same
    evidence snapshot with the same roots/policy/manual targets always
    produce the same `preview_digest` (deterministic policy digest,
    section 4).
    """

    roots = sorted(set(roots))
    manual_targets = manual_targets or []
    recorded = list(recorded_descendants(workspace, roots, include_roots=True))

    replay_confirmed: list[dict] = []
    replay_rejected: list[dict] = []
    unresolved: list[dict] = []
    inferred_excluded: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    for member in recorded:
        # Hidden-influence edges are recorded ancestor(root) -> descendant, so
        # a closure member's *outgoing* hidden-influence edges are what
        # points at replay-confirmed/inferred/rejected/unresolved candidates
        # -- never `incoming`, which would look for edges pointing *at* the
        # root itself.
        for edge in workspace.edges.outgoing(member):
            if edge.relation != RelationType.HIDDEN_INFLUENCE:
                continue
            pair = (edge.source, edge.target)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            entry = {
                "source": edge.source,
                "target": edge.target,
                "evidence_class": edge.evidence_class.value,
                "confidence": edge.confidence,
            }
            if edge.evidence_class == EvidenceClass.REPLAY_CONFIRMED:
                replay_confirmed.append(entry)
            elif edge.evidence_class == EvidenceClass.REJECTED:
                replay_rejected.append(entry)
            elif edge.evidence_class == EvidenceClass.UNRESOLVED:
                unresolved.append(entry)
            elif edge.evidence_class == EvidenceClass.INFERRED:
                inferred_excluded.append(entry)

    already_revoked_or_quarantined = []
    for artifact_id in recorded:
        try:
            artifact = workspace.artifacts.get(artifact_id)
        except NotFoundError:
            continue
        if artifact.status in (LifecycleStatus.REVOKED, LifecycleStatus.QUARANTINED) or workspace.revocations.is_quarantined(
            artifact_id
        ):
            already_revoked_or_quarantined.append(artifact_id)

    proposed: list[dict] = []
    proposed_ids: set[str] = set()
    for root in roots:
        proposed.append({"artifact_id": root, "action": "revoke", "evidence_class": "recorded", "reason": "explicitly revoked root"})
        proposed_ids.add(root)
    for member in recorded:
        if member in proposed_ids:
            continue
        proposed.append(
            {"artifact_id": member, "action": "quarantine", "evidence_class": "recorded", "reason": "recorded descendant of revoked root"}
        )
        proposed_ids.add(member)

    if policy != RevocationPolicy.FORENSIC:
        for entry in replay_confirmed:
            target = entry["target"]
            if target in proposed_ids:
                continue
            proposed.append(
                {
                    "artifact_id": target,
                    "action": "quarantine",
                    "evidence_class": "replay-confirmed",
                    "reason": f"replay-confirmed hidden influence from {entry['source']}",
                }
            )
            proposed_ids.add(target)

    if policy == RevocationPolicy.STRICT:
        for entry in unresolved:
            target = entry["target"]
            if target in proposed_ids:
                continue
            proposed.append(
                {
                    "artifact_id": target,
                    "action": "manual-review",
                    "evidence_class": "unresolved",
                    "reason": "strict policy: unresolved candidate flagged for manual review, not auto-quarantined",
                }
            )
            proposed_ids.add(target)

    for manual in manual_targets:
        target = manual.get("artifact_id")
        if not target or target in proposed_ids:
            continue
        proposed.append(
            {
                "artifact_id": target,
                "action": manual.get("action", "manual-review"),
                "evidence_class": "manual",
                "reason": manual.get("reason", "explicit manual target override"),
            }
        )
        proposed_ids.add(target)

    rebuild_available: dict[str, bool] = {}
    for entry in proposed:
        target = entry["artifact_id"]
        if entry["action"] not in ("quarantine", "rebuild-candidate"):
            continue
        try:
            plan_rebuild(workspace, target)
            rebuild_available[target] = True
        except (NotFoundError, ValueError):
            rebuild_available[target] = False
            entry["proposed_action_note"] = "no clean-room rebuild recipe available"

    unresolved_uncertainty = [
        f"{u['source']} -> {u['target']}: replay outcome unresolved, not treated as confirmation" for u in unresolved
    ]
    if inferred_excluded and policy != RevocationPolicy.STRICT:
        unresolved_uncertainty.append(
            f"{len(inferred_excluded)} inferred (non-replay-confirmed) candidate(s) exist for this closure and "
            "were NOT included in the proposed target set under this policy."
        )

    estimated_operation_counts = {
        "recorded_descendants": len(recorded),
        "proposed_targets": len(proposed),
        "quarantine_operations": sum(1 for p in proposed if p["action"] == "quarantine"),
        "revoke_operations": sum(1 for p in proposed if p["action"] == "revoke"),
        "rebuild_candidates": sum(1 for v in rebuild_available.values() if v),
        "manual_review_items": sum(1 for p in proposed if p["action"] == "manual-review"),
    }

    digest_input = {
        "roots": roots,
        "policy": policy.value,
        "policy_version": PREVIEW_POLICY_VERSION,
        "recorded_descendants": recorded,
        "replay_confirmed": sorted(replay_confirmed, key=lambda e: (e["source"], e["target"])),
        "proposed_targets": sorted(({k: v for k, v in p.items() if k != "proposed_action_note"} for p in proposed), key=lambda e: str(e["artifact_id"])),
    }
    preview_digest = sha256_hex(digest_input)

    return RevocationPreview(
        roots=roots,
        policy=policy,
        policy_version=PREVIEW_POLICY_VERSION,
        recorded_descendants=recorded,
        replay_confirmed=replay_confirmed,
        replay_rejected=replay_rejected,
        unresolved=unresolved,
        inferred_excluded=inferred_excluded,
        already_revoked_or_quarantined=already_revoked_or_quarantined,
        proposed_targets=proposed,
        rebuild_available=rebuild_available,
        unresolved_uncertainty=unresolved_uncertainty,
        estimated_operation_counts=estimated_operation_counts,
        preview_digest=preview_digest,
    )


@dataclass
class RevocationRunResult:
    event: RevocationEvent


#: Linear ordering of the non-terminal stages `run_revocation` walks through.
#: Used only to decide, on a resumed/retried call, whether a stage has
#: already been passed -- never to bypass the state machine's own validity
#: checks in `transition()`.
_STAGE_ORDER = [
    RevocationState.REQUESTED,
    RevocationState.BARRIER_APPLIED,
    RevocationState.CANDIDATE_RECOVERY,
    RevocationState.REPLAY_SELECTION,
    RevocationState.REPLAYING,
    RevocationState.QUARANTINE_APPLIED,
    RevocationState.REBUILD_PLANNING,
    RevocationState.REBUILDING,
    RevocationState.VERIFYING,
]

_TERMINAL_STATES = (
    RevocationState.COMPLETED,
    RevocationState.COMPLETED_WITH_UNRESOLVED,
    RevocationState.FAILED,
    RevocationState.CANCELLED_BEFORE_BARRIER,
)


def _advance(workspace: WorkspaceLike, event: RevocationEvent, to_state: RevocationState) -> RevocationEvent:
    """Transition to `to_state` unless the event has already reached or passed
    it (a resumed call re-walking the same code path from the top)."""

    if event.state in _TERMINAL_STATES:
        return event
    current_index = _STAGE_ORDER.index(event.state) if event.state in _STAGE_ORDER else -1
    target_index = _STAGE_ORDER.index(to_state) if to_state in _STAGE_ORDER else len(_STAGE_ORDER)
    if current_index >= target_index and event.state != to_state:
        return event
    return transition(workspace.revocations, event, to_state)


def run_revocation(
    workspace: WorkspaceLike,
    event: RevocationEvent,
    *,
    config: Optional[SkillRewindConfig] = None,
    replay_selection: str = "active",
    max_replay_calls: Optional[int] = None,
    verification_suite: Optional[VerificationSuite] = None,
    attempt_rebuild: bool = True,
) -> RevocationEvent:
    """Runs (or resumes) a revocation event through its full pipeline.

    Resumable by construction: a crash or worker restart mid-run and a
    subsequent call with the same, freshly-refetched `event` re-derives the
    deterministic parts (recorded closure, candidate recovery, replay
    selection) idempotently, and skips any per-target quarantine/rebuild work
    already recorded on the persisted event rather than repeating it -- so no
    duplicate quarantine entries, rebuild attempts, or audit events are
    produced. A call on an already-terminal event is a complete no-op.
    """

    config = config or workspace.config

    if event.state in _TERMINAL_STATES:
        return event

    event.recorded_closure = list(recorded_descendants(workspace, event.roots, include_roots=True))

    # --- hidden-lineage candidate recovery: for every artifact NOT already in
    # the recorded closure, check whether a root/closure member is a
    # plausible hidden ancestor.
    hidden_candidates: list[dict] = []
    for artifact in workspace.artifacts.list(limit=10_000):
        if artifact.artifact_id in event.recorded_closure:
            continue
        results = recover_candidates(workspace, artifact.artifact_id, config=config)
        for result in results:
            if result.candidate_id in event.recorded_closure and result.result.is_candidate:
                hidden_candidates.append(
                    {
                        "root_or_closure_member": result.candidate_id,
                        "target": artifact.artifact_id,
                        "score": result.result.score,
                        "is_high_confidence": result.result.is_high_confidence,
                        "strict_negative_signal": result.result.strict_negative_signal,
                    }
                )
    event.candidates = hidden_candidates
    workspace.revocations.update(event)
    event = _advance(workspace, event, RevocationState.REPLAY_SELECTION)

    # --- budget-aware replay selection
    budget = Budget(max_replay_calls=max_replay_calls or event.budget.get("replay_calls") or config.default_replay_budget_calls)
    replay_pool = [
        ReplayCandidate(candidate_id=f"{c['root_or_closure_member']}=>{c['target']}", inferred_score=c["score"])
        for c in hidden_candidates
    ]
    selector = select_exhaustive if replay_selection == "exhaustive" else select_active
    selection = selector(replay_pool, budget=budget)

    # Replay decisions are only ever *computed* while still in REPLAYING (or
    # arriving at it for the first time); once the event has moved on to
    # QUARANTINE_APPLIED or beyond, `event.replay_decisions` as persisted is
    # authoritative and must not be recomputed -- re-invoking `run_paired_replay`
    # on a resumed call would create duplicate replay records for candidates
    # already decided.
    replaying_not_yet_done = event.state not in _STAGE_ORDER or _STAGE_ORDER.index(event.state) <= _STAGE_ORDER.index(
        RevocationState.REPLAYING
    )
    event = _advance(workspace, event, RevocationState.REPLAYING)

    if replaying_not_yet_done:
        replay_decisions = list(event.replay_decisions)
        already_decided_pairs = {f"{d['root_or_closure_member']}=>{d['target']}" for d in replay_decisions}
        selected_pairs = {s for s in selection.selected}
        for candidate in hidden_candidates:
            pair_key = f"{candidate['root_or_closure_member']}=>{candidate['target']}"
            if pair_key in already_decided_pairs:
                continue  # already decided in a prior (crashed/resumed) attempt
            if pair_key not in selected_pairs:
                replay_decisions.append({**candidate, "replay": "skipped", "reason": "not-selected-within-budget"})
            else:
                derivation = workspace.derivations.find_by_target(candidate["target"])
                if derivation is None:
                    replay_decisions.append({**candidate, "replay": "skipped", "reason": "no-derivation-recorded"})
                else:
                    outcome = run_paired_replay(
                        workspace, candidate["root_or_closure_member"], derivation.derivation_id, config=config
                    )
                    replay_decisions.append(
                        {**candidate, "replay": "executed", "replay_id": outcome.replay_id, "verdict": outcome.verdict.value}
                    )
            # persist after every single decision so a crash mid-loop never
            # re-executes a replay that already ran.
            event.replay_decisions = replay_decisions
            workspace.revocations.update(event)
        event.replay_decisions = replay_decisions
        workspace.revocations.update(event)

    confirmed_targets = {d["target"] for d in event.replay_decisions if d.get("verdict") == "confirmed"}
    unresolved_targets = [
        d for d in event.replay_decisions if d.get("replay") == "executed" and d.get("verdict") not in ("confirmed", "rejected")
    ]

    # --- quarantine. Forensic mode makes no serving-state changes: confirmed
    # findings are reported but never actually quarantined.
    event = _advance(workspace, event, RevocationState.QUARANTINE_APPLIED)
    is_forensic = event.policy == RevocationPolicy.FORENSIC
    already_unresolved_targets = {u.get("target") for u in event.unresolved}
    for target in sorted(confirmed_targets):
        if target in event.quarantined:
            continue  # already quarantined in a prior attempt
        if is_forensic:
            # Forensic mode makes no serving-state change at all: `quarantine_artifact`
            # is never called, and `event.quarantined` -- which is meant to reflect
            # artifacts actually placed into quarantine -- must not silently claim
            # otherwise. The confirmed finding is still visible via `replay_decisions`.
            continue
        quarantine_artifact(workspace, target, revocation_event_id=event.event_id, reason="replay-confirmed hidden descendant")
        event.quarantined.append(target)
        workspace.revocations.update(event)
    for candidate in unresolved_targets:
        target = candidate["target"]
        if target in event.quarantined or target in already_unresolved_targets:
            continue  # already resolved (either way) in a prior attempt
        if not is_forensic and should_quarantine_unresolved(policy=event.policy, severity=event.severity, config=config):
            quarantine_artifact(
                workspace, target, revocation_event_id=event.event_id,
                reason=f"policy-quarantined unresolved high-severity candidate ({candidate['verdict']})",
            )
            event.quarantined.append(target)
        else:
            event.unresolved.append(candidate)
            already_unresolved_targets.add(target)
        workspace.revocations.update(event)

    # --- rebuild + verify quarantined artifacts (best-effort; artifacts
    # without a registered clean-room recipe remain quarantined+unresolved).
    # Never attempted in forensic mode, which makes no serving-state changes.
    if attempt_rebuild and not is_forensic and event.quarantined:
        suite = verification_suite or VerificationSuite(suite_id="default", version="0.1.0")
        event = _advance(workspace, event, RevocationState.REBUILD_PLANNING)
        event = _advance(workspace, event, RevocationState.REBUILDING)
        already_rebuilt = {r["original"] for r in event.rebuilt}
        already_unresolved_targets = {u.get("target") for u in event.unresolved}
        reached_verifying = event.state not in (RevocationState.REBUILD_PLANNING, RevocationState.REBUILDING)
        for artifact_id in list(event.quarantined):
            if artifact_id in already_rebuilt or artifact_id in already_unresolved_targets:
                continue  # already rebuilt-and-verified (or given up on) in a prior attempt
            try:
                rebuild_result = rebuild_artifact(workspace, artifact_id, revocation_event_id=event.event_id)
            except (NotFoundError, KeyError) as exc:
                event.unresolved.append({"target": artifact_id, "reason": f"rebuild-unavailable: {exc}"})
                already_unresolved_targets.add(artifact_id)
                workspace.revocations.update(event)
                continue

            if not reached_verifying:
                event = _advance(workspace, event, RevocationState.VERIFYING)
                reached_verifying = True
            report = run_suite(
                workspace, rebuild_result.new_artifact.artifact_id, suite, fixture_output=rebuild_result.fixture_output
            )
            if report.status == "pass":
                publish_successor(workspace, artifact_id, rebuild_result.new_artifact.artifact_id)
                event.rebuilt.append(
                    {"original": artifact_id, "successor": rebuild_result.new_artifact.artifact_id, "verification": report.to_dict()}
                )
                already_rebuilt.add(artifact_id)
            else:
                event.unresolved.append(
                    {"target": artifact_id, "reason": "verification-failed", "verification": report.to_dict()}
                )
                already_unresolved_targets.add(artifact_id)
            workspace.revocations.update(event)
        if event.state == RevocationState.REBUILDING:
            # every rebuild attempt failed before producing a candidate to verify
            event = _advance(workspace, event, RevocationState.VERIFYING)
        workspace.revocations.update(event)

    unresolved_high_severity = [
        u for u in event.unresolved if event.severity in (Severity.HIGH, Severity.CRITICAL)
    ]
    final_state = (
        RevocationState.COMPLETED_WITH_UNRESOLVED if unresolved_high_severity or event.unresolved else RevocationState.COMPLETED
    )
    if event.state not in (RevocationState.COMPLETED, RevocationState.COMPLETED_WITH_UNRESOLVED):
        event = transition(workspace.revocations, event, final_state)
    workspace.revocations.update(event)

    workspace.audit.append(
        "revocation.finalized",
        config.actor,
        {"event_id": event.event_id, "state": event.state.value, "quarantined": event.quarantined, "unresolved_count": len(event.unresolved)},
    )
    return event
