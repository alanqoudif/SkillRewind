# ADR 0009: Service-mode persistence is additive (SQLAlchemy + Alembic), not a Lite-mode rewrite

## Status
Accepted (v0.3.0-in-progress)

## Context
ADR-0003 deferred PostgreSQL/SQLAlchemy entirely, and named the repository classes (`ArtifactRepository`, `EdgeRepository`, etc. in `skillrewind.persistence.repositories`) as the intended seam for a future service-mode swap. Building Service mode now raises the question this ADR resolves: rewrite those repositories and the raw-`sqlite3` schema in `skillrewind.persistence.database` to run through SQLAlchemy against both SQLite and PostgreSQL, or add a second, parallel persistence layer for Service mode only.

A full rewrite was considered and rejected for this increment:
- The Lite-mode repositories use SQLite-specific syntax in at least one place (`INSERT OR REPLACE` in `QuarantineRepository`, `INTEGER PRIMARY KEY AUTOINCREMENT` in the `revocation_transitions` table) that does not translate to PostgreSQL without either a SQL-dialect-translating wrapper (fragile, effectively reinventing an ORM's dialect layer badly) or hand-porting every repository method (high risk to the 86 tests and the `make demo` vertical slice this whole project's credibility rests on, for a mode — Service — that has no consumer yet since Phase B/C, the job queue and API, are not built).
- Section 6.1 of the master spec explicitly permits this: *"If the existing persistence layer is custom, wrap it rather than forcing every core module through an unsafe rewrite."*

## Decision
Add `skillrewind.persistence.service` as a new, independent package:
- `models.py`: SQLAlchemy 2.0 declarative models for the full Service-mode entity set from spec section 6.2 (artifacts, alias history, derivations, edges, candidate scores, replay records, revocation events/transitions, quarantine, waivers, rebuild plans/attempts, verification reports, attestations, an audit-event projection, API keys, idempotency records, durable jobs + job events, benchmark runs).
- `engine.py`: `build_engine(database_url)` (SQLite or `postgresql+psycopg://`), `create_all()` for dev/test bootstrap, `schema_current(engine)` that compares the live `alembic_version` row against the repository's Alembic head — this is what Service-mode `/health/ready` and `skillrewind doctor` must call to refuse to serve mutating traffic against a stale schema.
- `migrations/` (repo root, Alembic-managed): `alembic upgrade head` is the only supported way to bring a Service-mode database to the current schema. No silent schema mutation happens at application startup, satisfying the master spec's "No silent schema mutation in service mode" rule.

Lite mode (`skillrewind.persistence.database`, `skillrewind.persistence.repositories`, `Workspace`) is **not touched** by this change. Every existing test, the CLI, and `make demo` run through the exact same code path as before this ADR.

## Consequences
- Two schemas now describe overlapping domain concepts (Lite's SQLite DDL string, Service's SQLAlchemy models). They are kept conceptually aligned by hand for now; a follow-up phase building the API/worker layer (Phase B/C) is where Service mode gets a real consumer and where any schema drift would first be caught by integration tests.
- PostgreSQL itself was not reachable in the environment this ADR was written in — Docker's CLI is installed but its daemon is not running (`docker info` fails to connect to the daemon socket). The Postgres-specific test (`tests/integration/test_service_persistence.py::test_postgres_migration_and_schema_current`) is written to run for real against any reachable PostgreSQL (via `SKILLREWIND_TEST_POSTGRES_URL`) and skips with that exact, printed reason otherwise — it has not been exercised against a live PostgreSQL server as of this commit. SQLite-backed tests exercise the same SQLAlchemy models, engine, and Alembic migration path to catch dialect-portability mistakes early, but SQLite is not a supported Service-mode deployment target (Lite mode already covers single-writer SQLite; Service mode's reason to exist is PostgreSQL's concurrent-writer guarantees, needed by Phase B's multi-worker job claiming).
- There is deliberately no data-migration tool yet that moves an existing Lite-mode SQLite workspace into a Service-mode PostgreSQL database. That is a real, separate piece of work (an ETL across two different schemas, not an Alembic schema migration) and is out of scope for this ADR; it is tracked as a gap in `docs/completion-matrix-v0.3.md`.
- `sqlalchemy`, `alembic`, and `psycopg[binary]` are added as the `service` optional dependency group in `pyproject.toml`, not core dependencies — `pip install skillrewind` (Lite mode / CLI / research use) does not pull in a database driver it does not need.
