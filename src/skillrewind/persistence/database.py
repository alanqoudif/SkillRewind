"""SQLite persistence for lite mode.

This session implements the "Lite mode" persistence target from the spec
(SQLite, WAL mode, local filesystem CAS, single worker) using the standard
library ``sqlite3`` module directly rather than SQLAlchemy + Alembic. That is
a deliberate scope reduction — see ``docs/adr/0003-persistence.md`` — made
because service-mode PostgreSQL/worker deployment is not implemented in this
session; introducing a full ORM/migration framework for a single supported
backend and no live schema evolution would be premature.

The schema below is versioned by ``schema_meta.schema_version`` and is
additive-only across this project's 0.2.x line to date.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "0.2.0"

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    digest_hex TEXT NOT NULL,
    kind TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    creator TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT NOT NULL DEFAULT '0.2',
    supersedes TEXT,
    superseded_by TEXT,
    alias TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_digest ON artifacts(digest_hex);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
CREATE INDEX IF NOT EXISTS idx_artifacts_logical_name ON artifacts(logical_name);
CREATE INDEX IF NOT EXISTS idx_artifacts_alias ON artifacts(alias);

CREATE TABLE IF NOT EXISTS derivations (
    derivation_id TEXT PRIMARY KEY,
    recipe TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    target_artifact_id TEXT,
    payload_json TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_derivations_target ON derivations(target_artifact_id);
CREATE INDEX IF NOT EXISTS idx_derivations_started ON derivations(started_at);

CREATE TABLE IF NOT EXISTS edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    relation TEXT NOT NULL,
    evidence_class TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT,
    scorer_version TEXT,
    invalidation_reason TEXT,
    PRIMARY KEY (source, target, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_evidence ON edges(evidence_class);
CREATE INDEX IF NOT EXISTS idx_edges_status ON edges(status);

CREATE TABLE IF NOT EXISTS replay_records (
    replay_id TEXT PRIMARY KEY,
    target_derivation_id TEXT NOT NULL,
    candidate_ancestor_id TEXT NOT NULL,
    intervention_kind TEXT NOT NULL,
    verdict TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_replay_target ON replay_records(target_derivation_id);
CREATE INDEX IF NOT EXISTS idx_replay_candidate ON replay_records(candidate_ancestor_id);

CREATE TABLE IF NOT EXISTS revocation_events (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    policy TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_revocation_state ON revocation_events(state);
CREATE INDEX IF NOT EXISTS idx_revocation_created ON revocation_events(created_at);

CREATE TABLE IF NOT EXISTS revocation_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_event ON revocation_transitions(event_id);

CREATE TABLE IF NOT EXISTS quarantine (
    artifact_id TEXT PRIMARY KEY,
    revocation_event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waivers (
    waiver_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revocation_event_id TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_waivers_artifact ON waivers(artifact_id);

CREATE TABLE IF NOT EXISTS attestations (
    attestation_id TEXT PRIMARY KEY,
    event_id TEXT,
    content_digest TEXT NOT NULL,
    signature_json TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT,
    lease_expires_at TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_class TEXT,
    error_message TEXT,
    result_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection in WAL mode with the SkillRewind schema applied."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
        )
        conn.commit()
    return conn


def connect_memory() -> sqlite3.Connection:
    """Open an in-memory SQLite connection with the schema applied (for tests)."""

    conn = sqlite3.connect(":memory:")
    conn.executescript(_DDL)
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def get_schema_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return row[0] if row else None
