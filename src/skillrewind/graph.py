"""Deterministic recorded-lineage graph primitives."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


class LineageFormatError(ValueError):
    """Raised when an edge file is malformed."""


@dataclass(frozen=True, slots=True, order=True)
class Edge:
    """A directed recorded lineage edge."""

    source: str
    target: str
    relation: str = "recorded-influence"

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, location: str) -> "Edge":
        source = value.get("source")
        target = value.get("target")
        relation = value.get("relation", "recorded-influence")
        if not isinstance(source, str) or not source.strip():
            raise LineageFormatError(f"{location}: 'source' must be a non-empty string")
        if not isinstance(target, str) or not target.strip():
            raise LineageFormatError(f"{location}: 'target' must be a non-empty string")
        if not isinstance(relation, str) or not relation.strip():
            raise LineageFormatError(f"{location}: 'relation' must be a non-empty string")
        return cls(source=source.strip(), target=target.strip(), relation=relation.strip())

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "relation": self.relation}


def _iter_jsonl(path: Path) -> Iterator[Mapping[str, object]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LineageFormatError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise LineageFormatError(
                    f"{path}:{line_number}: each JSONL record must be an object"
                )
            yield value


def load_edges(path: str | Path) -> tuple[Edge, ...]:
    """Load edges from JSONL or from a JSON array.

    Duplicate edges are removed and the result is sorted for reproducibility.
    """

    edge_path = Path(path)
    if not edge_path.is_file():
        raise FileNotFoundError(f"edge file not found: {edge_path}")

    if edge_path.suffix.lower() == ".jsonl":
        records = _iter_jsonl(edge_path)
    else:
        try:
            value = json.loads(edge_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LineageFormatError(f"{edge_path}: invalid JSON: {exc.msg}") from exc
        if not isinstance(value, list):
            raise LineageFormatError(f"{edge_path}: top-level JSON value must be an array")
        if not all(isinstance(item, dict) for item in value):
            raise LineageFormatError(f"{edge_path}: every edge must be a JSON object")
        records = iter(value)

    edges = {
        Edge.from_mapping(record, location=f"{edge_path}:record-{index}")
        for index, record in enumerate(records, start=1)
    }
    return tuple(sorted(edges))


class RecordedLineageGraph:
    """An immutable directed graph over recorded provenance edges."""

    def __init__(self, edges: Iterable[Edge]) -> None:
        unique = tuple(sorted(set(edges)))
        adjacency: dict[str, set[str]] = defaultdict(set)
        nodes: set[str] = set()
        for edge in unique:
            adjacency[edge.source].add(edge.target)
            nodes.add(edge.source)
            nodes.add(edge.target)
        self._edges = unique
        self._adjacency = {source: tuple(sorted(targets)) for source, targets in adjacency.items()}
        self._nodes = frozenset(nodes)

    @property
    def edges(self) -> tuple[Edge, ...]:
        return self._edges

    @property
    def nodes(self) -> frozenset[str]:
        return self._nodes

    def descendants(self, roots: Sequence[str], *, include_roots: bool = True) -> tuple[str, ...]:
        """Return the transitive recorded descendant closure.

        Traversal is cycle-safe and output is sorted for stable artifacts.
        Roots need not already appear in the graph; they are included when
        ``include_roots`` is true.
        """

        normalized = tuple(sorted({root.strip() for root in roots if root.strip()}))
        if not normalized:
            raise ValueError("at least one non-empty root is required")

        visited: set[str] = set(normalized)
        queue: deque[str] = deque(normalized)
        while queue:
            source = queue.popleft()
            for target in self._adjacency.get(source, ()):
                if target not in visited:
                    visited.add(target)
                    queue.append(target)

        if not include_roots:
            visited.difference_update(normalized)
        return tuple(sorted(visited))

    def induced_edges(self, artifacts: Iterable[str]) -> tuple[Edge, ...]:
        selected = frozenset(artifacts)
        return tuple(
            edge for edge in self._edges if edge.source in selected and edge.target in selected
        )
