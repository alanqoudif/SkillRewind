"""Service-mode paired-replay orchestration (Phase C2.2).

Reuses the existing, already-tested replay *science* unchanged: intervention
construction (`build_present_spec`/`build_withheld_spec`), the approved-
runner registry (`get_runner` -- no arbitrary module path or shell command
ever reaches this code, only a short registry-key string), output comparison
(`compare_target_behavior`), fidelity scoring (`build_fidelity_report`), and
paired-bootstrap statistics (`paired_binary_bootstrap`). Only the
orchestration/persistence differs from `skillrewind.replay.service.
run_paired_replay`: that function reads/writes a Lite-mode `Workspace`
directly, one-shot; this module is checkpoint-resumable over Service-mode
repositories, persisting one `ReplayRecord` per repetition so a crash and
retry never redoes or duplicates a repetition already on disk.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Callable, Optional

from ..config import SkillRewindConfig
from ..domain.enums import EvidenceClass, ReplayVerdict
from ..domain.models import Derivation as DomainDerivation
from .base import RunnerOutput, approved_runner_names, get_runner
from .comparator import compare_target_behavior
from .fidelity import build_fidelity_report
from .interventions import build_present_spec, build_withheld_spec
from .statistics import paired_binary_bootstrap

VERDICT_TO_EVIDENCE_CLASS = {
    ReplayVerdict.CONFIRMED: EvidenceClass.REPLAY_CONFIRMED.value,
    ReplayVerdict.REJECTED: EvidenceClass.REJECTED.value,
    ReplayVerdict.UNRESOLVED_FIDELITY: EvidenceClass.UNRESOLVED.value,
    ReplayVerdict.UNRESOLVED_REPETITIONS: EvidenceClass.UNRESOLVED.value,
    ReplayVerdict.UNRESOLVED_BUDGET: EvidenceClass.UNRESOLVED.value,
    ReplayVerdict.UNRESOLVED_RUNNER_FAILURE: EvidenceClass.UNRESOLVED.value,
    ReplayVerdict.UNRESOLVED_MISSING_INPUTS: EvidenceClass.UNRESOLVED.value,
}


class ReplayInputsUnavailable(Exception):
    """Raised when reconstruction inputs (the descendant's derivation) do not
    exist. Callers must classify this as UNRESOLVED_MISSING_INPUTS, never
    REJECTED -- missing inputs prove nothing about influence (invariant 2)."""


def build_domain_derivation(deriv_row: Any, derivation_inputs: list[Any]) -> DomainDerivation:
    """Adapts a Service-mode `Derivation` ORM row (+ its `DerivationInput`
    rows) into the Lite-mode `domain.models.Derivation` dataclass the
    existing `build_present_spec`/`build_withheld_spec` functions expect.
    Only a data-shape adaptation -- no intervention-construction logic lives
    here."""

    payload = deriv_row.payload_json or {}
    return DomainDerivation(
        derivation_id=deriv_row.derivation_id,
        recipe=deriv_row.recipe,
        recipe_version=deriv_row.recipe_version,
        target_artifact_id=deriv_row.target_artifact_id,
        recorded_inputs=[i.parent_artifact_id for i in derivation_inputs],
        candidate_context_pool=list(payload.get("candidate_context_pool", [])),
        task_snapshot=payload.get("task_snapshot", {}),
        seed=payload.get("seed"),
        started_at=deriv_row.started_at.isoformat() if deriv_row.started_at else None,
        ended_at=deriv_row.ended_at.isoformat() if deriv_row.ended_at else None,
    )


def _with_repetition(spec, index: int):
    seed = None if spec.seed is None else spec.seed + index
    return replace(spec, seed=seed, repetition_index=index)


def validate_runner(runner_name: str) -> None:
    if runner_name not in approved_runner_names():
        raise ValueError(f"runner {runner_name!r} is not in the approved registry: {approved_runner_names()}")


class ReplayRunResult:
    __slots__ = (
        "verdict",
        "fidelity_overall",
        "effect_estimate",
        "repetitions_run",
        "cancelled",
        "error_message",
    )

    def __init__(
        self,
        *,
        verdict: Optional[ReplayVerdict],
        fidelity_overall: Optional[float],
        effect_estimate: Optional[float],
        repetitions_run: int,
        cancelled: bool,
        error_message: Optional[str] = None,
    ) -> None:
        self.verdict = verdict
        self.fidelity_overall = fidelity_overall
        self.effect_estimate = effect_estimate
        self.repetitions_run = repetitions_run
        self.cancelled = cancelled
        self.error_message = error_message


def run_service_replay(
    *,
    domain_derivation: DomainDerivation,
    ancestor_artifact_id: str,
    runner_name: str,
    repetitions_requested: int,
    already_done: dict[int, dict[str, Any]],
    config: SkillRewindConfig,
    persist_repetition: Callable[[int, str, Optional[str], dict[str, Any]], None],
    checkpoint: Callable[[int], None],
    should_cancel: Optional[Callable[[], bool]] = None,
) -> ReplayRunResult:
    """Runs (or resumes) the repetitions of a paired present/withheld replay.

    `persist_repetition(index, replay_id, verdict, payload)` and
    `checkpoint(index)` are injected so this function has no direct
    Service-mode session dependency -- it is pure orchestration over the
    reused replay-science functions, testable without a database. Repetition
    indices already present in `already_done` (a map of repetition index ->
    that repetition's previously persisted payload) are not re-executed --
    their outcome is reconstructed from the persisted payload instead, so
    the final classification after a resume is identical to what it would
    have been in one uninterrupted run, and no repetition's evidence is ever
    recomputed or duplicated.
    """

    validate_runner(runner_name)
    runner = get_runner(runner_name)

    outcomes: list[bool] = []
    last_present: Optional[RunnerOutput] = None
    last_withheld: Optional[RunnerOutput] = None
    error: Optional[str] = None
    repetitions_run = 0

    for i in range(max(1, repetitions_requested)):
        if should_cancel is not None and should_cancel():
            return ReplayRunResult(
                verdict=None, fidelity_overall=None, effect_estimate=None, repetitions_run=repetitions_run, cancelled=True
            )

        if i in already_done:
            stored = already_done[i]
            repetitions_run += 1
            if "error" in stored:
                error = stored["error"]
                break
            outcomes.append(bool(stored.get("any_change")))
            fc = stored.get("fidelity_components", {})
            last_present = RunnerOutput(
                outputs=stored.get("present_outputs", {}),
                target_behavior_vector=stored.get("present_vector", {}),
                fidelity_components=fc.get("present", {}),
            )
            last_withheld = RunnerOutput(
                outputs=stored.get("withheld_outputs", {}),
                target_behavior_vector=stored.get("withheld_vector", {}),
                fidelity_components=fc.get("withheld", {}),
            )
            continue

        present_spec = _with_repetition(build_present_spec(domain_derivation, ancestor_artifact_id), i)
        withheld_spec = _with_repetition(build_withheld_spec(domain_derivation, ancestor_artifact_id), i)

        present_out = runner.run(present_spec)
        withheld_out = runner.run(withheld_spec)
        last_present, last_withheld = present_out, withheld_out
        replay_id = f"replay-{uuid.uuid4()}"

        if present_out.error or withheld_out.error:
            error = present_out.error or withheld_out.error
            persist_repetition(
                i,
                replay_id,
                ReplayVerdict.UNRESOLVED_RUNNER_FAILURE.value,
                {"error": error, "present_logs": list(present_out.logs), "withheld_logs": list(withheld_out.logs)},
            )
            checkpoint(i + 1)
            repetitions_run += 1
            break

        comparison = compare_target_behavior(present_out, withheld_out)
        outcomes.append(comparison.any_change)
        persist_repetition(
            i,
            replay_id,
            None,
            {
                "present_outputs": present_out.outputs,
                "withheld_outputs": withheld_out.outputs,
                "present_vector": comparison.present_vector,
                "withheld_vector": comparison.withheld_vector,
                "changed_keys": list(comparison.changed_keys),
                "any_change": comparison.any_change,
                "fidelity_components": {
                    "present": present_out.fidelity_components,
                    "withheld": withheld_out.fidelity_components,
                },
            },
        )
        checkpoint(i + 1)
        repetitions_run += 1

    if error is not None:
        return ReplayRunResult(
            verdict=ReplayVerdict.UNRESOLVED_RUNNER_FAILURE,
            fidelity_overall=0.0,
            effect_estimate=None,
            repetitions_run=repetitions_run,
            cancelled=False,
            error_message=error,
        )

    assert last_present is not None and last_withheld is not None  # loop always executes >=1 iteration when not resuming a fully-done run
    fidelity = build_fidelity_report(last_present.fidelity_components, last_withheld.fidelity_components)
    comparison = compare_target_behavior(last_present, last_withheld)
    stats = paired_binary_bootstrap(outcomes, seed=domain_derivation.seed or 0)

    if not fidelity.meets_minimum(config.replay_fidelity.minimum_confirmed):
        verdict = ReplayVerdict.UNRESOLVED_FIDELITY
    elif repetitions_requested > 1 and not stats.sufficient_sample:
        verdict = ReplayVerdict.UNRESOLVED_REPETITIONS
    elif comparison.any_change:
        verdict = ReplayVerdict.CONFIRMED
    else:
        verdict = ReplayVerdict.REJECTED

    return ReplayRunResult(
        verdict=verdict,
        fidelity_overall=fidelity.overall(),
        effect_estimate=stats.effect_size,
        repetitions_run=repetitions_run,
        cancelled=False,
    )
