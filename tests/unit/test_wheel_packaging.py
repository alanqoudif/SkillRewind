"""Regression test for the packaging fix in this milestone: Alembic's
migration scripts must ship inside the built wheel as package data (see
``pyproject.toml``'s ``[tool.setuptools.package-data]``) and must be usable
from a real ``pip install``, not just an editable checkout.

This is a smaller, faster sibling of ``tests/smoke/clean_install_smoke.py``
(which additionally runs the full conformance self-test end to end via
``make clean-install-smoke``); this test focuses narrowly on: build a real
wheel, assert the migration files are inside it, install it into a fresh
venv, and prove ``skillrewind db-upgrade`` applies real Alembic history
using only what pip installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

UV = shutil.which("uv")

REQUIRED_MIGRATION_MEMBERS = (
    "skillrewind/persistence/service/migrations/env.py",
    "skillrewind/persistence/service/migrations/script.py.mako",
    "skillrewind/persistence/service/migrations/versions/3d6450d37a15_initial_service_schema.py",
    "skillrewind/persistence/service/migrations/versions/b29242c5289a_derivation_lineage_evidence_and_.py",
    "skillrewind/persistence/service/migrations/versions/de2e581d5efc_service_replay_evidence_and_promotion.py",
)


@pytest.mark.skipif(UV is None, reason="requires `uv` on PATH to build a real wheel")
def test_built_wheel_contains_migration_files(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    subprocess.run(
        [UV, "build", "--wheel", str(REPO_ROOT), "--out-dir", str(dist_dir)],
        check=True, capture_output=True, text=True,
    )
    wheels = list(dist_dir.glob("skillrewind-*.whl"))
    assert wheels, "expected exactly one built wheel"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
    missing = [m for m in REQUIRED_MIGRATION_MEMBERS if m not in names]
    assert not missing, f"wheel is missing required migration files: {missing}"


@pytest.mark.skipif(UV is None, reason="requires `uv` on PATH to build+install a real wheel")
def test_installed_wheel_applies_real_migrations_with_no_repo_checkout(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    venv_dir = tmp_path / "venv"
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    subprocess.run(
        [UV, "build", "--wheel", str(REPO_ROOT), "--out-dir", str(dist_dir)],
        check=True, capture_output=True, text=True,
    )
    wheel_path = next(dist_dir.glob("skillrewind-*.whl"))

    subprocess.run([UV, "venv", "--python", sys.executable, str(venv_dir)], check=True, capture_output=True, text=True)
    venv_python = venv_dir / "bin" / "python"
    subprocess.run(
        [UV, "pip", "install", "--python", str(venv_python), f"{wheel_path}[service]"],
        check=True, capture_output=True, text=True,
    )

    service_db = run_dir / "service.db"
    venv_skillrewind = venv_dir / "bin" / "skillrewind"
    upgrade = subprocess.run(
        [str(venv_skillrewind), "db-upgrade", "--database-url", f"sqlite:///{service_db}"],
        cwd=run_dir, capture_output=True, text=True,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert service_db.exists(), "db-upgrade did not create the Service-mode database"

    current = subprocess.run(
        [str(venv_skillrewind), "db-current", "--database-url", f"sqlite:///{service_db}"],
        cwd=run_dir, capture_output=True, text=True,
    )
    assert current.returncode == 0, current.stderr
    assert "schema at head" in current.stdout

    probe = subprocess.run(
        [
            str(venv_python), "-c",
            "from skillrewind.persistence.service.migrations_runtime import migrations_root\n"
            "with migrations_root() as p:\n"
            "    print(p)\n",
        ],
        cwd=run_dir, check=True, capture_output=True, text=True,
    )
    resolved = probe.stdout.strip()
    assert str(REPO_ROOT) not in resolved, f"migrations resolved inside the repository checkout: {resolved}"
    assert str(venv_dir) in resolved, f"migrations did not resolve inside the installed venv: {resolved}"
