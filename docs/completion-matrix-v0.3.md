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
| SQLite WAL Lite mode | complete | `src/skillrewind/persistence/database.py`, `docs/adr/0003-persistence.md` |
| Hash-chained audit log | complete | `src/skillrewind/audit/log.py`, tested tamper detection |
| PostgreSQL service mode | missing | no `sqlalchemy`/`psycopg` dependency in `pyproject.toml` |
| Migrations (Alembic or explicit) | missing | schema applied idempotently at `connect()`, no versioned migration files |
| CAS backend abstraction / S3 adapter | partial | `src/skillrewind/cas/base.py` defines a protocol; only `local.py` implements it |

### Durable jobs / worker (Phase B)
| Requirement | Status | Evidence |
|---|---|---|
| Job queue, lease, retry, cancellation | missing | `serve`/`worker` CLI commands are explicit stubs (ADR-0004) |
| All domain work synchronous in-process | complete (by design) | every service module (`revocation/service.py`, `rebuild/service.py`, etc.) runs synchronously |

### API (Phase C)
| Requirement | Status | Evidence |
|---|---|---|
| FastAPI app, `/api/v1/*` | missing | no `fastapi`/`uvicorn` dependency; no `api/` package |
| API-key auth, idempotency, SSE | missing | none of the above exist |

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
