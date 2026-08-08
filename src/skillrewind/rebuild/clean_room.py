"""Clean-room rebuild execution using the same registered deterministic
fixture recipes as replay (the recipe is re-invoked against the plan's
clean support set instead of the original, possibly-contaminated context).

Reusing the replay fixture registry (rather than inventing a second one)
keeps the rebuild honest: it is defined as "what the original recipe would
have produced given only clean support," not a separately hand-authored
"safe" output.
"""

from __future__ import annotations

from typing import Any

from ..replay.deterministic import get_fixture
from .planner import RebuildPlan


def execute_clean_room_recipe(plan: RebuildPlan, *, seed: int | None = None) -> dict[str, Any]:
    fixture = get_fixture(plan.recipe)
    if fixture is None:
        raise KeyError(f"no deterministic fixture registered for recipe {plan.recipe!r}; cannot rebuild")
    return fixture(plan.task_snapshot, frozenset(plan.clean_support), seed)
