"""Materialize a :class:`BenchmarkCase` into a real workspace, and executable
baseline methods that only ever see :attr:`BenchmarkCase.observed_edges`
(never the oracle graph)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.enums import ArtifactKind, EvidenceClass, RelationType
from ..domain.models import Derivation, InfluenceEdge
from ..lineage.candidates import recover_candidates
from ..lineage.closure import recorded_descendants
from ..replay.deterministic import register_fixture
from ..replay.service import run_paired_replay
from ..workspace import Workspace, timestamp
from .cases import BenchmarkCase

BENCH_RECIPE = "rewindbench-poisoned-descendant"


def _fixture(task_snapshot: dict, available_context: frozenset, seed) -> dict:
    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {"mock_disable_verification": canary, "_behavior_keys": ["mock_disable_verification"], "_utility": {"task_success": 1.0}}


register_fixture(BENCH_RECIPE, _fixture)


def materialize_case(case: BenchmarkCase, workspace_dir: str | Path) -> Workspace:
    """Build a workspace containing only what an evaluated method may observe:
    the oracle artifacts' content/metadata, but only ``observed_edges``."""

    ws = Workspace.init(workspace_dir)
    for artifact in case.artifacts:
        ingested = ws.ingest_artifact(
            json.dumps({"name": artifact.logical_name, "body": artifact.body}).encode(),
            kind=ArtifactKind.AGENT_SKILL,
            logical_name=artifact.logical_name,
            metadata={
                "agent_skills_manifest": {"name": artifact.logical_name, "description": artifact.logical_name, "body": artifact.body, "files": []},
                "canary_activated": artifact.canary,
                "probes": {"mock_disable_verification": artifact.canary},
            },
        )
        ws.derivations.upsert(
            Derivation(
                derivation_id=f"deriv-{artifact.logical_name}",
                recipe=BENCH_RECIPE,
                recipe_version="0.1",
                target_artifact_id=ingested.artifact_id,
                task_snapshot=artifact.task_snapshot,
                started_at="2026-01-01T00:00:00Z",
                ended_at="2026-01-01T00:00:01Z",
                seed=case.seed,
            )
        )

    now = timestamp()
    for edge in case.observed_edges:
        ws.edges.upsert(
            InfluenceEdge(
                source=edge.source, target=edge.target, relation=RelationType.USED_AS_INPUT,
                evidence_class=EvidenceClass.RECORDED, created_at=now, updated_at=now,
            )
        )
    return ws


@dataclass(frozen=True, slots=True)
class Prediction:
    method: str
    case_id: str
    predicted_hidden_descendants: frozenset[str]
    predicted_edges: frozenset[tuple[str, str]]
    replay_calls: int


def run_delete_root(case: BenchmarkCase, ws: Workspace) -> Prediction:
    return Prediction("delete-root", case.case_id, frozenset(), frozenset(), 0)


def run_recorded_closure(case: BenchmarkCase, ws: Workspace) -> Prediction:
    closure = set(recorded_descendants(ws, [case.revoked_root], include_roots=False))
    return Prediction("recorded-closure", case.case_id, frozenset(closure), frozenset((case.revoked_root, t) for t in closure), 0)


def run_static_multitrace(case: BenchmarkCase, ws: Workspace) -> Prediction:
    closure = set(recorded_descendants(ws, [case.revoked_root], include_roots=True))
    predicted: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for artifact in ws.artifacts.list(limit=10_000):
        if artifact.artifact_id in closure:
            continue
        results = recover_candidates(ws, artifact.artifact_id, persist=False)
        for r in results:
            if r.candidate_id == case.revoked_root and r.result.is_candidate:
                predicted.add(artifact.artifact_id)
                edges.add((case.revoked_root, artifact.artifact_id))
    return Prediction("static-multitrace", case.case_id, frozenset(predicted), frozenset(edges), 0)


def run_exhaustive_replay(case: BenchmarkCase, ws: Workspace) -> Prediction:
    static = run_static_multitrace(case, ws)
    confirmed: set[str] = set()
    edges: set[tuple[str, str]] = set()
    replay_calls = 0
    for target in static.predicted_hidden_descendants:
        derivation = ws.derivations.find_by_target(target)
        if derivation is None:
            continue
        outcome = run_paired_replay(ws, case.revoked_root, derivation.derivation_id)
        replay_calls += 2
        if outcome.verdict.value == "confirmed":
            confirmed.add(target)
            edges.add((case.revoked_root, target))
    return Prediction("exhaustive-replay", case.case_id, frozenset(confirmed), frozenset(edges), replay_calls)


BASELINES: dict[str, Any] = {
    "delete-root": run_delete_root,
    "recorded-closure": run_recorded_closure,
    "static-multitrace": run_static_multitrace,
    "exhaustive-replay": run_exhaustive_replay,
}


def run_method(method: str, case: BenchmarkCase, workspace_root: Path) -> Prediction:
    with tempfile.TemporaryDirectory(dir=str(workspace_root), prefix=f"{case.case_id}-") as tmp:
        ws = materialize_case(case, tmp)
        try:
            return BASELINES[method](case, ws)
        finally:
            ws.close()
