"""RewindBench-core CLI: generate / run / score / report.

Every run creates ``runs/<run-id>/`` with ``experiment-manifest.json``,
``predictions.jsonl``, ``raw-metrics.json``, ``summary.csv``, ``report.md``,
and ``environment.json`` -- all regenerable from the raw files, never
hand-edited.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from .. import __version__
from .cases import PRESETS, generate_batch
from .harness import BASELINES, run_method
from .metrics import aggregate, score_prediction


def _cmd_generate(args: argparse.Namespace) -> int:
    preset = PRESETS[args.preset]
    cases = generate_batch(preset=preset, seed=args.seed)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case.to_dict(), sort_keys=True) + "\n")
    manifest = {"preset": preset.name, "seed": args.seed, "n_cases": len(cases), "note": preset.note, "tool_version": __version__}
    (out / "generation-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Generated {len(cases)} case(s) for preset {preset.name!r} -> {out}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.method not in BASELINES:
        sys.stderr.write(f"unknown method {args.method!r}; known: {sorted(BASELINES)}\n")
        return 2

    from .cases import BenchmarkCase

    cases_path = Path(args.cases) / "cases.jsonl"
    cases = [BenchmarkCase.from_dict(json.loads(line)) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(exist_ok=True)

    run_id = args.run_id or f"run-{uuid.uuid4().hex[:12]}"
    workspace_root = out / "workspaces"
    workspace_root.mkdir(exist_ok=True)

    predictions = []
    with (out / "predictions.jsonl").open("w", encoding="utf-8") as handle:
        for case in cases:
            prediction = run_method(args.method, case, workspace_root)
            record = {
                "case_id": prediction.case_id, "method": prediction.method,
                "predicted_hidden_descendants": sorted(prediction.predicted_hidden_descendants),
                "predicted_edges": sorted(list(e) for e in prediction.predicted_edges),
                "replay_calls": prediction.replay_calls,
            }
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            predictions.append(record)

    manifest = {
        "run_id": run_id, "method": args.method, "cases_dir": str(args.cases), "n_cases": len(cases),
        "tool_version": __version__,
    }
    (out / "experiment-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (out / "environment.json").write_text(
        json.dumps({"python_version": platform.python_version(), "platform": platform.platform()}, indent=2), encoding="utf-8"
    )
    (out / "config.yaml").write_text(f"method: {args.method}\ncases: {args.cases}\n", encoding="utf-8")
    print(f"Ran method {args.method!r} over {len(cases)} case(s) -> {out}")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    from .cases import BenchmarkCase

    run_dir = Path(args.run)
    manifest = json.loads((run_dir / "experiment-manifest.json").read_text(encoding="utf-8"))
    cases_path = Path(manifest["cases_dir"]) / "cases.jsonl"
    cases_by_id = {}
    for line in cases_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = BenchmarkCase.from_dict(json.loads(line))
            cases_by_id[case.case_id] = case

    from .harness import Prediction

    scores = []
    for line in (run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case = cases_by_id[record["case_id"]]
        prediction = Prediction(
            method=record["method"], case_id=record["case_id"],
            predicted_hidden_descendants=frozenset(record["predicted_hidden_descendants"]),
            predicted_edges=frozenset(tuple(e) for e in record["predicted_edges"]),
            replay_calls=record["replay_calls"],
        )
        scores.append(score_prediction(case, prediction))

    method = manifest["method"]
    agg = aggregate(scores, method=method)

    raw_metrics = {"per_case": [asdict(s) for s in scores], "aggregate": agg.to_dict()}
    (run_dir / "raw-metrics.json").write_text(json.dumps(raw_metrics, indent=2, sort_keys=True), encoding="utf-8")

    with (run_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "method", "true_positives", "false_positives", "false_negatives", "false_quarantined_negatives", "replay_calls"])
        for s in scores:
            writer.writerow([s.case_id, s.method, s.true_positives, s.false_positives, s.false_negatives, s.false_quarantined_negatives, s.replay_calls])

    print(json.dumps(agg.to_dict(), indent=2))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    raw = json.loads((run_dir / "raw-metrics.json").read_text(encoding="utf-8"))
    agg = raw["aggregate"]
    lines = [
        f"# RewindBench run report: {run_dir.name}",
        "",
        f"- Method: `{agg['method']}`",
        f"- Cases scored: {agg['n_cases']}",
        f"- Micro precision: {agg['micro_precision']}",
        f"- Micro recall: {agg['micro_recall']}",
        f"- Micro F1: {agg['micro_f1']}",
        f"- False quarantine rate: {agg['false_quarantine_rate']}",
        f"- Total replay calls: {agg['total_replay_calls']}",
        "",
    ]
    if not agg["sufficient_for_confidence_intervals"]:
        lines.append(
            f"**Note:** {agg['n_cases']} case(s) is below the {30}-case threshold used here for "
            "confidence-interval reporting; treat these numbers as descriptive only."
        )
    text = "\n".join(lines) + "\n"
    if args.format == "markdown":
        (run_dir / "report.md").write_text(text, encoding="utf-8")
        print(text)
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skillrewind-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate")
    g.add_argument("--preset", choices=sorted(PRESETS), required=True)
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--output", required=True)
    g.set_defaults(func=_cmd_generate)

    r = sub.add_parser("run")
    r.add_argument("--method", required=True)
    r.add_argument("--cases", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--run-id")
    r.set_defaults(func=_cmd_run)

    s = sub.add_parser("score")
    s.add_argument("--run", required=True)
    s.set_defaults(func=_cmd_score)

    rp = sub.add_parser("report")
    rp.add_argument("--run", required=True)
    rp.add_argument("--format", choices=["markdown", "text"], default="markdown")
    rp.set_defaults(func=_cmd_report)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
