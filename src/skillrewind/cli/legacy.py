"""Command-line interface for the SkillRewind research starter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from ..attestation import recorded_attestation
from ..graph import LineageFormatError, RecordedLineageGraph, load_edges


def _write_json(value: object, output: str | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output is None or output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillrewind",
        description=(
            "SkillRewind research starter. Version 0.1 implements recorded-lineage "
            "closure only; it does not simulate hidden-lineage recovery or causal replay."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    closure = subparsers.add_parser(
        "closure", help="Compute the deterministic transitive closure of recorded edges."
    )
    closure.add_argument("--edges", required=True, help="JSONL or JSON edge file.")
    closure.add_argument(
        "--root", action="append", required=True, help="Revoked root; repeat for multiple roots."
    )
    closure.add_argument(
        "--exclude-roots", action="store_true", help="Return descendants without the roots."
    )
    closure.add_argument("--output", help="Output JSON path; defaults to stdout.")

    attest = subparsers.add_parser(
        "attest", help="Emit a recorded-evidence attestation with explicit capability limits."
    )
    attest.add_argument("--edges", required=True, help="JSONL or JSON edge file.")
    attest.add_argument(
        "--root", action="append", required=True, help="Revoked root; repeat for multiple roots."
    )
    attest.add_argument("--reason", default="Recorded-lineage forensic analysis")
    attest.add_argument("--output", required=True, help="Output attestation JSON path.")

    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        graph = RecordedLineageGraph(load_edges(args.edges))
        if args.command == "closure":
            closure = graph.descendants(args.root, include_roots=not args.exclude_roots)
            induced = graph.induced_edges(closure)
            _write_json(
                {
                    "mode": "recorded-only",
                    "roots": sorted(set(args.root)),
                    "artifacts": list(closure),
                    "edges": [edge.as_dict() for edge in induced],
                    "limitations": [
                        "This result traverses only supplied recorded edges.",
                        "It does not infer hidden influence or test causal effects."
                    ]
                },
                args.output,
            )
            return 0
        if args.command == "attest":
            _write_json(recorded_attestation(graph, args.root, reason=args.reason), args.output)
            return 0
        parser.error(f"unsupported command: {args.command}")
    except (FileNotFoundError, LineageFormatError, ValueError) as exc:
        parser.exit(2, f"skillrewind: error: {exc}\n")
    return 2


def main() -> None:
    raise SystemExit(run())
