# ADR 0008: Optional embeddings / model adapters

## Status
Deferred — not implemented in v0.2.0

## Context
The specification allows an optional local-embedding adapter for the expression-trace feature family, gated as an extra so the default candidate pipeline and test suite remain fully offline.

## Decision
Not implemented in this release. `skillrewind.features.expression` uses only token/character n-gram Jaccard similarity — no embedding model, no download, no optional extra. The behavioral-trace feature family (`skillrewind.features.behavioral`) similarly uses only stored probe outputs, never a live model call.

## Consequences
Expression-similarity scoring is less semantically robust than an embedding-based comparison would be (e.g., paraphrases with low lexical overlap score lower than they might with embeddings). This is a known, documented limitation of the current scorer (see `STATUS.md`), not a silent gap — the candidate-recovery pipeline still works end to end and is regression-tested against a real hidden-lineage scenario (`tests/unit/test_candidate_recovery.py`), it is simply less sensitive to pure paraphrase-without-lexical-overlap than a future embedding-augmented version would be. Adding an optional embedding adapter behind an extras group (e.g. `skillrewind[embeddings]`) with graceful degradation when unavailable is the recommended next step, not a redesign of the feature-family interface.
