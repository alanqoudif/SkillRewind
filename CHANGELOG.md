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
- Real Service-mode artifact ingest/retrieval (`ArtifactRepository` + `POST/GET /api/v1/artifacts...`), backed by a real SQLAlchemy repository + `LocalCAS`. 8 new tests (`tests/integration/test_artifacts_api.py`).
- `revocation.execute` job handler wired to a made-checkpoint-resumable `run_revocation`; `revocation.execute` job-handler + resumability tests.

### Added — Phase C2.1: Service-mode derivations, evidence, lineage, and candidate recovery
- New Service-mode schema (Alembic migration `b29242c5289a`): `derivation_inputs`, `feature_observations`, `candidate_scoring_runs`, plus new columns on `candidate_scores` (`run_id`, `evidence_class`, `status`, `explanation_text`, `inclusion_reasons_json`). Six new `RelationType` values (`direct-input`, `retrieved-memory`, `distilled-from`, `generated-from-trace`, `imported-dependency`, reusing existing `context-exposure`) for derivation-input edges.
- `DerivationRepository`, `LineageRepository`, `FeatureRepository`, `CandidateRepository` (`skillrewind.persistence.service.repositories`): typed domain methods over the new schema; every write goes through a new hash-chained `record_audit_event` helper (actor, request correlation ID, timestamp, stable entity ID). Recorded derivation inputs are materialized into the existing `edges` table as `recorded`-evidence rows as soon as a derivation's output artifact is known.
- Derivation capture API: `POST /api/v1/derivations`, `GET .../{id}`, `POST .../{id}/inputs`, `POST .../{id}/output`, `GET /api/v1/artifacts/{id}/derivation|parents|children`. Idempotency-Key support; rejects nonexistent-artifact references, self-edges, and unknown relation types; duplicate input edges are silently deduplicated (no duplicate row or audit event).
- Recorded lineage query API: `GET /api/v1/lineage/{id}/descendants|ancestors|graph|cycles`, `GET /api/v1/lineage/path`. `descendants`/`ancestors` are hard-wired to `evidence_class == "recorded"` only — a dedicated regression test (`TestRecordedClosureInvariant`) proves an `inferred` edge never leaks into recorded closure, the same class of bug previously found and fixed in Lite mode.
- `skillrewind.features.bridge`: Service-mode feature-extraction bridge connecting the existing six feature-family extractors (expression/implementation/operational/behavioral/graph/temporal — unchanged, not reimplemented) to Service-mode artifacts via `ArtifactRepository`/CAS/`DerivationRepository`; persists observations through `FeatureRepository` with extractor version + config digest.
- `skillrewind.lineage.service_recovery`: Service-mode candidate-recovery application service reusing `skillrewind.inference.scoring.score_candidate` (no algorithm duplication). Checkpoint-resumable (already-scored candidates are skipped on resume), never writes a calibrated probability from a raw score, never marks a recovered edge `replay-confirmed`.
- `lineage.recover` job handler, registered with the existing worker; candidate recovery API: `POST /api/v1/lineage/recovery-runs` (202, enqueues the real job), `GET .../recovery-runs/{id}[/candidates]`, `GET .../candidates/{id}[/explanation]`.
- `GET /api/v1/artifacts/{id}/resolve`: serving resolution distinguishing `active`/`revoked`/`quarantined`/`superseded`/`unavailable`/`allowed-by-waiver`; side-effect free; never affected merely by an inferred candidate score.
- `skillrewind.service_import` + `skillrewind service-import` CLI: idempotent Lite-mode-workspace -> Service-mode import bridge (artifacts + recorded-only edges), with a dry-run mode and a structured import report.
- `docs/adr/0011-service-lineage-evidence.md`.
- 27 new tests across `tests/integration/test_derivation_lineage_api.py`, `tests/integration/test_lineage_recovery_acceptance.py` (the full numbered acceptance scenario against a fresh DB/CAS/API/worker), and `tests/integration/test_lineage_recover_job_handler.py` (failure/security/resumability/concurrency cases).
- **Not implemented in this increment**: replay/rebuild/verification/attestation Service-mode API endpoints (candidate edges stay `inferred`; only a real replay result — out of scope here — can promote one to `replay-confirmed`); full OpenAPI-example coverage beyond one derivation example; the full 16-item failure/security matrix from the milestone spec (a representative subset is covered — see `docs/completion-matrix-v0.3.md`).

### Added — Phase C2.2: Service-mode replay and evidence-promotion path
- New schema (Alembic migration `de2e581d5efc`): `replay_runs` table; `replay_records` gains `replay_run_id`/`repetition_index` (unique together, the checkpoint-resume key); `candidate_scores` gains a denormalized `latest_replay_run_id`/`latest_replay_verdict`/`latest_replay_at` pointer (never a rewrite of the original score/breakdown fields). New `ReplayVerdict.UNRESOLVED_MISSING_INPUTS` enum member.
- `skillrewind.replay.service_mode`: Service-mode paired-replay orchestration reusing the existing intervention-construction, approved-runner registry, comparator, fidelity, and paired-bootstrap-statistics functions unchanged (no replay science reimplemented). Checkpoint-resumable: a repetition already persisted is reconstructed from its stored payload rather than re-executed.
- `ReplayRepository` (`skillrewind.persistence.service.repositories`): `ReplayRun` creation/idempotency, per-repetition `ReplayRecord` persistence, and atomic final classification (`finalize_run`) that never mutates the originating `CandidateScore`'s score/evidence-class fields and only ever creates/updates one `edges` row for a definitive replay outcome.
- `lineage.replay` job handler, registered with the existing worker; checkpoint-resumable and idempotent after a crash (proven by a real crash-simulation test).
- Replay API: `POST /api/v1/replay/runs` (202; validates candidate existence, runner approval, and CAS content integrity for both artifacts before enqueue -- clear RFC-9457 422/404/409/403 errors for invalid candidates, unsupported runners, missing/corrupted CAS content, idempotency conflicts, and cross-tenant access), `GET .../runs/{id}`, `.../evidence`, `.../classification`; `GET /api/v1/lineage/candidates/{id}/history` (original inferred evidence plus every replay run submitted against it).
- Actor-scoped tenant isolation on all replay-run read endpoints (403 for a different actor's key without `admin` scope) -- see `docs/adr/0012-service-replay-evidence.md` for why this is actor-scoped rather than a new multi-tenant schema.
- `docs/adr/0012-service-replay-evidence.md`.
- 20 new tests (`tests/integration/test_service_replay_api.py`, `tests/integration/test_service_replay_job_handler.py`): replay-confirmed and rejected happy paths, runner-failure/fidelity-failure/insufficient-repetitions/missing-derivation all classifying as `unresolved` (never `rejected`), duplicate-submission and concurrent-submission idempotency, a real crash-mid-run resume proof with no duplicate repetitions or audit events, recorded-traversal and serving-resolution invariance after replay, cross-tenant rejection, tampered-CAS-input rejection, SSE progress resume during a real replay job, and a PostgreSQL-gated concurrent-job-claim test.
- **Not implemented in this increment**: rebuild/verification/attestation Service-mode API endpoints remain the next backend gate; a replay-confirmed edge does not itself trigger revocation/quarantine (invariant 4 requires that stay a separate, explicit action, not built here); PostgreSQL-gated tests (schema migration, job-claim concurrency, and this phase's replay-job-claim concurrency test) remain unexercised against a live server in this environment (Docker daemon not running) -- they skip with an explicit, reproducible reason and run for real wherever `SKILLREWIND_TEST_POSTGRES_URL` points at a reachable PostgreSQL instance.

### Added — Phase C2.3: Service-mode revocation, rebuild, verification, and attestation
- `skillrewind.persistence.service.workspace.ServiceWorkspace`: a Service-mode adapter duck-typing the Lite `Workspace` interface, so the existing tested revocation/barrier/quarantine/waiver/rebuild/verification/attestation orchestration runs unchanged against Service-mode SQLAlchemy persistence and CAS. `skillrewind.workspace_protocol.WorkspaceLike` (a `Protocol`) is the shared structural type both concrete workspace classes satisfy, used only for type hints. No new Alembic migration was required — the `revocation_events`/`revocation_transitions`/`quarantine`/`waivers`/`rebuild_plans`/`rebuild_attempts`/`verification_reports`/`attestations` tables already existed in the schema from an earlier, never-wired scaffolding pass.
- `skillrewind.revocation.service.build_revocation_preview`: a new side-effect-free preview function (recorded closure, replay-confirmed/rejected/unresolved/inferred-excluded breakdown, proposed targets with per-target evidence/reason, rebuild-recipe availability, a deterministic preview digest).
- `skillrewind.revocation.attestation_service`: idempotently projects a completed revocation's rebuild/verification results and a built/signed attestation into the dedicated `rebuild_plans`/`rebuild_attempts`/`verification_reports`/`attestations` tables.
- `revocation.execute` job handler now branches on payload shape (`{database_url, cas_root, event_id}` vs the pre-existing `{workspace_dir, event_id}`) to run the same `run_revocation` against either a `ServiceWorkspace` or a Lite `Workspace`.
- New API routers: `POST /api/v1/revocations/preview`, `POST /api/v1/revocations`, `GET .../{id}[/targets|/preview]`, `POST .../{id}/cancel`; `GET /api/v1/quarantine[/{id}]`, `GET /api/v1/artifacts/{id}/quarantine`; `POST /api/v1/waivers`, `GET .../{id}`, `GET /api/v1/artifacts/{id}/waivers`, `POST /api/v1/waivers/{id}/revoke`; `POST /api/v1/attestations`, `GET .../{id}[/canonical]`, `GET .../{id}/render?format=markdown|html`, `POST .../{id}/sign`, `POST .../{id}/verify`.
- Artifact ingest (`POST /api/v1/artifacts`) accepts an optional bounded `X-Metadata` JSON header, needed for behavioral-probe features to be recorded through the API (previously unreachable — the `_MAX_INLINE_METADATA_BYTES` bound existed unused).
- `docs/adr/0013-service-revocation-and-verified-recovery.md`.
- 7 new tests: a full end-to-end acceptance test (`tests/integration/test_service_revocation_e2e.py`, 46 assertions against real DB rows/CAS objects/job events, not status codes alone) covering candidate-recovery-API -> replay-API -> side-effect-free-preview -> submit -> real-worker -> barrier -> quarantine -> clean-room-rebuild -> verification -> atomic-publication -> attestation-build-sign-verify-render -> idempotent-resubmission -> SSE-event-ordering -> recorded-closure-invariant; plus `tests/integration/test_service_revocation_extras.py` (Service-mode crash-mid-rebuild-then-resume, forensic-mode side-effect-freedom, waiver authorization/expiry/scope tests).

### Fixed
- `run_revocation` (`skillrewind.revocation.service`, shared by both modes) was appending a replay-confirmed target to `event.quarantined` even under `forensic` policy, where `quarantine_artifact` is never actually called — a latent bug in already-shipped Lite logic, invisible until a Service-mode test asserted the invariant directly (no prior Lite test checked this field for forensic mode).
- `GET /api/v1/artifacts/{id}/resolve` checked the `quarantined` branch before `superseded`, so a rebuilt-and-published artifact's original (whose quarantine-history row is deliberately preserved, never deleted) resolved as `quarantined` instead of `superseded`. `superseded` is now checked first.
- Two API routers (`quarantine.py`, `waivers.py`) registered their `/artifacts/{artifact_id}/...` suffix routes with a plain `{artifact_id}` path parameter, which cannot match artifact IDs containing `/` (every artifact ID does, e.g. `skill://name@sha256:...`); fixed to `{artifact_id:path}`, and their routers now register before `artifacts.router`'s catch-all, mirroring the existing `derivations.router`-before-`artifacts.router` fix from Phase C2.

### Reduced-depth items in this increment
- Standalone `rebuilds.py`/`verifications.py` API routers are not separate resources; rebuild/verification results are surfaced via the revocation detail response (`rebuilt`/`unresolved`) plus direct queries against `rebuild_attempts`/`verification_reports`. The data is real and persisted; only the dedicated top-level listing endpoints from the milestone's preferred route shape are absent.
- Waivers with `scope="quarantine-release"` (the default) still perform a one-time quarantine release (reused unchanged from Lite mode) rather than a continuously-evaluated overlay that auto-reverts at expiry; a non-default scope (e.g. `"temporary-review-access"`) does leave the quarantine intact and surfaces via the resolver's `allowed-by-waiver` branch, which *is* expiry-bounded and revocable.
- SSE ordering/resumability is proven at the `JobQueue.events(after_event_id=...)` level (what the SSE endpoint itself polls), not via a full streaming HTTP client test.
- The 40+-item failure/security/policy-mode test matrix in the milestone spec is covered by a representative subset (documented above), not exhaustively enumerated.

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
