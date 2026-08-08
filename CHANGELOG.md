# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.0] — Research Preview

### Added
- Foundation layer: canonical JSON, full-length content-addressed artifact IDs, local CAS with integrity verification, hash-chained audit log, SQLite persistence (WAL mode).
- Agent Skills directory ingestion/export adapter; capture SDK; generic JSONL trace import.
- Recorded-lineage graph engine with closure/ancestry/cycle-detection/Mermaid export, generalized from the v0.1 baseline to operate over evidence-typed influence edges.
- Hidden-lineage candidate recovery: bounded neighborhoods, six feature families (expression, implementation, operational, behavioral, graph, temporal), deterministic explainable scorer with a strict-negative heuristic.
- Counterfactual replay engine: deterministic-fixture and sandboxed-subprocess runners, paired present/withheld interventions, comparators, multi-component fidelity, paired-bootstrap statistics, classification into confirmed/rejected/unresolved-*, budget-aware active selector plus 3 baseline selectors.
- Revocation state machine (forensic/balanced/strict), barrier application, quarantine service, waivers, serving-resolution layer.
- Clean-room rebuild planner + execution, verification suites.
- Bounded attestation v0.2 with Markdown/HTML rendering and Ed25519 signing/verification.
- RewindBench-core: controlled oracle generation, provenance-loss operators, 4 executable baselines, metrics, smoke/ci/research/paper presets.
- CLI: 30+ commands covering the full workflow; v0.1 `closure`/`attest --edges` commands preserved.
- `poisoned-descendant` primary deterministic demo (`make demo`).

### Fixed
- Recorded closure previously included edges of any evidence class (inferred/replay-confirmed/rejected/unresolved), which could let unconfirmed candidate edges silently expand what a barrier treated as "recorded." Closure now strictly filters to `evidence_class == "recorded"`.
- `lineage.neighborhoods.build_neighborhood` previously excluded any artifact with an existing incoming edge regardless of evidence class, which meant an already-scored `inferred` candidate could never be found again by a later candidate-recovery pass (e.g. inside a revocation run). Now only `recorded`-evidence edges are excluded.
- RewindBench oracle artifact IDs were computed independently of the content-addressing scheme used when materializing cases into a real workspace, so oracle IDs never matched real workspace IDs and every prediction silently scored zero. Oracle IDs are now computed with the identical serialization/hash used by `Workspace.ingest_artifact`.

### Not implemented in this release
See `STATUS.md` for the full list: HTTP API, durable job worker, PostgreSQL/service mode, web dashboard, Docker/CI-CD, i18n, hosted-model replay adapter, scorer calibration pipeline.

## [0.1.0] — Research starter

- Publication-oriented research proposal, RFC 0001, JSON Schemas, RewindBench design document.
- Dependency-free deterministic recorded-lineage closure baseline.
- CLI for closure reports and recorded-only attestations.
- Six passing unit tests.
