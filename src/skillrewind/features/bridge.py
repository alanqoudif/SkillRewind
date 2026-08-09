"""Service-mode feature-extraction bridge (Phase C2.1 section 4).

Connects the existing feature-family extractors (expression, implementation,
operational, behavioral, graph, temporal -- unchanged, imported directly, not
reimplemented) to Service-mode artifacts. All artifact/content access goes
through `ArtifactRepository` + CAS and `DerivationRepository`; extracted
values are persisted through `FeatureRepository` with extractor version and a
configuration digest so a given `(pair, extractor, config)` is never
recomputed. Deterministic for deterministic fixtures, calls no network/paid
API, and never mislabels lexical similarity as embedding similarity or
static similarity as causal evidence -- see the individual feature modules'
own docstrings for what each score does and does not mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..canonical.json import sha256_hex
from ..config import SkillRewindConfig
from ..domain.errors import NotFoundError
from ..inference.scoring import FeatureBreakdown
from ..lineage.graph import LineageGraph
from ..persistence.service.models import Artifact
from ..persistence.service.repositories import ArtifactRepository, DerivationRepository, FeatureRepository
from .behavioral import behavioral_similarity
from .expression import expression_similarity
from .graph import graph_similarity
from .implementation import implementation_similarity
from .operational import operational_similarity
from .temporal import temporal_proximity

EXTRACTOR_VERSION = "service-bridge-0.1.0"


def config_digest(config: SkillRewindConfig) -> str:
    return sha256_hex(
        {
            "weights": {
                "expression": config.feature_weights.expression,
                "implementation": config.feature_weights.implementation,
                "operational": config.feature_weights.operational,
                "behavioral": config.feature_weights.behavioral,
                "graph": config.feature_weights.graph,
                "temporal": config.feature_weights.temporal,
            },
            "thresholds": {
                "candidate": config.thresholds.candidate,
                "high_confidence": config.thresholds.high_confidence,
            },
            "extractor_version": EXTRACTOR_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class _ArtifactText:
    expression_text: str
    python_source: str


def _artifact_text(cas, artifact: Artifact) -> _ArtifactText:
    """Best-effort `(expression_text, python_source_text)` for a Service-mode
    artifact, mirroring `skillrewind.lineage.candidates._artifact_text`'s Lite
    -mode logic but sourced from `metadata_json` + CAS instead of a
    `Workspace`."""

    manifest = (artifact.metadata_json or {}).get("agent_skills_manifest")
    if manifest:
        expr_text = f"{manifest.get('description', '')}\n{manifest.get('body', '')}"
        python_source = ""
        for entry in manifest.get("files", []):
            if entry.get("path", "").endswith(".py"):
                try:
                    python_source += cas.get_bytes(entry["sha256"]).decode("utf-8", errors="replace")
                except Exception:
                    continue
        return _ArtifactText(expr_text, python_source)

    try:
        raw = cas.get_bytes(artifact.digest_hex).decode("utf-8", errors="replace")
    except Exception:
        return _ArtifactText("", "")
    if artifact.mime_type in ("text/x-python", "application/x-python"):
        return _ArtifactText(raw, raw)
    return _ArtifactText(raw, "")


def extract_pair_breakdown(
    session,
    cas,
    config: SkillRewindConfig,
    *,
    source_artifact_id: str,
    target_artifact_id: str,
    recorded_graph: Optional[LineageGraph] = None,
    persist: bool = True,
) -> FeatureBreakdown:
    """Compute (and, if `persist`, record via `FeatureRepository`) the
    per-family similarity breakdown between two Service-mode artifacts, using
    the same feature functions Lite mode uses -- no algorithm is duplicated
    here, only the data-access plumbing differs."""

    artifacts = ArtifactRepository(session, cas)
    derivations = DerivationRepository(session, artifacts)
    features = FeatureRepository(session)
    digest = config_digest(config)

    source = artifacts.get(source_artifact_id)
    target = artifacts.get(target_artifact_id)
    source_text = _artifact_text(cas, source)
    target_text = _artifact_text(cas, target)

    expression = None
    if source_text.expression_text and target_text.expression_text:
        expression = expression_similarity(source_text.expression_text, target_text.expression_text).combined

    implementation = None
    if source_text.python_source and target_text.python_source:
        implementation = implementation_similarity(source_text.python_source, target_text.python_source).combined

    def _tool_calls(artifact_id: str) -> Optional[list]:
        try:
            deriv = derivations.get_derivation_for_output(artifact_id)
        except NotFoundError:
            return None
        return (deriv.payload_json or {}).get("tool_calls") or []

    source_tools = _tool_calls(source_artifact_id)
    target_tools = _tool_calls(target_artifact_id)
    operational = None
    if source_tools is not None and target_tools is not None:
        operational = operational_similarity(source_tools, target_tools).combined

    behavioral_score = behavioral_similarity(source.metadata_json or {}, target.metadata_json or {})
    behavioral = behavioral_score.combined if behavioral_score.source != "unavailable" else None

    graph = recorded_graph
    graph_value = None
    if graph is not None:
        graph_value = graph_similarity(graph, source_artifact_id, target_artifact_id).combined

    def _started_at(artifact_id: str) -> Optional[str]:
        try:
            deriv = derivations.get_derivation_for_output(artifact_id)
        except NotFoundError:
            return None
        return deriv.started_at.isoformat() if deriv.started_at else None

    temporal = temporal_proximity(_started_at(source_artifact_id), _started_at(target_artifact_id))

    breakdown = FeatureBreakdown(
        expression=expression,
        implementation=implementation,
        operational=operational,
        behavioral=behavioral,
        graph=graph_value,
        temporal=temporal,
    )

    if persist:
        for family, value in breakdown.as_dict().items():
            features.record(
                source_artifact_id=source_artifact_id,
                target_artifact_id=target_artifact_id,
                feature_family=family,
                value=value,
                extractor_version=EXTRACTOR_VERSION,
                config_digest=digest,
            )

    return breakdown


_STRICT_NEGATIVE_EXPRESSION_CEILING = 0.5


def is_strict_negative(behavioral_score, expression_value: Optional[float]) -> bool:
    """Same strict-negative heuristic as Lite mode's `lineage.candidates`:
    behavioral probes actively disagree while lexical similarity stays low.
    Never proves independence on its own -- only replay can -- it only
    suppresses a false high-confidence label pending replay."""

    return (
        behavioral_score.source == "stored-probes"
        and behavioral_score.canary_agreement == 0.0
        and (expression_value or 0.0) < _STRICT_NEGATIVE_EXPRESSION_CEILING
    )
