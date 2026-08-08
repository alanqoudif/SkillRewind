"""Revocation orchestration: request -> barrier -> recover -> replay -> quarantine
-> rebuild -> verify -> finalize.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from ..config import SkillRewindConfig
from ..domain.enums import EvidenceClass, LifecycleStatus, RevocationPolicy, RevocationState, Severity
from ..domain.errors import NotFoundError
from ..domain.models import RevocationEvent
from ..lineage.candidates import recover_candidates
from ..lineage.closure import recorded_descendants
from ..quarantine.service import quarantine_artifact
from ..rebuild.service import publish_successor, rebuild_artifact
from ..replay.selector import Budget, ReplayCandidate, select_active, select_exhaustive
from ..replay.service import run_paired_replay
from ..verification.suites import VerificationSuite, run_suite
from ..workspace import Workspace, timestamp
from .barrier import apply_barrier
from .decisions import should_quarantine_unresolved
from .state_machine import transition


def request_revocation(
    workspace: Workspace,
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


@dataclass
class RevocationRunResult:
    event: RevocationEvent


def run_revocation(
    workspace: Workspace,
    event: RevocationEvent,
    *,
    config: Optional[SkillRewindConfig] = None,
    replay_selection: str = "active",
    max_replay_calls: Optional[int] = None,
    verification_suite: Optional[VerificationSuite] = None,
    attempt_rebuild: bool = True,
) -> RevocationEvent:
    config = config or workspace.config

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
    event = transition(workspace.revocations, event, RevocationState.REPLAY_SELECTION)

    # --- budget-aware replay selection
    budget = Budget(max_replay_calls=max_replay_calls or event.budget.get("replay_calls") or config.default_replay_budget_calls)
    replay_pool = [
        ReplayCandidate(candidate_id=f"{c['root_or_closure_member']}=>{c['target']}", inferred_score=c["score"])
        for c in hidden_candidates
    ]
    selector = select_exhaustive if replay_selection == "exhaustive" else select_active
    selection = selector(replay_pool, budget=budget)

    event = transition(workspace.revocations, event, RevocationState.REPLAYING)
    replay_decisions: list[dict] = []
    confirmed_targets: set[str] = set()
    unresolved_targets: list[dict] = []

    selected_pairs = {s for s in selection.selected}
    for candidate in hidden_candidates:
        pair_key = f"{candidate['root_or_closure_member']}=>{candidate['target']}"
        if pair_key not in selected_pairs:
            replay_decisions.append({**candidate, "replay": "skipped", "reason": "not-selected-within-budget"})
            continue
        derivation = workspace.derivations.find_by_target(candidate["target"])
        if derivation is None:
            replay_decisions.append({**candidate, "replay": "skipped", "reason": "no-derivation-recorded"})
            continue
        outcome = run_paired_replay(
            workspace, candidate["root_or_closure_member"], derivation.derivation_id, config=config
        )
        replay_decisions.append(
            {**candidate, "replay": "executed", "replay_id": outcome.replay_id, "verdict": outcome.verdict.value}
        )
        if outcome.verdict.value == "confirmed":
            confirmed_targets.add(candidate["target"])
        elif outcome.verdict.value != "rejected":
            unresolved_targets.append({**candidate, "verdict": outcome.verdict.value})

    event.replay_decisions = replay_decisions
    workspace.revocations.update(event)

    # --- quarantine. Forensic mode makes no serving-state changes: confirmed
    # findings are reported but never actually quarantined.
    event = transition(workspace.revocations, event, RevocationState.QUARANTINE_APPLIED)
    is_forensic = event.policy == RevocationPolicy.FORENSIC
    for target in sorted(confirmed_targets):
        if not is_forensic:
            quarantine_artifact(workspace, target, revocation_event_id=event.event_id, reason="replay-confirmed hidden descendant")
        event.quarantined.append(target)
    for candidate in unresolved_targets:
        if not is_forensic and should_quarantine_unresolved(policy=event.policy, severity=event.severity, config=config):
            quarantine_artifact(
                workspace, candidate["target"], revocation_event_id=event.event_id,
                reason=f"policy-quarantined unresolved high-severity candidate ({candidate['verdict']})",
            )
            if candidate["target"] not in event.quarantined:
                event.quarantined.append(candidate["target"])
        else:
            event.unresolved.append(candidate)
    workspace.revocations.update(event)

    # --- rebuild + verify quarantined artifacts (best-effort; artifacts
    # without a registered clean-room recipe remain quarantined+unresolved).
    # Never attempted in forensic mode, which makes no serving-state changes.
    if attempt_rebuild and not is_forensic and event.quarantined:
        suite = verification_suite or VerificationSuite(suite_id="default", version="0.1.0")
        event = transition(workspace.revocations, event, RevocationState.REBUILD_PLANNING)
        event = transition(workspace.revocations, event, RevocationState.REBUILDING)
        reached_verifying = False
        for artifact_id in list(event.quarantined):
            try:
                result = rebuild_artifact(workspace, artifact_id, revocation_event_id=event.event_id)
            except (NotFoundError, KeyError) as exc:
                event.unresolved.append({"target": artifact_id, "reason": f"rebuild-unavailable: {exc}"})
                continue

            if not reached_verifying:
                event = transition(workspace.revocations, event, RevocationState.VERIFYING)
                reached_verifying = True
            report = run_suite(workspace, result.new_artifact.artifact_id, suite, fixture_output=result.fixture_output)
            if report.status == "pass":
                publish_successor(workspace, artifact_id, result.new_artifact.artifact_id)
                event.rebuilt.append(
                    {"original": artifact_id, "successor": result.new_artifact.artifact_id, "verification": report.to_dict()}
                )
            else:
                event.unresolved.append(
                    {"target": artifact_id, "reason": "verification-failed", "verification": report.to_dict()}
                )
        if event.state == RevocationState.REBUILDING:
            # every rebuild attempt failed before producing a candidate to verify
            event = transition(workspace.revocations, event, RevocationState.VERIFYING)
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
