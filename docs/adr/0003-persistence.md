# ADR 0003: SQLite-lite persistence, no PostgreSQL/SQLAlchemy in this release

## Status
Accepted (v0.2.0); PostgreSQL service mode deferred

## Context
The specification this repository targets describes two deployment modes: "lite" (SQLite, single machine) and "service" (PostgreSQL, workers, API). Building both well in one session, with a full ORM/migration framework, would have meant shipping an untested and likely broken service-mode path.

## Decision
Implement only "lite mode": the standard library `sqlite3` module in WAL mode, accessed through a thin repository layer (`skillrewind.persistence.repositories`) over a hand-written, additive-only schema (`skillrewind.persistence.database`). No SQLAlchemy, no Alembic. Repository methods commit individually rather than batching multi-statement transactions; this is documented as a known limitation (see `revocation.barrier.apply_barrier`'s docstring) rather than a false claim of atomicity, mitigated by every write being idempotent so a crash mid-sequence is safely retryable.

## Consequences
- Service-mode PostgreSQL deployment, connection pooling, and a durable job queue are not implemented (see `STATUS.md`).
- If/when service mode is built, the repository-layer interface (`ArtifactRepository`, `EdgeRepository`, etc.) is the intended seam to swap in a SQLAlchemy+PostgreSQL implementation without touching the domain/service layers above it, which depend only on the repository classes' method signatures, not on SQLite specifically.
- Rule followed: "Simple documented infrastructure over unnecessary distributed complexity. SQLite/local CAS first, PostgreSQL/workers second."
