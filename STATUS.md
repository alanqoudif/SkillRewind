# Research status - version 0.1

## Completed

- Publication-oriented research proposal with a formal problem statement, related-work positioning, research questions, proposed method, benchmark design, experimental protocol, limitations, ethics, and a 20-week plan.
- RFC 0001 defining artifact identity, evidence classes, revocation semantics, clean-room rebuilding, and bounded attestations.
- JSON Schemas for artifact manifests, influence edges, revocation attestations, and RewindBench cases.
- Valid example artifacts for the schemas.
- RewindBench v0.1 design and a semantic-laundering example case.
- A dependency-free Python baseline for deterministic recorded-lineage closure.
- A CLI for closure reports and recorded-only attestations.
- Six passing unit tests.

## Not completed and not claimed

- Hidden-lineage candidate model.
- Counterfactual re-derivation harness.
- Active replay selector.
- Quarantine or serving integration.
- Clean-room rebuilding.
- Public-system adapters.
- Experimental results or a peer-reviewed publication.

## Immediate next milestone

Implement **RewindBench-Core** before optimizing the proposed method. The first frozen experiment should compare:

1. DeleteRoot;
2. RecordedClosure;
3. text embeddings;
4. static multi-trace recovery; and
5. exhaustive replay on a small oracle graph.

The first go/no-go question is whether semantic, implementation, operational, and behavioral traces can recover planted missing edges without classifying independently developed same-function skills as descendants.
