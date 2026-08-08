from __future__ import annotations

import tempfile

from skillrewind.bench.cases import PRESETS, generate_batch, generate_case
from skillrewind.bench.harness import materialize_case, run_method
from skillrewind.bench.metrics import aggregate, score_prediction


def test_generate_case_is_deterministic_given_seed():
    a = generate_case(scenario_family="direct-inheritance", seed=1, case_index=0, provenance_loss_rate=0.5)
    b = generate_case(scenario_family="direct-inheritance", seed=1, case_index=0, provenance_loss_rate=0.5)
    assert a.case_id == b.case_id
    assert a.observed_edges == b.observed_edges
    assert a.expected_hidden_descendants == b.expected_hidden_descendants


def test_oracle_ids_match_materialized_workspace_ids():
    case = generate_case(scenario_family="direct-inheritance", seed=7, case_index=0, provenance_loss_rate=1.0)
    with tempfile.TemporaryDirectory() as tmp:
        ws = materialize_case(case, tmp)
        artifact_ids = {a.artifact_id for a in ws.artifacts.list(limit=10)}
        assert case.revoked_root in artifact_ids
        for hidden in case.expected_hidden_descendants:
            assert hidden in artifact_ids
        ws.close()


def test_delete_root_and_recorded_closure_baselines_miss_hidden_descendants():
    case = generate_case(scenario_family="direct-inheritance", seed=7, case_index=0, provenance_loss_rate=1.0)
    assert case.expected_hidden_descendants  # provenance_loss_rate=1.0 always drops the edge
    with tempfile.TemporaryDirectory() as tmp:
        pred_delete = run_method("delete-root", case, __import__("pathlib").Path(tmp))
        pred_closure = run_method("recorded-closure", case, __import__("pathlib").Path(tmp))
    assert pred_delete.predicted_hidden_descendants == frozenset()
    assert set(case.expected_hidden_descendants) - pred_closure.predicted_hidden_descendants


def test_static_multitrace_recovers_hidden_descendant_without_false_quarantine():
    case = generate_case(scenario_family="direct-inheritance", seed=7, case_index=0, provenance_loss_rate=1.0)
    with tempfile.TemporaryDirectory() as tmp:
        pred = run_method("static-multitrace", case, __import__("pathlib").Path(tmp))
    score = score_prediction(case, pred)
    assert score.true_positives >= 1
    assert score.false_quarantined_negatives == 0


def test_exhaustive_replay_confirms_and_never_flags_strict_negative():
    case = generate_case(scenario_family="direct-inheritance", seed=7, case_index=0, provenance_loss_rate=1.0)
    with tempfile.TemporaryDirectory() as tmp:
        pred = run_method("exhaustive-replay", case, __import__("pathlib").Path(tmp))
    assert set(case.expected_hidden_descendants).issubset(pred.predicted_hidden_descendants)
    assert not (pred.predicted_hidden_descendants & set(case.strict_negatives))
    assert pred.replay_calls > 0


def test_smoke_preset_generates_cases_and_scores_aggregate():
    preset = PRESETS["smoke"]
    cases = generate_batch(preset=preset, seed=42)
    assert len(cases) == preset.cases_per_combination
    with tempfile.TemporaryDirectory() as tmp:
        scores = []
        for case in cases:
            pred = run_method("static-multitrace", case, __import__("pathlib").Path(tmp))
            scores.append(score_prediction(case, pred))
    agg = aggregate(scores, method="static-multitrace")
    assert agg.n_cases == len(cases)
    assert not agg.sufficient_for_confidence_intervals  # smoke preset is intentionally tiny
