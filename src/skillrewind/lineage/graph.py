"""Deterministic lineage graph over persisted influence edges.

This generalizes the v0.1 :mod:`skillrewind.graph` baseline (still preserved
unmodified for backward compatibility) to operate over
:class:`~skillrewind.domain.models.InfluenceEdge` records with relation and
evidence-class filtering, and to be constructible either from a workspace's
edge repository or from an in-memory list (used by property tests that
compare the two).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..domain.models import InfluenceEdge


@dataclass(frozen=True, slots=True)
class PathResult:
    nodes: tuple[str, ...]
    edges: tuple[InfluenceEdge, ...]


class LineageGraph:
    """An immutable directed graph over a fixed snapshot of influence edges."""

    def __init__(self, edges: Iterable[InfluenceEdge]) -> None:
        unique: dict[tuple[str, str, str], InfluenceEdge] = {}
        for edge in edges:
            unique[(edge.source, edge.target, edge.relation.value)] = edge
        self._edges: tuple[InfluenceEdge, ...] = tuple(
            sorted(unique.values(), key=lambda e: (e.source, e.target, e.relation.value))
        )
        adjacency: dict[str, set[str]] = defaultdict(set)
        reverse: dict[str, set[str]] = defaultdict(set)
        nodes: set[str] = set()
        for edge in self._edges:
            adjacency[edge.source].add(edge.target)
            reverse[edge.target].add(edge.source)
            nodes.add(edge.source)
            nodes.add(edge.target)
        self._adjacency = {k: tuple(sorted(v)) for k, v in adjacency.items()}
        self._reverse = {k: tuple(sorted(v)) for k, v in reverse.items()}
        self._nodes = frozenset(nodes)

    @property
    def edges(self) -> tuple[InfluenceEdge, ...]:
        return self._edges

    @property
    def nodes(self) -> frozenset[str]:
        return self._nodes

    def _closure(
        self, roots: Sequence[str], adjacency: dict[str, tuple[str, ...]], *, include_roots: bool
    ) -> tuple[str, ...]:
        normalized = tuple(sorted({r.strip() for r in roots if r.strip()}))
        if not normalized:
            raise ValueError("at least one non-empty root is required")
        visited: set[str] = set(normalized)
        queue: deque[str] = deque(normalized)
        while queue:
            node = queue.popleft()
            for neighbor in adjacency.get(node, ()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if not include_roots:
            visited.difference_update(normalized)
        return tuple(sorted(visited))

    def descendants(self, roots: Sequence[str], *, include_roots: bool = True) -> tuple[str, ...]:
        """Transitive closure following edges forward (source -> target); cycle-safe."""

        return self._closure(roots, self._adjacency, include_roots=include_roots)

    def ancestors(self, roots: Sequence[str], *, include_roots: bool = True) -> tuple[str, ...]:
        """Transitive closure following edges backward (target -> source); cycle-safe."""

        return self._closure(roots, self._reverse, include_roots=include_roots)

    def induced_edges(self, artifacts: Iterable[str]) -> tuple[InfluenceEdge, ...]:
        selected = frozenset(artifacts)
        return tuple(e for e in self._edges if e.source in selected and e.target in selected)

    def has_cycle(self) -> bool:
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in self._nodes}

        def visit(node: str) -> bool:
            color[node] = GRAY
            for neighbor in self._adjacency.get(node, ()):
                if color[neighbor] == GRAY:
                    return True
                if color[neighbor] == WHITE and visit(neighbor):
                    return True
            color[node] = BLACK
            return False

        return any(color[n] == WHITE and visit(n) for n in sorted(self._nodes))

    def shortest_path(self, source: str, target: str) -> Optional[PathResult]:
        if source == target:
            return PathResult(nodes=(source,), edges=())
        parent: dict[str, str] = {}
        edge_used: dict[tuple[str, str], InfluenceEdge] = {
            (e.source, e.target): e for e in self._edges
        }
        visited = {source}
        queue: deque[str] = deque([source])
        while queue:
            node = queue.popleft()
            for neighbor in self._adjacency.get(node, ()):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                parent[neighbor] = node
                if neighbor == target:
                    path_nodes = [target]
                    cur = target
                    while cur != source:
                        cur = parent[cur]
                        path_nodes.append(cur)
                    path_nodes.reverse()
                    edges = tuple(
                        edge_used[(path_nodes[i], path_nodes[i + 1])]
                        for i in range(len(path_nodes) - 1)
                    )
                    return PathResult(nodes=tuple(path_nodes), edges=edges)
                queue.append(neighbor)
        return None

    def filtered(
        self,
        *,
        relations: Optional[Iterable[str]] = None,
        evidence_classes: Optional[Iterable[str]] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> "LineageGraph":
        rel_set = set(relations) if relations else None
        ev_set = set(evidence_classes) if evidence_classes else None
        st_set = set(statuses) if statuses else None
        kept = [
            e
            for e in self._edges
            if (rel_set is None or e.relation.value in rel_set)
            and (ev_set is None or e.evidence_class.value in ev_set)
            and (st_set is None or e.status in st_set)
        ]
        return LineageGraph(kept)

    def to_mermaid(self) -> str:
        lines = ["flowchart LR"]
        ids = {node: f"n{i}" for i, node in enumerate(sorted(self._nodes))}
        for node, node_id in ids.items():
            label = node.replace('"', "'")
            lines.append(f'    {node_id}["{label}"]')
        for edge in self._edges:
            lines.append(
                f"    {ids[edge.source]} -->|{edge.relation.value} / {edge.evidence_class.value}| {ids[edge.target]}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "nodes": sorted(self._nodes),
            "edges": [e.to_dict() for e in self._edges],
        }
