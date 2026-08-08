"""Paired-intervention spec construction: present / withheld / controls."""

from __future__ import annotations

from typing import Optional

from ..domain.enums import InterventionKind
from ..domain.models import Derivation
from .base import ReplaySpec


def build_present_spec(
    derivation: Derivation, candidate_ancestor_id: str, *, seed: Optional[int] = None
) -> ReplaySpec:
    context = set(derivation.candidate_context_pool) | set(derivation.recorded_inputs) | {candidate_ancestor_id}
    return ReplaySpec(
        target_derivation_id=derivation.derivation_id,
        recipe=derivation.recipe,
        task_snapshot=derivation.task_snapshot,
        available_context=frozenset(context),
        candidate_ancestor_id=candidate_ancestor_id,
        intervention_kind=InterventionKind.PRESENT,
        seed=seed if seed is not None else derivation.seed,
    )


def build_withheld_spec(
    derivation: Derivation, candidate_ancestor_id: str, *, seed: Optional[int] = None
) -> ReplaySpec:
    context = (set(derivation.candidate_context_pool) | set(derivation.recorded_inputs)) - {candidate_ancestor_id}
    return ReplaySpec(
        target_derivation_id=derivation.derivation_id,
        recipe=derivation.recipe,
        task_snapshot=derivation.task_snapshot,
        available_context=frozenset(context),
        candidate_ancestor_id=candidate_ancestor_id,
        intervention_kind=InterventionKind.WITHHELD,
        seed=seed if seed is not None else derivation.seed,
    )


def build_clean_control_spec(
    derivation: Derivation, candidate_ancestor_id: str, control_artifact_id: str, *, seed: Optional[int] = None
) -> ReplaySpec:
    context = (
        set(derivation.candidate_context_pool) | set(derivation.recorded_inputs)
    ) - {candidate_ancestor_id} | {control_artifact_id}
    return ReplaySpec(
        target_derivation_id=derivation.derivation_id,
        recipe=derivation.recipe,
        task_snapshot=derivation.task_snapshot,
        available_context=frozenset(context),
        candidate_ancestor_id=candidate_ancestor_id,
        intervention_kind=InterventionKind.CLEAN_CONTROL,
        seed=seed if seed is not None else derivation.seed,
    )


def build_irrelevant_control_spec(
    derivation: Derivation, candidate_ancestor_id: str, irrelevant_artifact_id: str, *, seed: Optional[int] = None
) -> ReplaySpec:
    context = set(derivation.candidate_context_pool) | set(derivation.recorded_inputs) | {irrelevant_artifact_id}
    return ReplaySpec(
        target_derivation_id=derivation.derivation_id,
        recipe=derivation.recipe,
        task_snapshot=derivation.task_snapshot,
        available_context=frozenset(context),
        candidate_ancestor_id=candidate_ancestor_id,
        intervention_kind=InterventionKind.IRRELEVANT_CONTROL,
        seed=seed if seed is not None else derivation.seed,
    )
