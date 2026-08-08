"""Budget-aware active replay selection and baseline selection strategies."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    candidate_id: str
    inferred_score: float
    severity_weight: float = 1.0
    closure_impact: float = 1.0
    bridge_weight: float = 1.0
    uncertainty_value: Optional[float] = None
    fidelity_estimate: float = 1.0
    expected_cost: float = 1.0

    def priority(self) -> float:
        uncertainty = self.uncertainty_value
        if uncertainty is None:
            uncertainty = 1.0 - abs(self.inferred_score - 0.5) * 2  # max at score=0.5
        return (
            self.severity_weight
            * self.closure_impact
            * self.bridge_weight
            * max(uncertainty, EPSILON)
            * self.fidelity_estimate
        ) / max(self.expected_cost, EPSILON)


@dataclass(frozen=True, slots=True)
class Budget:
    max_replay_calls: Optional[int] = None
    max_wall_clock_seconds: Optional[float] = None
    max_input_output_tokens: Optional[int] = None
    max_cost: Optional[float] = None


@dataclass(frozen=True, slots=True)
class SelectionTrace:
    selected: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]  # (candidate_id, reason)
    order: tuple[str, ...]


def _within_budget(spent_calls: int, budget: Budget, calls_per_replay: int = 2) -> bool:
    if budget.max_replay_calls is None:
        return True
    return spent_calls + calls_per_replay <= budget.max_replay_calls


def select_active(
    candidates: list[ReplayCandidate], *, budget: Budget, calls_per_replay: int = 2
) -> SelectionTrace:
    ordered = sorted(candidates, key=lambda c: (-c.priority(), c.candidate_id))
    selected: list[str] = []
    skipped: list[tuple[str, str]] = []
    spent = 0
    for candidate in ordered:
        if _within_budget(spent, budget, calls_per_replay):
            selected.append(candidate.candidate_id)
            spent += calls_per_replay
        else:
            skipped.append((candidate.candidate_id, "budget-exhausted"))
    return SelectionTrace(selected=tuple(selected), skipped=tuple(skipped), order=tuple(c.candidate_id for c in ordered))


def select_exhaustive(candidates: list[ReplayCandidate], **_: object) -> SelectionTrace:
    ids = tuple(sorted(c.candidate_id for c in candidates))
    return SelectionTrace(selected=ids, skipped=(), order=ids)


def select_highest_inferred_first(candidates: list[ReplayCandidate], *, budget: Budget, calls_per_replay: int = 2) -> SelectionTrace:
    ordered = sorted(candidates, key=lambda c: (-c.inferred_score, c.candidate_id))
    selected, skipped, spent = [], [], 0
    for c in ordered:
        if _within_budget(spent, budget, calls_per_replay):
            selected.append(c.candidate_id)
            spent += calls_per_replay
        else:
            skipped.append((c.candidate_id, "budget-exhausted"))
    return SelectionTrace(tuple(selected), tuple(skipped), tuple(c.candidate_id for c in ordered))


def select_random(candidates: list[ReplayCandidate], *, budget: Budget, seed: int = 0, calls_per_replay: int = 2) -> SelectionTrace:
    rng = random.Random(seed)
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    selected, skipped, spent = [], [], 0
    for c in shuffled:
        if _within_budget(spent, budget, calls_per_replay):
            selected.append(c.candidate_id)
            spent += calls_per_replay
        else:
            skipped.append((c.candidate_id, "budget-exhausted"))
    return SelectionTrace(tuple(selected), tuple(skipped), tuple(c.candidate_id for c in shuffled))
