# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — v0.3.0 work in progress (branch `feat/v0.3-completion`)

### Added
- `docs/completion-matrix-v0.3.md`, `docs/implementation-plan-v0.3.md`: evidence-based v0.3 gap inventory and phase plan.
- `skillrewind.persistence.service`: additive SQLAlchemy 2.0 models + Alembic migrations (`migrations/`) for Service-mode persistence (PostgreSQL target; SQLite exercised in tests since no live PostgreSQL server was reachable in the authoring environment). Does not modify Lite mode's existing `sqlite3`-based persistence.
- `skillrewind doctor` now checks Service-mode schema currency and fails (nonzero exit) when the configured database is behind the Alembic head.
- `make db-migrate` / `make db-current` targets; `service` optional dependency group (`sqlalchemy`, `alembic`, `psycopg[binary]`).
- `docs/adr/0009-service-mode-persistence.md`.
- 10 new tests (`tests/integration/test_service_persistence.py`, `tests/integration/test_doctor_service_mode.py`); 1 test skips honestly pending a reachable PostgreSQL instance.
- `skillrewind.jobs`: durable database-backed job queue (enqueue/claim/lease/heartbeat/retry-backoff/cancellation/lease-expiry-recovery/persisted progress events), `Worker` loop, and `worker-run`/`worker-once`/`jobs-list`/`jobs-show`/`jobs-cancel`/`jobs-retry`/`jobs-reap-expired`/`jobs-enqueue` CLI commands. One real handler (`benchmark.run`) wraps the existing RewindBench CLI pipeline and is proven idempotent under simulated worker crash/restart.
- `docs/adr/0010-job-handler-scope.md` documents why revocation/replay/rebuild/verification/attestation handlers are not wired yet.
- 27 new tests (`tests/unit/test_jobs.py`, `tests/integration/test_benchmark_job_handler.py`).
- `skillrewind.api` + `skillrewind serve`: a real, running FastAPI Service-mode API. Health/readiness (fails 503 before migration or with auth disabled in service mode), API-key admin with Argon2id hashing and scopes, `Idempotency-Key` support, an SSE event stream with `Last-Event-ID` resume, an in-memory rate limiter, CORS-off-by-default, and job/bench endpoints over the real Phase B queue. `fastapi`, `uvicorn`, `argon2-cffi`, `sse-starlette`, `httpx` added to the `service` extra.
- 28 new tests (`tests/integration/test_api.py`), including one that caught and fixed a real auth bug (key secrets containing `_` failed to parse).

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
