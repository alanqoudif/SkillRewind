"""Service-mode (SQLAlchemy + Alembic) persistence tests.

These exercise the new, additive Service-mode schema in
``skillrewind.persistence.service`` -- see ``docs/adr/0009-service-mode-persistence.md``.
They do not touch Lite mode's raw-``sqlite3`` layer at all.

PostgreSQL-specific behavior (concurrent job claiming, etc.) is gated on a
reachable ``DATABASE_URL``/local Postgres and skips with an explicit reason
when unavailable, per the "no fabricated pass" rule in the master spec. As of
this commit, Docker is present but its daemon is not running in this
environment (``docker info`` fails with a socket-connect error), so those
tests are expected to skip here; they will run for real wherever Postgres is
reachable, including in CI once a `postgres:` service container is wired up.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from skillrewind.persistence.service.engine import build_engine, create_all, schema_current
from skillrewind.persistence.service.models import Artifact, Base, Job

REPO_ROOT = Path(__file__).resolve().parents[2]


def _postgres_url() -> str | None:
    return os.environ.get("SKILLREWIND_TEST_POSTGRES_URL")


def test_fresh_sqlite_create_all_produces_full_schema(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = build_engine(f"sqlite:///{db_path}")
    create_all(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {
        "artifacts",
        "alias_history",
        "derivations",
        "edges",
        "candidate_scores",
        "replay_records",
        "revocation_events",
        "revocation_transitions",
        "quarantine",
        "waivers",
        "rebuild_plans",
        "rebuild_attempts",
        "verification_reports",
        "attestations",
        "audit_events",
        "api_keys",
        "idempotency_records",
        "jobs",
        "job_events",
        "benchmark_runs",
        "installations",
    }
    assert expected.issubset(tables)


def test_expected_indexes_present_on_hot_query_columns(tmp_path):
    db_path = tmp_path / "idx.db"
    engine = build_engine(f"sqlite:///{db_path}")
    create_all(engine)
    inspector = inspect(engine)

    def index_columns(table: str) -> set[str]:
        cols: set[str] = set()
        for ix in inspector.get_indexes(table):
            cols.update(ix["column_names"])
        return cols

    assert "digest_hex" in index_columns("artifacts")
    assert "status" in index_columns("artifacts")
    assert "evidence_class" in index_columns("edges")
    assert "status" in index_columns("jobs") or {"status", "priority"} & index_columns("jobs")
    assert "lease_expires_at" in index_columns("jobs")


def test_alembic_upgrade_head_on_fresh_database(tmp_path):
    """Real migration run via the `alembic` CLI against a throwaway SQLite DB."""

    db_path = tmp_path / "migrated.db"
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = build_engine(f"sqlite:///{db_path}")
    is_current, detail = schema_current(engine)
    assert is_current, detail


def test_schema_current_false_before_migrations_and_true_after(tmp_path):
    db_path = tmp_path / "not_migrated.db"
    engine = build_engine(f"sqlite:///{db_path}")
    # No alembic_version table yet -- nothing has been migrated.
    is_current, detail = schema_current(engine)
    assert is_current is False
    assert "alembic_version" in detail

    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    is_current, detail = schema_current(engine)
    assert is_current is True, detail


def test_transaction_rollback_leaves_no_partial_row(tmp_path):
    db_path = tmp_path / "txn.db"
    engine = build_engine(f"sqlite:///{db_path}")
    create_all(engine)

    from sqlalchemy.orm import Session

    with pytest.raises(RuntimeError):
        with Session(engine) as session:
            session.add(
                Artifact(
                    artifact_id="skill://x@sha256:" + "0" * 64,
                    digest_hex="0" * 64,
                    kind="agent-skill",
                    logical_name="x",
                    mime_type="text/plain",
                    byte_size=1,
                    storage_ref="cas://0",
                )
            )
            session.flush()
            raise RuntimeError("simulated failure before commit")

    with Session(engine) as session:
        count = session.query(Artifact).count()
    assert count == 0


def test_job_row_round_trip(tmp_path):
    from sqlalchemy.orm import Session

    db_path = tmp_path / "jobs.db"
    engine = build_engine(f"sqlite:///{db_path}")
    create_all(engine)

    with Session(engine) as session:
        job = Job(job_id="job-1", kind="revocation.progress", payload_json={"revocation_event_id": "rev-1"})
        session.add(job)
        session.commit()

    with Session(engine) as session:
        fetched = session.get(Job, "job-1")
        assert fetched is not None
        assert fetched.status == "queued"
        assert fetched.payload_json == {"revocation_event_id": "rev-1"}


@pytest.mark.skipif(
    _postgres_url() is None,
    reason=(
        "No reachable PostgreSQL configured via SKILLREWIND_TEST_POSTGRES_URL. "
        "Docker CLI is present in this environment but its daemon is not running "
        "(`docker info` fails to connect to the socket), so a throwaway Postgres "
        "container could not be started here. This test runs for real in any "
        "environment with SKILLREWIND_TEST_POSTGRES_URL pointing at a reachable "
        "PostgreSQL instance, e.g. `postgresql+psycopg://user:pass@localhost:5432/skillrewind_test`."
    ),
)
def test_postgres_migration_and_schema_current():
    url = _postgres_url()
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    engine = build_engine(url)
    is_current, detail = schema_current(engine)
    assert is_current, detail
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def test_docker_daemon_status_is_honestly_recorded():
    """Documents the exact environment constraint rather than silently skipping.

    This is not a functional assertion about SkillRewind; it is a recorded,
    reproducible check of *why* the Postgres-gated test above skips in this
    environment, per the master spec's "blocked-by-environment" requirement.
    """

    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed in this environment")
    result = subprocess.run(["docker", "info"], capture_output=True, text=True)
    # Recorded for the test log; not asserted on, since daemon availability is
    # environment state, not something this test suite controls.
    print(f"docker info exit={result.returncode} stderr={result.stderr.strip()[:200]}")
