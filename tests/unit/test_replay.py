from __future__ import annotations

import pytest

from skillrewind.domain.enums import ArtifactKind, EvidenceClass, ReplayVerdict
from skillrewind.domain.models import Derivation
from skillrewind.replay.deterministic import register_fixture
from skillrewind.replay.selector import Budget, ReplayCandidate, select_active, select_exhaustive, select_random
from skillrewind.replay.service import run_paired_replay
from skillrewind.workspace import Workspace


def _canary_fixture(task_snapshot, available_context, seed):
    root_id = task_snapshot.get("root_marker")
    canary = root_id in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"]}


def _always_same_fixture(task_snapshot, available_context, seed):
    return {"policy": "safe", "_behavior_keys": ["policy"]}


def _broken_fixture(task_snapshot, available_context, seed):
    raise RuntimeError("simulated runner crash")


register_fixture("test-canary-recipe", _canary_fixture)
register_fixture("test-stable-recipe", _always_same_fixture)
register_fixture("test-broken-recipe", _broken_fixture)


def _make_ws_with_derivation(tmp_path, recipe: str, root_marker: str):
    ws = Workspace.init(tmp_path / "ws")
    root = ws.ingest_artifact(b"root", kind=ArtifactKind.AGENT_SKILL, logical_name="root")
    target = ws.ingest_artifact(b"target", kind=ArtifactKind.AGENT_SKILL, logical_name="target")
    derivation = Derivation(
        derivation_id="d1",
        recipe=recipe,
        recipe_version="1",
        target_artifact_id=target.artifact_id,
        candidate_context_pool=[root.artifact_id],
        task_snapshot={"root_marker": root.artifact_id if root_marker else None},
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        seed=42,
    )
    ws.derivations.upsert(derivation)
    return ws, root.artifact_id, target.artifact_id


def test_replay_confirms_when_behavior_changes(tmp_path):
    ws, root_id, _ = _make_ws_with_derivation(tmp_path, "test-canary-recipe", root_marker=True)
    outcome = run_paired_replay(ws, root_id, "d1")
    assert outcome.verdict == ReplayVerdict.CONFIRMED
    assert "mock_disable_verification" in outcome.changed_keys

    incoming = ws.edges.incoming(ws.derivations.get("d1").target_artifact_id)
    edge = next(e for e in incoming if e.source == root_id)
    assert edge.evidence_class == EvidenceClass.REPLAY_CONFIRMED
    ws.close()


def test_replay_rejects_when_behavior_unchanged(tmp_path):
    ws, root_id, _ = _make_ws_with_derivation(tmp_path, "test-stable-recipe", root_marker=True)
    outcome = run_paired_replay(ws, root_id, "d1")
    assert outcome.verdict == ReplayVerdict.REJECTED
    ws.close()


def test_replay_runner_failure_is_unresolved_not_rejected(tmp_path):
    ws, root_id, _ = _make_ws_with_derivation(tmp_path, "test-broken-recipe", root_marker=True)
    outcome = run_paired_replay(ws, root_id, "d1")
    assert outcome.verdict == ReplayVerdict.UNRESOLVED_RUNNER_FAILURE
    incoming = ws.edges.incoming(ws.derivations.get("d1").target_artifact_id)
    edge = next(e for e in incoming if e.source == root_id)
    assert edge.evidence_class == EvidenceClass.UNRESOLVED
    ws.close()


def test_replay_low_fidelity_becomes_unresolved(tmp_path, monkeypatch):
    ws, root_id, _ = _make_ws_with_derivation(tmp_path, "test-canary-recipe", root_marker=True)
    ws.config.replay_fidelity.minimum_confirmed = 1.1  # impossible to meet
    outcome = run_paired_replay(ws, root_id, "d1")
    assert outcome.verdict == ReplayVerdict.UNRESOLVED_FIDELITY
    ws.close()


def test_active_selector_respects_budget():
    candidates = [
        ReplayCandidate("a", inferred_score=0.9, expected_cost=1.0),
        ReplayCandidate("b", inferred_score=0.5, expected_cost=1.0),
        ReplayCandidate("c", inferred_score=0.1, expected_cost=1.0),
    ]
    trace = select_active(candidates, budget=Budget(max_replay_calls=4), calls_per_replay=2)
    assert len(trace.selected) == 2
    assert trace.skipped


def test_exhaustive_selector_selects_all():
    candidates = [ReplayCandidate("a", 0.5), ReplayCandidate("b", 0.9)]
    trace = select_exhaustive(candidates)
    assert set(trace.selected) == {"a", "b"}


def test_random_selector_is_deterministic_given_seed():
    candidates = [ReplayCandidate(f"c{i}", 0.5) for i in range(10)]
    t1 = select_random(candidates, budget=Budget(max_replay_calls=100), seed=7)
    t2 = select_random(candidates, budget=Budget(max_replay_calls=100), seed=7)
    assert t1.order == t2.order
