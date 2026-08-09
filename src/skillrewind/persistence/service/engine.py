"""Engine construction and schema-currency checks for Service mode.

Supports two ``database_url`` shapes:

- ``postgresql+psycopg://user:pass@host:port/dbname`` (real Service mode)
- ``sqlite:///path/to/file.db`` or ``sqlite://`` (in-memory) — used by this
  repository's own tests, since no live PostgreSQL server is available in
  this environment (see ``docs/completion-matrix-v0.3.md``). SQLite is not
  a supported Service-mode deployment target on its own (Lite mode already
  covers single-writer SQLite deployments); it is exercised here purely to
  prove the schema/migrations/ORM layer is dialect-portable before a real
  PostgreSQL server is reachable.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect, text

from .models import Base

SCHEMA_VERSION = "0.3.0"


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    if not database_url:
        raise ValueError("database_url must be non-empty for Service mode")
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, echo=echo, future=True, connect_args=connect_args)
    return engine


def create_all(engine: Engine) -> None:
    """Create all tables directly from the ORM metadata.

    Used by tests and by first-time local/dev bootstrap. Real deployments
    should prefer ``alembic upgrade head`` (see ``migrations/``) so the
    ``alembic_version`` table is populated and ``schema_current`` can verify
    the deployment is not behind head.
    """

    Base.metadata.create_all(engine)


def schema_current(engine: Engine) -> tuple[bool, str]:
    """Return ``(is_current, detail)``.

    ``is_current`` is True only if an ``alembic_version`` table exists and its
    single row matches the repository's current Alembic head revision. This
    is what Service-mode readiness (``/health/ready``) and ``skillrewind
    doctor`` must check before serving mutating traffic — see spec section
    6.1 ("readiness failure when schema is behind").
    """

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return False, "alembic_version table not present -- migrations have never been applied"

    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    current_rev = row[0] if row else None

    cfg = Config()
    cfg.set_main_option("script_location", str(_migrations_dir()))
    script = ScriptDirectory.from_config(cfg)
    head_rev = script.get_current_head()

    if current_rev == head_rev:
        return True, f"schema at head ({head_rev})"
    return False, f"schema at {current_rev!r}, head is {head_rev!r} -- run migrations"


def _migrations_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[3].parent / "migrations"
