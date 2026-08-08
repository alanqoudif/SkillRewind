# ADR 0001: Evidence classes and non-causal static scoring

## Status
Accepted (v0.2.0)

## Context
SkillRewind must never let a similarity score masquerade as proof of causal influence, and must never let a static candidate silently become "confirmed" without an actual intervention.

## Decision
Every influence edge carries exactly one of five evidence classes: `recorded`, `inferred`, `replay-confirmed`, `rejected`, `unresolved`. Only a real paired counterfactual replay (`skillrewind.replay.service.run_paired_replay`) may set `replay-confirmed` or `rejected`. The static scorer (`skillrewind.inference.scoring`) may only ever produce `inferred`. A runner failure produces `unresolved`, never `rejected` — an error is not evidence of absence. Promotion from `inferred` to `replay-confirmed`/`rejected` appends to the edge's evidence list rather than overwriting it, so the static evidence that originally raised the candidate remains inspectable.

Recorded closure (`skillrewind.lineage.closure.build_graph`) strictly filters to `evidence_class == "recorded"` — this was tightened during implementation after a real bug where inferred edges were silently included, letting an unconfirmed candidate collapse into what a `balanced`/`strict` barrier treated as ground truth.

## Consequences
- Every consumer of an edge must check `evidence_class` before treating it as ground truth.
- The scorer's default weights (`FeatureWeights`) are explicitly documented as provisional/hand-tuned, not learned; an offline calibration pipeline is a documented but unimplemented future extension.
