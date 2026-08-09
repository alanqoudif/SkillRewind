"""Service-mode persistence: SQLAlchemy models, engine, and Alembic-driven migrations.

This package is additive. It does not replace ``skillrewind.persistence.database``
(the SQLite/raw-``sqlite3`` Lite-mode layer that the CLI and the deterministic
demo depend on) — see ``docs/adr/0009-service-mode-persistence.md`` for why. It
exists to back the not-yet-built Service-mode API/worker (durable jobs,
PostgreSQL, API keys, idempotency records) with a real, migration-versioned
schema rather than another hand-rolled DDL string.
"""

from .engine import build_engine, create_all, schema_current
from .models import Base

__all__ = ["Base", "build_engine", "create_all", "schema_current"]
