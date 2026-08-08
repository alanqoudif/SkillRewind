# ADR 0004: Database-backed durable jobs deferred

## Status
Deferred — not implemented in v0.2.0

## Context
The specification calls for a database-backed durable job queue (lease claiming, heartbeats, retries, `FOR UPDATE SKIP LOCKED` on PostgreSQL, a single-worker SQLite fallback) so replay/rebuild/verification/attestation work does not block an API request process.

## Decision
Not implemented in this release. All work in v0.2.0 (`revocation.service.run_revocation`, replay, rebuild, attestation generation) executes synchronously in the calling process — CLI command or library call. A `jobs` table exists in the SQLite schema (`skillrewind.persistence.database`) as a forward-compatible placeholder, but nothing writes to or reads from it yet. The CLI's `serve`/`worker` commands print an explicit "not implemented in this session" message and exit non-zero rather than silently no-op.

## Consequences
This is acceptable for the current CLI/library-only vertical slice, where nothing needs to survive a process restart mid-operation. It becomes a hard requirement before any HTTP API is built, since request handlers must not block on multi-second replay/rebuild work. The `jobs` table schema is a starting point for that future work but has not been validated end-to-end.
