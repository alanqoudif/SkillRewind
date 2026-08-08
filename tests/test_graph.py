from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from skillrewind.attestation import recorded_attestation
from skillrewind.graph import Edge, LineageFormatError, RecordedLineageGraph, load_edges


class RecordedLineageGraphTests(unittest.TestCase):
    def test_multihop_closure_is_sorted_and_includes_root(self) -> None:
        graph = RecordedLineageGraph(
            [
                Edge("root", "b"),
                Edge("b", "c"),
                Edge("root", "a"),
                Edge("unrelated", "z"),
            ]
        )
        self.assertEqual(graph.descendants(["root"]), ("a", "b", "c", "root"))

    def test_cycle_is_safe(self) -> None:
        graph = RecordedLineageGraph([Edge("a", "b"), Edge("b", "a")])
        self.assertEqual(graph.descendants(["a"]), ("a", "b"))

    def test_multiple_roots_and_exclusion(self) -> None:
        graph = RecordedLineageGraph([Edge("a", "b"), Edge("x", "y")])
        self.assertEqual(graph.descendants(["x", "a"], include_roots=False), ("b", "y"))

    def test_load_jsonl_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edges.jsonl"
            record = {"source": "a", "target": "b", "relation": "used-as-input"}
            path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(load_edges(path), (Edge("a", "b", "used-as-input"),))

    def test_invalid_edge_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edges.jsonl"
            path.write_text('{"source":"a"}\n', encoding="utf-8")
            with self.assertRaises(LineageFormatError):
                load_edges(path)

    def test_attestation_does_not_claim_verification(self) -> None:
        graph = RecordedLineageGraph([Edge("root", "child")])
        result = recorded_attestation(graph, ["root"])
        self.assertEqual(result["mode"], "recorded-only")
        self.assertEqual(result["verification"]["status"], "not-run")
        self.assertIsNone(result["verification"]["target_behavior_observed"])
        self.assertEqual(result["recorded_closure"], ["child", "root"])


if __name__ == "__main__":
    unittest.main()
