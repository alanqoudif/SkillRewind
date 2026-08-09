# SkillRewind v0.3 implementation plan

See `docs/completion-matrix-v0.3.md` for the evidence behind every gap listed here.

## Principles

1. The v0.2.0 offline vertical slice (`main` tag `v0.2.0-research-preview`) is frozen and must keep passing its 86 tests, `make demo`, and legacy `closure --edges`/`attest --edges` at every commit.
2. New work lands as small-to-medium commits on `feat/v0.3-completion`, one per coherent milestone, each with passing tests before moving on.
3. No phase is declared done in `STATUS.md` unless its tests actually run and pass in this environment. Environment-blocked items (no Docker, no network) get an honest skip with the exact command and error recorded, not a fabricated pass.
4. Persistence is the dependency root: jobs, API, and UI all need Phase A's abstraction before they can be real rather than mocked.

## Phase sequence and dependencies

```
Phase A (persistence: SQLAlchemy + Alembic + Postgres)
   -> Phase B (durable jobs/worker, needs job table)
        -> Phase C (FastAPI: enqueues jobs, needs A+B)
             -> Phase D (web dashboard, needs C's API)
Phase E (replay adapters/Docker sandbox) -- independent, can run parallel to A/B
Phase F (RewindBench families + calibration) -- independent, can run parallel to A-E
Phase G (observability/security) -- threads through B/C as they're built
Phase H (containers/Compose/backup) -- needs A (Postgres) + C (API) + D (web) images to exist
Phase I (CI/CD) -- needs H's Docker targets and G's test suites to have something to run
Phase J (docs) -- continuous, finalized last
Phase K (performance smoke + final verification) -- last
```

## Acceptance checks per phase (abbreviated; full detail in the master spec)

- **A**: fresh-DB migration test, v0.2-fixture-DB migration test, PostgreSQL integration test (skips honestly without Docker/`DATABASE_URL`), all 86 existing tests still pass against the new persistence layer.
- **B**: enqueue/claim/heartbeat/retry/cancel/lease-recovery tests with an injectable clock; SQLite single-worker guard; Postgres `FOR UPDATE SKIP LOCKED` two-worker no-double-execution test (Postgres-gated).
- **C**: full endpoint contract tests, auth/scope matrix, idempotency replay+conflict, SSE resume, no secrets in errors.
- **D**: `tsc --noEmit`, component tests, Playwright primary flow (English) + Arabic/RTL pass against real API+worker.
- **E**: Docker network-off proof test (Docker-gated, honest skip otherwise), OpenAI-adapter offline mock tests, no live network call in default test run.
- **F**: all 8 families generate valid cases including Arabic; oracle isolation test; calibration leakage test; smoke preset stays offline.
- **G**: secret-redaction fixture test; doctor exit codes; CORS/rate-limit tests.
- **H**: `make docker-smoke` real pass when Docker available, honest skip otherwise; backup/restore round-trip test in a temp Lite workspace.
- **I**: workflow YAML lints; no job requires paid API keys; Dependabot config valid.
- **J**: README commands verified from a clean checkout; STATUS.md matches actual state at time of final commit.
- **K**: performance smoke script runs and records real numbers with hardware/seed metadata, no marketing threshold baked into CI.

## Environment constraints already known

- No Docker CLI has been confirmed available/unavailable yet in this environment — checked at the start of Phase E/H and recorded with the exact command run.
- No PostgreSQL server is assumed running — Postgres-gated tests will check for `DATABASE_URL`/a reachable local Postgres and skip with a printed reason if absent, never fabricate a pass.
- No outbound network calls are used for any default test target; the OpenAI-compatible adapter's live-integration test is opt-in via an environment variable per the master spec.
