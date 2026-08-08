"""Composable output comparators for paired replay results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..canonical.json import canonical_bytes
from .base import RunnerOutput


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    changed_keys: tuple[str, ...]
    present_vector: dict[str, Any]
    withheld_vector: dict[str, Any]
    any_change: bool
    utility_delta: dict[str, float]


def compare_target_behavior(present: RunnerOutput, withheld: RunnerOutput) -> ComparisonResult:
    """Compare present vs withheld target-behavior vectors key by key.

    Comparison uses canonical-JSON equality, so it is defined for any JSON
    value (bool canaries, string policy choices, structured tool-call
    graphs) without relying on semantic/LLM-based judgment.
    """

    keys = sorted(set(present.target_behavior_vector) | set(withheld.target_behavior_vector))
    changed = [
        key
        for key in keys
        if canonical_bytes(present.target_behavior_vector.get(key))
        != canonical_bytes(withheld.target_behavior_vector.get(key))
    ]
    utility_keys = sorted(set(present.utility_metrics) | set(withheld.utility_metrics))
    utility_delta = {
        key: float(present.utility_metrics.get(key, 0.0)) - float(withheld.utility_metrics.get(key, 0.0))
        for key in utility_keys
        if isinstance(present.utility_metrics.get(key, 0.0), (int, float))
        and isinstance(withheld.utility_metrics.get(key, 0.0), (int, float))
    }
    return ComparisonResult(
        changed_keys=tuple(changed),
        present_vector=dict(present.target_behavior_vector),
        withheld_vector=dict(withheld.target_behavior_vector),
        any_change=bool(changed),
        utility_delta=utility_delta,
    )


def canary_absent(output: RunnerOutput, canary_key: str) -> bool:
    return not bool(output.target_behavior_vector.get(canary_key, False))
