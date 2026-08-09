"""Resolve and drive the packaged Alembic migrations without ever assuming
a repository checkout is present on disk.

The migration scripts (``env.py``, ``script.py.mako``, ``versions/*.py``)
ship as package data inside ``skillrewind.persistence.service.migrations``
(see ``pyproject.toml``'s ``[tool.setuptools.package-data]``), so a
``pip install skillrewind[service]`` deployment can run real Alembic
migration history -- not just ``create_all()`` -- with no separate GitHub
checkout. ``importlib.resources`` is used instead of a repository-relative
filesystem path so this also works if the package is ever served from a
zip/egg (``importlib.resources.as_file`` extracts to a real directory on
disk for the duration of the ``with`` block in that case).
"""

from __future__ import annotations

from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

from alembic.config import Config


@contextmanager
def migrations_root() -> Iterator[Path]:
    """Yield a real filesystem directory containing the packaged migrations."""

    anchor = resources.files("skillrewind.persistence.service") / "migrations"
    with resources.as_file(anchor) as path:
        yield path


@contextmanager
def alembic_config(database_url: str) -> Iterator[Config]:
    """Yield an Alembic ``Config`` pointed at the packaged migrations, with
    no dependency on an ``alembic.ini`` file existing on disk."""

    with migrations_root() as root:
        cfg = Config()
        cfg.set_main_option("script_location", str(root))
        cfg.set_main_option("sqlalchemy.url", database_url)
        yield cfg


def upgrade_to_head(database_url: str, *, revision: str = "head") -> None:
    from alembic import command

    with alembic_config(database_url) as cfg:
        command.upgrade(cfg, revision)


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    with migrations_root() as root:
        cfg = Config()
        cfg.set_main_option("script_location", str(root))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        if head is None:
            raise RuntimeError("no Alembic head revision found in packaged migrations")
        return head
