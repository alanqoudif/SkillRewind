"""Deterministic fixture runner: fully offline, used by demos, CI, and RewindBench.

A "recipe" is a registered pure function ``(task_snapshot, available_context,
seed) -> dict``. It must be deterministic given its inputs — no wall-clock,
no randomness unless seeded, no network. This is what makes the primary demo
and the test suite runnable without any paid model or network connection.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .base import DerivationRunner, ReplaySpec, RunnerOutput
from .base import register_runner

FixtureFn = Callable[[dict[str, Any], frozenset[str], Optional[int]], dict[str, Any]]

_FIXTURES: dict[str, FixtureFn] = {}


def register_fixture(recipe: str, fn: FixtureFn) -> None:
    _FIXTURES[recipe] = fn


def registered_recipes() -> list[str]:
    return sorted(_FIXTURES)


def get_fixture(recipe: str) -> Optional[FixtureFn]:
    return _FIXTURES.get(recipe)


class DeterministicFixtureRunner:
    name = "deterministic-fixture"

    def run(self, spec: ReplaySpec) -> RunnerOutput:
        fixture = _FIXTURES.get(spec.recipe)
        if fixture is None:
            return RunnerOutput(
                outputs={},
                target_behavior_vector={},
                error=f"no deterministic fixture registered for recipe {spec.recipe!r}",
            )
        try:
            result = fixture(spec.task_snapshot, spec.available_context, spec.seed)
        except Exception as exc:  # a runner failure must become "unresolved", never "rejected"
            return RunnerOutput(outputs={}, target_behavior_vector={}, error=str(exc))

        behavior_keys = result.get("_behavior_keys", list(result.keys()))
        behavior_vector = {k: result[k] for k in behavior_keys if k in result}
        return RunnerOutput(
            outputs=result,
            target_behavior_vector=behavior_vector,
            utility_metrics=result.get("_utility", {}),
            fidelity_components={
                "task_snapshot_match": 1.0,
                "model_match": 1.0,
                "environment_match": 1.0,
                "seed_support": 1.0 if spec.seed is not None else 0.5,
                "candidate_context_reconstruction": 1.0,
            },
            logs=(f"deterministic-fixture recipe={spec.recipe} intervention={spec.intervention_kind.value}",),
            model_identity="deterministic-fixture",
            environment_identity="in-process",
        )


register_runner(DeterministicFixtureRunner())
