# SkillRewind v0.3 completion matrix

Evidence-based inventory of the v0.2.0 baseline against the v0.3 "Self-hosted Research Platform Beta" target. Every row is backed by a command actually run or a file actually inspected in this working tree — not by the prior implementation report's prose.

Legend: `complete` / `partial` / `missing` / `blocked-by-environment` / `intentionally-out-of-scope`

## Baseline verification (2026-08-09)

| Check | Command | Result |
|---|---|---|
| Test suite | `python -m pytest -q` | **86 passed** in 1.66s (matches STATUS.md claim exactly) |
| Lint | `ruff check .` | All checks passed |
| Types | `mypy src` | Success: no issues found in 79 source files |
| Source file count | `find src -name '*.py' \| wc -l` | 79 (matches claim) |
| CLI surface | `skillrewind --help` | 34 subcommands, incl. `serve`/`worker` stubs that print "not implemented" and exit 3 |
| Demo | `make demo` | All 13 milestones printed correctly, attestation signed+verified, exit 0 |
| Git tree | `git status` | Clean before this session; tagged `v0.2.0-research-preview` |
| `web/` | `ls web` | Does not exist |
| `.github/` | `ls .github` | Does not exist |
| Docker files | `find . -iname Dockerfile* -o -iname 'compose*.yaml'` | None found |
| Persistence | `src/skillrewind/persistence/database.py` | Raw `sqlite3`, WAL mode, no SQLAlchemy/Alembic (ADR-0003) |
| ADRs present | `docs/adr/*.md` | 8 ADRs (0001–0008), incl. 0004 "durable-jobs-deferred", 0005 "sandbox-no-network-default", 0008 "optional-embeddings" — all explicitly document current gaps |
| RewindBench families | `src/skillrewind/bench/cases.py` | 3 of 8 families (`direct-inheritance`, `semantic-laundering`, `implementation-mutation`) |

**Conclusion**: the prior implementation report's "implemented" list and STATUS.md's "Explicitly not implemented" list are both accurate. No blind trust was required to be revised downward — verification confirmed the claims.

## Requirement matrix

### Persistence / Service mode (Phase A)
| Requirement | Status | Evidence |
|---|---|---|
| SQLite WAL Lite mode | complete | `src/skillrewind/persistence/database.py`, `docs/adr/0003-persistence.md` (unchanged) |
| Hash-chained audit log | complete | `src/skillrewind/audit/log.py`, tested tamper detection |
| PostgreSQL service mode schema/engine | complete (schema layer only) | `src/skillrewind/persistence/service/{models,engine}.py`; SQLAlchemy 2.0, dialect-portable; not yet exercised against a live PostgreSQL server in this environment (Docker daemon not running) — see `docs/adr/0009-service-mode-persistence.md` |
| Migrations (Alembic) | complete | `migrations/` (Alembic), `tests/integration/test_service_persistence.py::test_alembic_upgrade_head_on_fresh_database` |
| Readiness fails on stale schema | complete | `skillrewind doctor` exits nonzero in Service mode when schema is behind head; `tests/integration/test_doctor_service_mode.py` |
| Data migration: existing Lite SQLite -> Service PostgreSQL | missing | not started; ADR-0009 explicitly scopes this out as a separate ETL problem |
| Domain services wired to Service-mode schema (jobs/API actually reading/writing it) | missing | schema exists but has no consumer yet — that's Phase B/C |
| PostgreSQL concurrent-job-claim (`FOR UPDATE SKIP LOCKED`) | missing | no worker exists yet (Phase B) |
| CAS backend abstraction / S3 adapter | partial | `src/skillrewind/cas/base.py` defines a protocol; only `local.py` implements it |

### Durable jobs / worker (Phase B)
| Requirement | Status | Evidence |
|---|---|---|
| Job queue: enqueue/claim/lease/heartbeat/retry-backoff/cancel/lease-expiry-recovery | complete | `src/skillrewind/jobs/queue.py`; 24 tests in `tests/unit/test_jobs.py` with a deterministic `FakeClock`, incl. simulated worker crash + lease recovery |
| Worker loop (`run_once`/`run`), handler registry | complete | `src/skillrewind/jobs/worker.py` |
| `worker-run`/`worker-once`/`jobs-*` CLI commands | complete | `skillrewind worker-run\|worker-once\|jobs-list\|jobs-show\|jobs-cancel\|jobs-retry\|jobs-reap-expired\|jobs-enqueue`; refuses to run against an unmigrated schema (`ServiceModeUnavailable`) rather than silently mutating it |
| PostgreSQL `FOR UPDATE SKIP LOCKED` claiming | complete (code), not exercised live | `JobQueue.claim` uses it when `engine.dialect.name == "postgresql"`; no live PostgreSQL server reachable in this environment to prove two-worker exclusivity end-to-end (same constraint as Phase A) |
| SQLite explicitly not claiming multi-worker safety | complete | `JobQueue.is_multi_worker_safe` reports `False` on SQLite; tested |
| Persisted progress events (for future SSE) | complete | `job_events` table, strictly increasing `event_id`; tested ordering |
| Secret redaction in job errors/events | complete | reuses `skillrewind.capture.redaction.Redactor`; tested |
| Real handler: `benchmark.run` | complete | `src/skillrewind/jobs/handlers.py`, wraps the existing tested `skillrewind.bench.cli` pipeline; idempotent-resume tested end to end |
| Handlers for candidate recovery / replay / revocation / rebuild / verification / attestation | missing (deliberate, documented) | `docs/adr/0010-job-handler-scope.md`: `run_revocation` is not currently checkpoint-resumable, so wrapping it today would make the "resumes without duplicating quarantine/rebuild/attestation" test requirement false; scoped out rather than faked |

### API (Phase C)
| Requirement | Status | Evidence |
|---|---|---|
| FastAPI app factory, `skillrewind serve` | complete | `src/skillrewind/api/app.py`; boots a real uvicorn server, manually verified with live curl requests |
| `/health/live`, `/health/ready` (schema + auth-safety checks) | complete | tested incl. 503 before migration and when `api_auth_disabled` is true in Service mode |
| `/version`, `/openapi.json`, `/api/v1/schemas/{name}` (path-traversal-safe) | complete | tested |
| API-key auth: create/list/revoke, Argon2id hash, scopes, prefix lookup | complete | `src/skillrewind/api/auth.py`; a real bug was found and fixed during testing (naive `split("_")` broke on `token_urlsafe` secrets containing `_`) |
| Scope enforcement matrix | complete | tested: insufficient scope -> 403, admin-only endpoints, `admin` implies all scopes |
| Idempotency-Key: replay + conflict | complete | `src/skillrewind/api/idempotency.py`, tested both paths |
| Pagination, RFC-9457-shaped error envelope | complete | tested |
| SSE `/api/v1/events/stream` with `Last-Event-ID` resume | complete | polls the real `job_events` table; tested ordering and no-duplicate-on-resume |
| Rate limiting (in-memory, single-instance, honestly documented as such) | complete | global middleware; tested 429 + `Retry-After` |
| CORS disabled by default, allowlist-only when configured | complete | tested both states |
| `POST /api/v1/bench/runs` + `GET .../jobs`, `.../jobs/{id}[/cancel\|/retry]` | complete | real endpoints over the Phase B job queue; full lifecycle tested through API -> worker -> API |
| Artifact/derivation/edge/lineage/candidate/replay/revocation/quarantine/waiver/rebuild/verification/attestation endpoints (spec 8.2) | missing (deliberate) | the Service-mode SQLAlchemy tables for these (Phase A) have no writer yet -- shipping endpoints that always return empty results was assessed as violating the "no fake UI" rule; see `src/skillrewind/api/app.py` module docstring |
| WebSocket transport | intentionally-out-of-scope | spec 8.7 states SSE is sufficient, WebSockets unnecessary |

### Web dashboard (Phase D)
| Requirement | Status | Evidence |
|---|---|---|
| React/TS dashboard, i18n en/ar, RTL, Playwright | missing | no `web/` directory |

### Replay adapters / sandbox (Phase E)
| Requirement | Status | Evidence |
|---|---|---|
| Runner protocol + allowlist registry | complete | `src/skillrewind/replay/base.py` |
| Deterministic fixture runner | complete | `src/skillrewind/replay/deterministic.py`, used by demo/tests/bench |
| Subprocess runner (weak isolation, documented) | complete | `src/skillrewind/replay/sandbox.py`, ADR-0005 explicitly states no network-namespace isolation |
| Docker sandbox runner (network-off) | missing | no Docker-based runner |
| OpenAI-compatible adapter | missing | not present, no test requires it |
| Embedding adapter | missing | ADR-0008 documents the deferral |

### RewindBench / calibration (Phase F)
| Requirement | Status | Evidence |
|---|---|---|
| 3 of 8 scenario families | partial | `direct-inheritance`, `semantic-laundering`, `implementation-mutation` implemented in `src/skillrewind/bench/cases.py` |
| Remaining 5 families (procedural-inheritance, multi-hop, memory-to-skill, cross-model-distillation, compositional) | missing | not present |
| Arabic fixtures | missing | none |
| `none` + `uniform-random-dropout` loss operators | complete | `src/skillrewind/bench/cases.py` |
| Remaining 3 loss operators (type-selective, bridge-targeted, attribution-selective) | missing | not present |
| 4 baselines (`delete-root`, `recorded-closure`, `static-multitrace`, `exhaustive-replay`) | complete | `src/skillrewind/bench/harness.py` |
| Embedding-similarity / active-selection baselines | missing | not present |
| Calibration pipeline (Platt/isotonic, Brier, ECE) | missing | scorer weights are hand-set (`src/skillrewind/inference/scoring.py`), STATUS.md confirms |

### Observability / security (Phase G)
| Requirement | Status | Evidence |
|---|---|---|
| Typed config, CLI>env>toml>defaults | complete | `src/skillrewind/config.py` |
| `doctor` command | partial | validates config/storage/audit chain; no schema-version, worker, Docker, or provider checks yet |
| Structured JSON logging, metrics, tracing | missing | plain print/exceptions only |
| Path traversal / zip-slip / symlink protections | complete (CAS + Agent Skills adapter) | tested in `tests/` |

### Containers / ops (Phase H)
| Requirement | Status | Evidence |
|---|---|---|
| Dockerfiles, Compose | missing | `make docker-build`/`docker-smoke` print "not implemented" |
| Backup/restore | missing | not present |

### CI/CD (Phase I)
| Requirement | Status | Evidence |
|---|---|---|
| GitHub Actions workflows | missing | no `.github/` directory |
| Dependabot | missing | none |
| Release dry run | missing | none |

### Docs (Phase J)
| Requirement | Status | Evidence |
|---|---|---|
| README/STATUS/CHANGELOG honest and current | complete | verified by reading; matches actual repo state |
| ADRs for existing decisions | complete | 8 ADRs present |
| `SECURITY.md`, `CITATION.cff`, `LICENSE` | complete | present, spot-checked |

### Performance (Phase K)
| Requirement | Status | Evidence |
|---|---|---|
| Performance smoke tooling | missing | not present |

## Scope reality check

This matrix covers roughly 20 major subsystems (PostgreSQL + Alembic migrations, a database-backed job/worker system, a 40+ endpoint FastAPI service with auth/idempotency/SSE, a React+TypeScript dashboard with English/Arabic/RTL and Playwright E2E, Docker sandbox isolation with 8 replay adapters, 5 additional RewindBench scenario families with Arabic fixtures plus a leakage-safe calibration pipeline, observability, Docker/Compose/backup-restore, and CI/CD). Each is independently a multi-day-to-multi-week engineering effort if built to the rigor this spec demands (real tests, no placeholders, no fabricated metrics). It is not realistic to complete all of it inside one working session while also holding to the "no fabricated results / no fake UI / no core-path placeholders" integrity rules the spec itself imposes.

The plan in `docs/implementation-plan-v0.3.md` sequences the phases by dependency and lands each as a real, tested, locally committed increment rather than attempting shallow coverage of everything at once.
