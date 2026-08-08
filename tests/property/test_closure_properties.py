"""Property tests for canonicalization, IDs, and lineage closure invariants."""

from __future__ import annotations

import hashlib

from hypothesis import given, settings
from hypothesis import strategies as st

from skillrewind.canonical.json import canonical_bytes
from skillrewind.domain.enums import EvidenceClass, RelationType
from skillrewind.domain.ids import build_artifact_id, parse_artifact_id
from skillrewind.domain.models import InfluenceEdge
from skillrewind.lineage.closure import build_graph, recorded_descendants
from skillrewind.lineage.graph import LineageGraph
from skillrewind.workspace import Workspace

_names = st.text(alphabet="abcdefgh", min_size=1, max_size=3)
_json_values = st.recursive(
    st.one_of(st.integers(min_value=-1000, max_value=1000), st.text(max_size=10), st.booleans(), st.none()),
    lambda children: st.dictionaries(st.text(alphabet="abcde", min_size=1, max_size=4), children, max_size=4),
    max_leaves=8,
)


@given(_json_values)
@settings(max_examples=100)
def test_canonicalization_is_deterministic(value):
    assert canonical_bytes(value) == canonical_bytes(value)


@given(st.binary(max_size=200))
@settings(max_examples=50)
def test_identical_content_produces_identical_digest(data):
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(data).hexdigest()


@given(_names, _names, st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
@settings(max_examples=50)
def test_artifact_id_roundtrip(scheme_suffix, name, digest):
    scheme = "s" + scheme_suffix
    artifact_id = build_artifact_id(scheme, name, digest)
    parsed = parse_artifact_id(artifact_id)
    assert parsed == (scheme, name, digest.lower())


_edge_lists = st.lists(
    st.tuples(_names, _names).map(lambda t: (f"n-{t[0]}", f"n-{t[1]}")),
    max_size=15,
)


def _edges_from_pairs(pairs) -> list[InfluenceEdge]:
    return [
        InfluenceEdge(source=s, target=t, relation=RelationType.USED_AS_INPUT, evidence_class=EvidenceClass.RECORDED)
        for s, t in pairs
    ]


@given(_edge_lists)
@settings(max_examples=60)
def test_closure_monotonic_when_edges_added(pairs):
    if not pairs:
        return
    base_edges = _edges_from_pairs(pairs[:-1])
    root = pairs[0][0]
    base_graph = LineageGraph(base_edges)
    extended_graph = LineageGraph(_edges_from_pairs(pairs))
    base_closure = set(base_graph.descendants([root]))
    extended_closure = set(extended_graph.descendants([root]))
    assert base_closure.issubset(extended_closure)


@given(_edge_lists)
@settings(max_examples=40)
def test_db_backed_closure_matches_in_memory_closure(tmp_path_factory, pairs):
    if not pairs:
        return
    edges = _edges_from_pairs(pairs)
    root = pairs[0][0]
    in_memory = LineageGraph(edges).descendants([root])

    ws_dir = tmp_path_factory.mktemp("ws")
    ws = Workspace.init(ws_dir)
    for edge in edges:
        ws.edges.upsert(edge)
    db_backed = recorded_descendants(ws, [root])
    ws.close()

    assert in_memory == db_backed
