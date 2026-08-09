# Project status — version 0.2.0 (Research Preview), v0.3.0 work in progress on `feat/v0.3-completion`

This file lists exactly what is implemented, tested, and honestly claimed as of this release, and what is explicitly deferred. If a capability is not listed under "Implemented," assume it does not exist — do not infer it from the master specification this repository was built against.

## v0.3.0 progress (branch `feat/v0.3-completion`, not yet merged to `main`)

See `docs/completion-matrix-v0.3.md` for the full evidence-based inventory and `docs/implementation-plan-v0.3.md` for the phase sequence. As of this commit:

- **Service-mode persistence (Phase A, partial)**: a new, additive `skillrewind.persistence.service` package adds SQLAlchemy 2.0 models for the full Service-mode entity set (artifacts, edges, candidate scores, replay records, revocation events/transitions, quarantine, waivers, rebuild plans/attempts, verification reports, attestations, an audit-event projection, API keys, idempotency records, durable jobs + job events, benchmark runs), with real Alembic migrations at `migrations/` and a `schema_current()` check wired into `skillrewind doctor` (fails readiness — nonzero exit — when Service mode is configured and the schema is behind head). See `docs/adr/0009-service-mode-persistence.md` for why this is additive rather than a rewrite of Lite mode's existing `sqlite3`-based repositories, which are untouched. PostgreSQL itself has not been exercised against a live server in this environment (Docker's daemon is not running here — `docker info` fails to connect); the Postgres-specific test skips honestly with that exact reason and runs for real wherever `SKILLREWIND_TEST_POSTGRES_URL` points at a reachable PostgreSQL instance.
- **Durable jobs + worker (Phase B, partial)**: `skillrewind.jobs` implements a real, database-backed job queue (enqueue/claim/lease/heartbeat/retry-with-backoff/cancellation/lease-expiry-recovery, persisted progress events) over the `jobs`/`job_events` tables from Phase A, plus a `Worker` loop and `skillrewind worker-run`/`worker-once`/`jobs-*` CLI commands. 24 tests pass using a deterministic fake clock, including a simulated worker-crash-and-lease-recovery scenario. Exactly one real handler is wired: `benchmark.run` (wraps the existing, already-tested RewindBench CLI pipeline; proven idempotent under simulated worker crash/restart). Handlers for revocation/replay/rebuild/verification/attestation progression are **not** wired — `docs/adr/0010-job-handler-scope.md` explains why (the existing `run_revocation` is not checkpoint-resumable, so wrapping it today would make the spec's "resumes without duplicating quarantine/rebuild/attestation" requirement false rather than true). PostgreSQL's `FOR UPDATE SKIP LOCKED` multi-worker claiming path exists in code but has not been exercised against a live PostgreSQL server in this environment.
- **FastAPI service (Phase C, partial)**: `skillrewind.api` + `skillrewind serve` is a real, running HTTP API (manually verified end to end with live curl requests, not just in-process tests): `/health/live`, `/health/ready` (fails 503 before migration or when Service mode has auth disabled), `/version`, `/api/v1/schemas/{name}` (path-traversal-safe), API-key admin (create/list/revoke, Argon2id-hashed, scoped, plaintext shown once), scope enforcement, `Idempotency-Key` replay/conflict, RFC-9457-shaped errors, pagination, an SSE `/api/v1/events/stream` with `Last-Event-ID` resume (tested for correct ordering and no duplicate delivery), an in-memory single-instance rate limiter, CORS off by default, and `POST /api/v1/bench/runs` + job management endpoints wired to the real Phase B job queue with a full API-to-worker-to-API lifecycle test. 28 new tests, all passing (one caught and fixed a real bug: naive key-parsing broke on `token_urlsafe` secrets containing underscores). Artifact/lineage/revocation/replay/rebuild/attestation endpoints from spec section 8.2 are **not implemented** — their Service-mode schema (Phase A) has no writer yet, and shipping endpoints that always return empty data was judged a "fake UI" violation rather than real functionality.
- **Not yet built**: the web dashboard (Phase D), Docker sandbox replay (Phase E), the remaining 5 RewindBench scenario families and calibration pipeline (Phase F), and everything in Phases G–K. Do not infer any of these exist.

## Implemented and tested (v0.2.0 baseline — still current on `main`)

### Foundation
- Deterministic, project-specific canonical JSON serialization (`skillrewind.canonical.json`) with SHA-256 hashing; rejects NaN/Infinity/raw bytes.
- Full-length `scheme://name@sha256:<64-hex>` artifact ID scheme with a documented legacy/demo compatibility mode.
- Local filesystem content-addressed store: atomic writes, dedup, corruption detection (`verify_integrity`), configurable size limits, no path traversal.
- Hash-chained, append-only audit log (SQLite-backed) with a verifier that detects tampering (payload mutation, deletion, reordering); `skillrewind audit-verify` / `audit-export`.
- SQLite persistence (WAL mode) via the standard library `sqlite3` module directly — no SQLAlchemy/Alembic (see `docs/adr/0003-persistence.md`).
- Typed configuration with CLI > env (`SKILLREWIND_*`) > `skillrewind.toml` > defaults precedence.

### Capture and recorded lineage
- Agent Skills directory adapter: `SKILL.md` frontmatter parsing, per-file hashing, symlink/traversal rejection, read-only ingestion, round-trip export.
- Capture SDK (`skillrewind.capture.sdk.SkillRewindClient`): context-manager derivation tracking, secret redaction, failed-derivation preservation.
- Generic JSONL trace batch import.
- Recorded-lineage graph: closure/ancestry, cycle detection, induced subgraphs, relation/evidence/status filtering, Mermaid/JSON export. Recorded closure strictly uses `evidence_class == "recorded"` edges only.
- Serving-resolution layer (`Workspace.resolve_alias` / `skillrewind resolve`): never resolves a revoked or quarantined artifact.

### Hidden-lineage recovery
- Bounded candidate-neighborhood construction (time window, shared recipe/model/context-pool, artifact-kind compatibility) with exposed reasons.
- Six feature families: expression (token/char n-gram), implementation (Python AST-based, no code execution), operational (tool-call sequence/multiset), behavioral (stored-probe comparison only, never live execution), graph (shared ancestry/path proximity), temporal (decay-based recency).
- Deterministic weighted scorer with missing-feature renormalization, a strict-negative heuristic that suppresses false high-confidence labels pending replay, and full score-breakdown explanations.
- **Not implemented:** the offline calibration pipeline (logistic regression + Platt/isotonic scaling, Brier score, ECE) described in the spec. Only the hand-weighted deterministic scorer exists; its weights are provisional, not validated.

### Replay
- `DerivationRunner` protocol + approved-runner registry (`UnapprovedRunnerError` on unknown names — no arbitrary module-path execution).
- `DeterministicFixtureRunner`: fully offline, used by the demo, tests, and RewindBench.
- `SandboxedSubprocessRunner`: allowlisted recipes only, sanitized environment, CPU/memory/process rlimits (POSIX), timeout + tree kill. **Not real network-namespace isolation** — documented as weaker than a container.
- **Not implemented:** `OpenAICompatibleRunner` (an optional hosted-model adapter). No test or the demo requires it.
- Paired present/withheld/clean-control/irrelevant-control intervention builders.
- Composable comparators (canonical-JSON-based target-behavior-vector diffing), exposed multi-component fidelity reports (never collapsed to one opaque number), paired-bootstrap statistics for repeated runs with an explicit insufficient-sample-size flag.
- Classification into confirmed / rejected / unresolved-fidelity / unresolved-repetitions / unresolved-runner-failure. A runner error always produces `unresolved`, never `rejected`.
- Budget-aware active selector (severity × closure-impact × bridge-weight × uncertainty × fidelity / cost) plus exhaustive, highest-score-first, and seeded-random baselines.

### Revocation, quarantine, rebuild, verification
- Durable(-in-SQLite) state machine with enforced transitions (`InvalidStateTransitionError` on invalid moves); `forensic`/`balanced`/`strict` policies.
- Barrier application for `balanced`/`strict`: revokes roots and quarantines recorded descendants before candidate recovery begins. `forensic` never mutates serving state.
- Idempotency: re-requesting a revocation with the same idempotency key returns the existing event rather than duplicating state.
- Quarantine service + explicit, audited, optionally-expiring waivers; `strict_forbids_waivers` config gate.
- Clean-room rebuild planner: excludes revoked roots, quarantined support, and replay-confirmed contaminated ancestors; produces a plan digest before execution; never mutates the original artifact.
- Verification suites (canary-absence, forbidden-predecessor-support, utility-retention-threshold checks) with a machine-readable report.
- Atomic successor publication: alias moves to the verified successor; the original becomes `superseded`, never silently reactivated.

### Attestation
- Bounded attestation v0.2 (`skillrewind.attestation.build_attestation`): every field is generated from persisted event/edge/replay state; bounded claims are narrow and never claim erasure from a foundation model or universal absence of influence.
- Markdown/HTML rendering.
- Local Ed25519 keygen/sign/verify (`cryptography`); private key written with `0600` permissions.
- Digest and signature verification both detectably fail after byte-level tampering (tested).
- v0.1 `skillrewind attest --edges ... --root ... --output ...` recorded-only command preserved unchanged and tested.

### RewindBench-core
- Controlled oracle case generator: `direct-inheritance`, `semantic-laundering`, `implementation-mutation` scenario families; `none`/`uniform-random-dropout` provenance-loss operators; every case includes one strict-negative artifact.
- Oracle artifact IDs are computed with the exact same content-addressing scheme the workspace uses, so predictions and ground truth are directly comparable (this was a real bug found and fixed during this session — see `docs/adr/0002-canonical-serialization.md`).
- Baselines: `delete-root`, `recorded-closure`, `static-multitrace`, `exhaustive-replay` — all real, executable, no fabricated numbers.
- Metrics: precision/recall/F1 (micro), false-quarantine rate, replay-call cost; a 30-case confidence-interval-sufficiency threshold is enforced and reported.
- `smoke`/`ci`/`research`/`paper` presets; `paper` is intentionally never run automatically.
- `make bench-smoke` runs `generate` → `run` → `score` → `report` end to end and writes a `runs/<id>/` directory with `experiment-manifest.json`, `predictions.jsonl`, `raw-metrics.json`, `summary.csv`, `report.md`, `environment.json`.
- **Not implemented:** `procedural-inheritance`, `multi-hop-contamination`, `memory-to-skill-promotion`, `cross-model-distillation`, `compositional-influence` scenario families; `type-selective`/`bridge-targeted`/`attribution-selective` provenance-loss operators; Arabic fixtures; embedding-similarity baseline; Brier score / ECE.

### CLI
- 30+ commands across `artifact-*`, `capture-*`, `edge-*`, `closure`/`ancestors`/`graph-export`, `candidates`, `replay-*`, `revoke-*`, `quarantine-list`, `waiver-*`, `rebuild-*`, `verify-run`, `attest-*`, `audit-*`, `resolve`, `init`, `doctor`. JSON output via `--output`.
- `serve`/`worker` commands exist and print an explicit "not implemented in this session" message (exit code 3) rather than pretending to start a service.

### Primary demo
- `make demo` runs the full `poisoned-descendant` scenario end to end, fully offline, printing 13 numbered milestones and writing a signed attestation. Verified in this session: recorded closure misses the hidden descendant, static scoring recovers it, the strict negative is not falsely flagged as high-confidence, paired replay confirms it, balanced revocation quarantines it, clean-room rebuild + verification produces a safe successor, and `resolve()` never returns the quarantined original.

## Explicitly not implemented in this session

These are real gaps, not hidden behind vague language:

- **HTTP API** (`/api/v1/...`), OpenAPI, SSE progress streaming, API-key auth/scopes.
- **Durable job queue / worker process.** All work in this release runs synchronously in the calling process.
- **PostgreSQL / service deployment mode.** Only SQLite "lite mode" exists.
- **React web dashboard**, i18n (English/Arabic), Playwright e2e tests.
- **Docker images, Docker Compose, Kubernetes/Helm.** `make docker-build`/`docker-smoke` print a clear "not implemented" message.
- **GitHub Actions CI/CD workflows**, Dependabot, SBOM generation, container scanning, Sigstore/Cosign signing.
- **Backup/restore tooling**, Alembic-style migrations (schema is additive-only, applied idempotently at `connect()` time).
- **OpenTelemetry tracing/metrics export**, structured JSON service logging (only plain Python exceptions/print output exists in this CLI-only release).
- **LaTeX paper build verification** — not attempted in this session; the paper source was not modified.
- **Cosign/hosted-model replay adapter** (`OpenAICompatibleRunner`).
- **Scorer calibration pipeline** (logistic regression / Platt / isotonic, Brier score, ECE).

## Known limitations / honest caveats

- The candidate scorer's feature weights (`FeatureWeights` defaults) are hand-set and provisional; they have not been validated against a held-out calibration set. Treat `candidates` output as a decision-support signal for replay prioritization, not a ground-truth lineage claim.
- The sandboxed-subprocess replay runner provides weaker isolation than a real container (no network namespace); it is suitable for local benchmark fixtures, not untrusted third-party code.
- RewindBench-core currently covers 3 of the 8 scenario families and 2 of the 5 provenance-loss operators described in the original specification.
- All reported benchmark numbers in this repository come from `make bench-smoke`'s 3-case `smoke` preset and are explicitly labeled descriptive-only (below the 30-case confidence-interval threshold). No paper-scale experiment has been run.
- This repository has not undergone external security review. Do not deploy any part of it against production credentials or untrusted third-party artifacts without further hardening (see the sandbox caveat above).

## Immediate next milestones

1. Wire a FastAPI service layer over the existing `Workspace`/service-module API surface (the domain logic is already decoupled from the CLI, so this should not require re-deriving business logic).
2. Add a database-backed job queue so replay/rebuild/verification can run asynchronously.
3. Expand RewindBench-core to the remaining 5 scenario families and the remaining 3 provenance-loss operators.
4. Build the offline calibration pipeline and re-derive the scorer's default weights from it instead of hand-tuning.
