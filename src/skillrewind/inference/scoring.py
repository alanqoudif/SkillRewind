"""Deterministic, explainable weighted candidate scorer.

Combines the feature families in :mod:`skillrewind.features` into a single
score using configurable weights (default weights mirror the illustrative
values in the spec and are provisional/hand-tuned, not learned — see
``docs/adr/0001-evidence-classes.md``). Missing feature families (e.g. no
behavioral probes recorded) are excluded and the remaining weights are
renormalized, with the omission reported explicitly rather than silently
treated as zero-similarity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import FeatureWeights, ScoringThresholds

SCORER_VERSION = "static-multitrace-0.2.0"


@dataclass(frozen=True, slots=True)
class FeatureBreakdown:
    expression: Optional[float]
    implementation: Optional[float]
    operational: Optional[float]
    behavioral: Optional[float]
    graph: Optional[float]
    temporal: Optional[float]

    def as_dict(self) -> dict[str, Optional[float]]:
        return {
            "expression": self.expression,
            "implementation": self.implementation,
            "operational": self.operational,
            "behavioral": self.behavioral,
            "graph": self.graph,
            "temporal": self.temporal,
        }

    def missing(self) -> list[str]:
        return [name for name, value in self.as_dict().items() if value is None]


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    breakdown: FeatureBreakdown
    missing_features: list[str]
    scorer_version: str
    weights_used: dict[str, float]
    is_high_confidence: bool
    is_candidate: bool
    strict_negative_signal: bool


def score_candidate(
    breakdown: FeatureBreakdown,
    *,
    weights: FeatureWeights,
    thresholds: ScoringThresholds,
    strict_negative_signal: bool = False,
) -> ScoreResult:
    values = breakdown.as_dict()
    weight_map = {
        "expression": weights.expression,
        "implementation": weights.implementation,
        "operational": weights.operational,
        "behavioral": weights.behavioral,
        "graph": weights.graph,
        "temporal": weights.temporal,
    }
    available = {k: v for k, v in values.items() if v is not None}
    missing = breakdown.missing()

    if not available:
        score = 0.0
        weights_used: dict[str, float] = {}
    else:
        total_weight = sum(weight_map[k] for k in available)
        if total_weight <= 0:
            score = 0.0
            weights_used = {}
        else:
            weights_used = {k: weight_map[k] / total_weight for k in available}
            score = sum(available[k] * weights_used[k] for k in available)

    is_candidate = score >= thresholds.candidate
    is_high_confidence = score >= thresholds.high_confidence

    return ScoreResult(
        score=round(score, 6),
        breakdown=breakdown,
        missing_features=missing,
        scorer_version=SCORER_VERSION,
        weights_used=weights_used,
        is_high_confidence=is_high_confidence and not strict_negative_signal,
        is_candidate=is_candidate,
        strict_negative_signal=strict_negative_signal,
    )
