"""Replay fidelity: exposed as components, never collapsed to an opaque magic number."""

from __future__ import annotations

from dataclasses import dataclass

_COMPONENTS = (
    "task_snapshot_match",
    "model_match",
    "environment_match",
    "seed_support",
    "candidate_context_reconstruction",
)


@dataclass(frozen=True, slots=True)
class FidelityReport:
    components: dict[str, float]

    def overall(self) -> float:
        if not self.components:
            return 0.0
        return sum(self.components.values()) / len(self.components)

    def meets_minimum(self, minimum: float) -> bool:
        return self.overall() >= minimum

    def to_dict(self) -> dict[str, float]:
        return {**self.components, "overall": round(self.overall(), 6)}


def build_fidelity_report(present_components: dict[str, float], withheld_components: dict[str, float]) -> FidelityReport:
    keys = set(_COMPONENTS) & (set(present_components) | set(withheld_components))
    merged = {
        key: min(present_components.get(key, 0.0), withheld_components.get(key, 0.0)) for key in keys
    }
    return FidelityReport(components=merged)
