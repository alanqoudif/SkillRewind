"""Graph/temporal-trace features: shared ancestry, path proximity, recency."""

from __future__ import annotations

from dataclasses import dataclass

from ..lineage.graph import LineageGraph
from .expression import jaccard
from .temporal import temporal_proximity as temporal_proximity  # re-exported for convenience


@dataclass(frozen=True, slots=True)
class GraphScore:
    shared_ancestor_overlap: float
    shared_descendant_overlap: float
    path_proximity: float
    combined: float


def graph_similarity(graph: LineageGraph, artifact_a: str, artifact_b: str) -> GraphScore:
    ancestors_a = set(graph.ancestors([artifact_a], include_roots=False)) if artifact_a in graph.nodes else set()
    ancestors_b = set(graph.ancestors([artifact_b], include_roots=False)) if artifact_b in graph.nodes else set()
    descendants_a = set(graph.descendants([artifact_a], include_roots=False)) if artifact_a in graph.nodes else set()
    descendants_b = set(graph.descendants([artifact_b], include_roots=False)) if artifact_b in graph.nodes else set()

    ancestor_overlap = jaccard(ancestors_a, ancestors_b)
    descendant_overlap = jaccard(descendants_a, descendants_b)

    path = None
    if artifact_a in graph.nodes and artifact_b in graph.nodes:
        path = graph.shortest_path(artifact_a, artifact_b) or graph.shortest_path(artifact_b, artifact_a)
    if path is not None:
        proximity = 1.0 / len(path.edges) if path.edges else 1.0
    else:
        proximity = 0.0

    combined = 0.4 * ancestor_overlap + 0.3 * descendant_overlap + 0.3 * proximity
    return GraphScore(
        shared_ancestor_overlap=ancestor_overlap,
        shared_descendant_overlap=descendant_overlap,
        path_proximity=proximity,
        combined=combined,
    )


