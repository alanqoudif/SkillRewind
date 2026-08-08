from __future__ import annotations

from skillrewind.domain.enums import EvidenceClass, RelationType
from skillrewind.domain.models import InfluenceEdge
from skillrewind.lineage.graph import LineageGraph


def _edge(source: str, target: str, relation=RelationType.USED_AS_INPUT) -> InfluenceEdge:
    return InfluenceEdge(source=source, target=target, relation=relation, evidence_class=EvidenceClass.RECORDED)


def test_descendants_and_ancestors_are_transitive_and_cycle_safe():
    edges = [_edge("a", "b"), _edge("b", "c"), _edge("c", "a"), _edge("c", "d")]
    graph = LineageGraph(edges)
    assert graph.descendants(["a"]) == ("a", "b", "c", "d")
    assert graph.ancestors(["d"]) == ("a", "b", "c", "d")
    assert graph.has_cycle()


def test_descendants_exclude_roots_when_requested():
    graph = LineageGraph([_edge("a", "b")])
    assert graph.descendants(["a"], include_roots=False) == ("b",)


def test_induced_edges_and_shortest_path():
    edges = [_edge("a", "b"), _edge("b", "c"), _edge("a", "c")]
    graph = LineageGraph(edges)
    induced = graph.induced_edges(["a", "b"])
    assert {(e.source, e.target) for e in induced} == {("a", "b")}
    path = graph.shortest_path("a", "c")
    assert path is not None
    assert path.nodes == ("a", "c")


def test_filtered_by_relation_and_evidence_class():
    edges = [
        _edge("a", "b", RelationType.USED_AS_INPUT),
        _edge("a", "c", RelationType.CONTEXT_EXPOSURE),
    ]
    graph = LineageGraph(edges)
    only_input = graph.filtered(relations=["used-as-input"])
    assert only_input.nodes == frozenset({"a", "b"})


def test_no_cycle_for_dag():
    graph = LineageGraph([_edge("a", "b"), _edge("b", "c")])
    assert not graph.has_cycle()


def test_to_mermaid_contains_all_nodes():
    graph = LineageGraph([_edge("a", "b")])
    mermaid = graph.to_mermaid()
    assert "flowchart LR" in mermaid
    assert "-->" in mermaid
