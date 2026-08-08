from __future__ import annotations

from skillrewind.config import FeatureWeights, ScoringThresholds
from skillrewind.features.expression import expression_similarity
from skillrewind.features.implementation import implementation_similarity
from skillrewind.features.operational import operational_similarity
from skillrewind.features.temporal import temporal_proximity
from skillrewind.inference.scoring import FeatureBreakdown, score_candidate


def test_expression_similarity_high_for_near_duplicate_text():
    a = "Deploy a fast HTTP service with mocked TLS verification handling."
    b = "deploy a FAST http service, with mocked tls verification handling!"
    score = expression_similarity(a, b)
    assert score.combined > 0.6


def test_expression_similarity_low_for_unrelated_text():
    a = "Deploy a fast HTTP service."
    b = "Compute the Fibonacci sequence recursively."
    score = expression_similarity(a, b)
    assert score.combined < 0.3


def test_implementation_similarity_survives_identifier_renaming():
    src_a = "import requests\n\ndef deploy(host):\n    if host:\n        requests.get(host)\n"
    src_b = "import requests\n\ndef ship(target):\n    if target:\n        requests.get(target)\n"
    score = implementation_similarity(src_a, src_b)
    assert score.used_ast
    assert score.combined > 0.7


def test_implementation_similarity_falls_back_on_unparsable_source():
    score = implementation_similarity("def broken(:", "def also broken(:")
    assert not score.used_ast


def test_operational_similarity_matches_identical_tool_sequences():
    calls = [{"type": "call", "name": "http_get"}, {"type": "call", "name": "http_post"}]
    score = operational_similarity(calls, calls)
    assert score.combined == 1.0


def test_operational_similarity_zero_for_empty_vs_nonempty():
    calls = [{"type": "call", "name": "http_get"}]
    score = operational_similarity([], calls)
    assert score.sequence_similarity == 0.0


def test_temporal_proximity_decays_with_distance():
    close = temporal_proximity("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", half_life_seconds=3600)
    far = temporal_proximity("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", half_life_seconds=3600)
    assert close > far
    assert temporal_proximity(None, "2026-01-01T00:00:00Z") == 0.0


def test_score_candidate_renormalizes_missing_features():
    breakdown = FeatureBreakdown(
        expression=0.9, implementation=None, operational=None, behavioral=None, graph=0.1, temporal=None
    )
    weights = FeatureWeights()
    thresholds = ScoringThresholds()
    result = score_candidate(breakdown, weights=weights, thresholds=thresholds)
    assert set(result.missing_features) == {"implementation", "operational", "behavioral", "temporal"}
    assert abs(sum(result.weights_used.values()) - 1.0) < 1e-9
    assert result.is_candidate


def test_score_candidate_strict_negative_suppresses_high_confidence():
    breakdown = FeatureBreakdown(
        expression=0.95, implementation=0.95, operational=0.95, behavioral=0.95, graph=0.95, temporal=0.95
    )
    result = score_candidate(
        breakdown, weights=FeatureWeights(), thresholds=ScoringThresholds(), strict_negative_signal=True
    )
    assert result.is_candidate
    assert not result.is_high_confidence
