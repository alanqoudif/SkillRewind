"""Operational-trace features: tool-call sequence/multiset similarity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .expression import jaccard


def _tool_names(tool_calls: list[dict[str, Any]]) -> list[str]:
    return [str(c.get("name")) for c in tool_calls if c.get("type") == "call" and c.get("name")]


def _sequence_edit_distance(a: list[str], b: list[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


@dataclass(frozen=True, slots=True)
class OperationalScore:
    sequence_similarity: float
    multiset_similarity: float
    combined: float


def operational_similarity(
    tool_calls_a: list[dict[str, Any]], tool_calls_b: list[dict[str, Any]]
) -> OperationalScore:
    seq_a = _tool_names(tool_calls_a)
    seq_b = _tool_names(tool_calls_b)

    if not seq_a and not seq_b:
        return OperationalScore(0.0, 0.0, 0.0)

    max_len = max(len(seq_a), len(seq_b), 1)
    distance = _sequence_edit_distance(seq_a, seq_b)
    sequence_similarity = 1.0 - (distance / max_len)

    multiset_similarity = jaccard(set(seq_a), set(seq_b))
    combined = 0.6 * sequence_similarity + 0.4 * multiset_similarity
    return OperationalScore(
        sequence_similarity=sequence_similarity,
        multiset_similarity=multiset_similarity,
        combined=combined,
    )
