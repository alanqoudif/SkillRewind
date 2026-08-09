"""End-to-end paired replay: run -> compare -> fidelity -> classify -> persist."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from ..config import SkillRewindConfig
from ..domain.enums import EvidenceClass, RelationType, ReplayVerdict
from ..domain.models import InfluenceEdge, ReplayRecord
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike
from .base import RunnerOutput, get_runner
from .comparator import compare_target_behavior
from .fidelity import build_fidelity_report
from .interventions import build_present_spec, build_withheld_spec
from .statistics import paired_binary_bootstrap


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    replay_id: str
    verdict: ReplayVerdict
    fidelity_overall: float
    changed_keys: tuple[str, ...]
    record: ReplayRecord


def run_paired_replay(
    workspace: WorkspaceLike,
    candidate_ancestor_id: str,
    target_derivation_id: str,
    *,
    runner_name: str = "deterministic-fixture",
    repetitions: int = 1,
    config: Optional[SkillRewindConfig] = None,
) -> ReplayOutcome:
    config = config or workspace.config
    derivation = workspace.derivations.get(target_derivation_id)
    runner = get_runner(runner_name)

    outcomes: list[bool] = []
    last_present: Optional[RunnerOutput] = None
    last_withheld: Optional[RunnerOutput] = None
    error: Optional[str] = None

    for i in range(max(1, repetitions)):
        present_spec = build_present_spec(derivation, candidate_ancestor_id)
        withheld_spec = build_withheld_spec(derivation, candidate_ancestor_id)
        present_spec = _with_repetition(present_spec, i)
        withheld_spec = _with_repetition(withheld_spec, i)

        present_out = runner.run(present_spec)
        withheld_out = runner.run(withheld_spec)
        last_present, last_withheld = present_out, withheld_out

        if present_out.error or withheld_out.error:
            error = present_out.error or withheld_out.error
            break

        comparison = compare_target_behavior(present_out, withheld_out)
        outcomes.append(comparison.any_change)

    replay_id = f"replay-{uuid.uuid4()}"
    now = timestamp()

    if error is not None:
        record = ReplayRecord(
            replay_id=replay_id,
            target_derivation_id=target_derivation_id,
            candidate_ancestor_id=candidate_ancestor_id,
            intervention_kind="present-withheld-pair",
            model_identity=runner_name,
            environment_identity=None,
            seed=derivation.seed,
            repetitions=len(outcomes),
            normalized_outputs={},
            target_behavior_vector={},
            utility_metrics={},
            effect_estimate=None,
            fidelity={},
            verdict=ReplayVerdict.UNRESOLVED_RUNNER_FAILURE,
            cost={"replay_calls": 2 * (len(outcomes) + 1)},
            created_at=now,
        )
        workspace.replays.insert(record)
        _persist_edge(workspace, candidate_ancestor_id, target_derivation_id, derivation, EvidenceClass.UNRESOLVED, None, record, now)
        return ReplayOutcome(replay_id, ReplayVerdict.UNRESOLVED_RUNNER_FAILURE, 0.0, (), record)

    assert last_present is not None and last_withheld is not None  # loop always runs >=1 iteration
    fidelity = build_fidelity_report(last_present.fidelity_components, last_withheld.fidelity_components)
    comparison = compare_target_behavior(last_present, last_withheld)
    stats = paired_binary_bootstrap(outcomes, seed=derivation.seed or 0)

    if not fidelity.meets_minimum(config.replay_fidelity.minimum_confirmed):
        verdict = ReplayVerdict.UNRESOLVED_FIDELITY
    elif repetitions > 1 and not stats.sufficient_sample:
        verdict = ReplayVerdict.UNRESOLVED_REPETITIONS
    elif comparison.any_change:
        verdict = ReplayVerdict.CONFIRMED
    else:
        verdict = ReplayVerdict.REJECTED

    record = ReplayRecord(
        replay_id=replay_id,
        target_derivation_id=target_derivation_id,
        candidate_ancestor_id=candidate_ancestor_id,
        intervention_kind="present-withheld-pair",
        model_identity=last_present.model_identity,
        environment_identity=last_present.environment_identity,
        seed=derivation.seed,
        repetitions=len(outcomes),
        normalized_outputs={"present": last_present.outputs, "withheld": last_withheld.outputs},
        target_behavior_vector={"present": comparison.present_vector, "withheld": comparison.withheld_vector},
        utility_metrics=comparison.utility_delta,
        effect_estimate=stats.effect_size,
        fidelity=fidelity.to_dict(),
        verdict=verdict,
        cost={"replay_calls": 2 * len(outcomes)},
        created_at=now,
    )
    workspace.replays.insert(record)

    evidence_class = {
        ReplayVerdict.CONFIRMED: EvidenceClass.REPLAY_CONFIRMED,
        ReplayVerdict.REJECTED: EvidenceClass.REJECTED,
        ReplayVerdict.UNRESOLVED_FIDELITY: EvidenceClass.UNRESOLVED,
        ReplayVerdict.UNRESOLVED_REPETITIONS: EvidenceClass.UNRESOLVED,
    }[verdict]
    _persist_edge(workspace, candidate_ancestor_id, target_derivation_id, derivation, evidence_class, stats.effect_size, record, now)

    workspace.audit.append(
        "replay.completed",
        config.actor,
        {"replay_id": replay_id, "candidate": candidate_ancestor_id, "verdict": verdict.value},
    )
    return ReplayOutcome(replay_id, verdict, fidelity.overall(), comparison.changed_keys, record)


def _with_repetition(spec, index: int):
    from dataclasses import replace

    seed = None if spec.seed is None else spec.seed + index
    return replace(spec, seed=seed, repetition_index=index)


def _persist_edge(workspace, source, target_derivation_id, derivation, evidence_class, confidence, record, now):
    target_artifact_id = derivation.target_artifact_id
    if not target_artifact_id:
        return
    existing = [
        e for e in workspace.edges.incoming(target_artifact_id) if e.source == source
    ]
    evidence_entry = {
        "kind": "counterfactual-replay",
        "replay_id": record.replay_id,
        "verdict": record.verdict.value if record.verdict else None,
        "fidelity": record.fidelity,
    }
    prior_evidence = existing[0].evidence if existing else []
    edge = InfluenceEdge(
        source=source,
        target=target_artifact_id,
        relation=RelationType.HIDDEN_INFLUENCE,
        evidence_class=evidence_class,
        confidence=confidence,
        evidence=[*prior_evidence, evidence_entry],
        intervention={"replay_id": record.replay_id},
        created_at=existing[0].created_at if existing else now,
        updated_at=now,
        scorer_version=existing[0].scorer_version if existing else None,
    )
    workspace.edges.upsert(edge)
