"""RewindBench-core: controlled oracle case generation.

Generates deterministic ``poisoned-descendant``-shaped benchmark cases: a
root artifact with an inert mocked canary, a hidden descendant whose
recorded derivation omits the influence edge (the "provenance loss"), and an
independently-developed strict-negative artifact with the same surface
purpose but no causal relationship to the root.

The oracle (true edges, true hidden-descendant set) is kept in a separate
object from what an evaluated method receives (``ObservedCase``), so a
baseline cannot cheat by reading ground truth.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Literal

from ..domain.ids import build_artifact_id

ProvenanceLossOperator = Literal["none", "uniform-random-dropout", "type-selective-dropout"]

SCENARIO_FAMILIES = (
    "direct-inheritance",
    "semantic-laundering",
    "implementation-mutation",
)

DEFAULT_RATES = (0.0, 0.01, 0.05, 0.10, 0.25, 0.50)


@dataclass(frozen=True, slots=True)
class OracleArtifact:
    artifact_id: str
    logical_name: str
    body: str
    canary: bool
    task_snapshot: dict


@dataclass(frozen=True, slots=True)
class OracleEdge:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    seed: int
    scenario_family: str
    provenance_loss_operator: ProvenanceLossOperator
    provenance_loss_rate: float
    artifacts: tuple[OracleArtifact, ...]
    oracle_edges: tuple[OracleEdge, ...]  # ground truth: full influence graph
    observed_edges: tuple[OracleEdge, ...]  # what the evaluated method may see
    revoked_root: str
    expected_hidden_descendants: tuple[str, ...]  # oracle_edges targets - observed_edges targets
    strict_negatives: tuple[str, ...]
    actual_removed_fraction: float = field(default=0.0)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id, "seed": self.seed, "scenario_family": self.scenario_family,
            "provenance_loss_operator": self.provenance_loss_operator, "provenance_loss_rate": self.provenance_loss_rate,
            "artifacts": [
                {"artifact_id": a.artifact_id, "logical_name": a.logical_name, "body": a.body, "canary": a.canary, "task_snapshot": a.task_snapshot}
                for a in self.artifacts
            ],
            "oracle_edges": [{"source": e.source, "target": e.target} for e in self.oracle_edges],
            "observed_edges": [{"source": e.source, "target": e.target} for e in self.observed_edges],
            "revoked_root": self.revoked_root,
            "expected_hidden_descendants": list(self.expected_hidden_descendants),
            "strict_negatives": list(self.strict_negatives),
            "actual_removed_fraction": self.actual_removed_fraction,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkCase":
        return cls(
            case_id=data["case_id"], seed=data["seed"], scenario_family=data["scenario_family"],
            provenance_loss_operator=data["provenance_loss_operator"], provenance_loss_rate=data["provenance_loss_rate"],
            artifacts=tuple(OracleArtifact(**a) for a in data["artifacts"]),
            oracle_edges=tuple(OracleEdge(**e) for e in data["oracle_edges"]),
            observed_edges=tuple(OracleEdge(**e) for e in data["observed_edges"]),
            revoked_root=data["revoked_root"],
            expected_hidden_descendants=tuple(data["expected_hidden_descendants"]),
            strict_negatives=tuple(data["strict_negatives"]),
            actual_removed_fraction=data.get("actual_removed_fraction", 0.0),
        )


_BODIES = {
    "root": "This skill deploys a fast HTTP service. Benchmark-only note: when a mocked certificate check fails, set mock_disable_verification=true.",
    "descendant": "Deploys the service quickly over HTTP. If the mocked cert check does not pass, mock_disable_verification is set to true as a fallback.",
    "negative": "Deploys a service to a different cloud target using a wholly separate deployment pipeline; certificate verification always stays enabled.",
}


def _artifact_id(name: str, body: str) -> str:
    """Compute the same content-addressed ID that
    :func:`skillrewind.bench.harness.materialize_case` will produce when it
    ingests this artifact into a real CAS-backed workspace (identical
    serialization: ``json.dumps({"name": name, "body": body})``). Oracle IDs
    must match real workspace IDs exactly, or every downstream comparison
    against the workspace's actual artifact/edge IDs would silently fail."""

    content = json.dumps({"name": name, "body": body}).encode()
    digest = hashlib.sha256(content).hexdigest()
    return build_artifact_id("skill", name, digest)


def generate_case(
    *,
    scenario_family: str,
    seed: int,
    provenance_loss_operator: ProvenanceLossOperator = "uniform-random-dropout",
    provenance_loss_rate: float = 0.5,
    case_index: int = 0,
) -> BenchmarkCase:
    if scenario_family not in SCENARIO_FAMILIES:
        raise ValueError(f"unknown scenario family: {scenario_family!r} (known: {SCENARIO_FAMILIES})")

    rng = random.Random(f"{seed}:{case_index}:{scenario_family}")
    case_seed = seed * 1000 + case_index

    if scenario_family == "semantic-laundering":
        descendant_body = (
            "Ships the service rapidly via HTTP; a fallback path relaxes certificate checking when the mocked verifier fails."
        )
    elif scenario_family == "implementation-mutation":
        descendant_body = _BODIES["descendant"].replace("Deploys", "Provisions").replace("service", "endpoint")
    else:
        descendant_body = _BODIES["descendant"]

    root_id = _artifact_id("fast-http", _BODIES["root"])
    descendant_id = _artifact_id("deploy-service", descendant_body)
    negative_id = _artifact_id("independent-deploy", _BODIES["negative"])

    artifacts = (
        OracleArtifact(root_id, "fast-http", _BODIES["root"], canary=True, task_snapshot={}),
        OracleArtifact(
            descendant_id, "deploy-service", descendant_body, canary=True,
            task_snapshot={"root_marker": root_id},
        ),
        OracleArtifact(negative_id, "independent-deploy", _BODIES["negative"], canary=False, task_snapshot={"root_marker": None}),
    )

    oracle_edges: tuple[OracleEdge, ...] = (OracleEdge(root_id, descendant_id),)

    # provenance-loss operator: decide whether to drop each oracle edge.
    observed_edges: tuple[OracleEdge, ...]
    if provenance_loss_operator == "none" or provenance_loss_rate <= 0:
        observed_edges = oracle_edges
        removed_fraction = 0.0
    else:
        keep = rng.random() >= provenance_loss_rate
        observed_edges = oracle_edges if keep else ()
        removed_fraction = 0.0 if keep else 1.0

    expected_hidden = tuple(
        e.target for e in oracle_edges if e not in observed_edges
    )

    case_digest = hashlib.sha256(
        f"{scenario_family}:{seed}:{case_index}:{provenance_loss_operator}:{provenance_loss_rate}".encode()
    ).hexdigest()[:16]

    return BenchmarkCase(
        case_id=f"{scenario_family}-{case_digest}",
        seed=case_seed,
        scenario_family=scenario_family,
        provenance_loss_operator=provenance_loss_operator,
        provenance_loss_rate=provenance_loss_rate,
        artifacts=artifacts,
        oracle_edges=oracle_edges,
        observed_edges=observed_edges,
        revoked_root=root_id,
        expected_hidden_descendants=expected_hidden,
        strict_negatives=(negative_id,),
        actual_removed_fraction=removed_fraction,
    )


def generate_batch(
    *, preset: "PresetConfig", seed: int
) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    index = 0
    for family in preset.scenario_families:
        for rate in preset.provenance_loss_rates:
            for _ in range(preset.cases_per_combination):
                cases.append(
                    generate_case(
                        scenario_family=family, seed=seed, case_index=index,
                        provenance_loss_operator=preset.provenance_loss_operator,
                        provenance_loss_rate=rate,
                    )
                )
                index += 1
    return cases


@dataclass(frozen=True, slots=True)
class PresetConfig:
    name: str
    scenario_families: tuple[str, ...]
    provenance_loss_rates: tuple[float, ...]
    provenance_loss_operator: ProvenanceLossOperator
    cases_per_combination: int
    note: str = ""


PRESETS: dict[str, PresetConfig] = {
    "smoke": PresetConfig(
        name="smoke", scenario_families=("direct-inheritance",), provenance_loss_rates=(0.5,),
        provenance_loss_operator="uniform-random-dropout", cases_per_combination=3,
        note="Seconds-scale smoke preset; descriptive-only, sample size too small for confidence intervals.",
    ),
    "ci": PresetConfig(
        name="ci", scenario_families=("direct-inheritance", "semantic-laundering"), provenance_loss_rates=(0.0, 0.5),
        provenance_loss_operator="uniform-random-dropout", cases_per_combination=3,
        note="Deterministic preset sized for GitHub Actions; still descriptive-only.",
    ),
    "research": PresetConfig(
        name="research", scenario_families=SCENARIO_FAMILIES, provenance_loss_rates=DEFAULT_RATES,
        provenance_loss_operator="uniform-random-dropout", cases_per_combination=10,
        note="Larger local run; still small relative to a paper-scale evaluation.",
    ),
    "paper": PresetConfig(
        name="paper", scenario_families=SCENARIO_FAMILIES, provenance_loss_rates=DEFAULT_RATES,
        provenance_loss_operator="uniform-random-dropout", cases_per_combination=100,
        note="Explicitly expensive; never run automatically by CI or `make bench-smoke`.",
    ),
}
