#!/usr/bin/env python3
"""Clean-install acceptance smoke test (Phase C2.4 section 11, packaging
follow-up).

Builds a real wheel, installs it into a **fresh temporary virtual
environment** (never the developer's editable checkout), and exercises a
CLI/Lite/Service flow using only what pip installed -- proving SkillRewind
does not silently depend on the source tree being on ``PYTHONPATH``.

This also proves the previously-known packaging gap is closed: Alembic's
migration scripts (``env.py``, ``script.py.mako``, ``versions/*.py``) now
ship as package data inside ``skillrewind.persistence.service.migrations``
(see ``pyproject.toml``'s ``[tool.setuptools.package-data]``), so this
script drives *real* Alembic migration history (``skillrewind db-upgrade``)
rather than the ``create_all()`` schema-bootstrap workaround, and runs the
full ``skillrewind conformance self-test`` (which itself now applies real
migrations via ``skillrewind.persistence.service.migrations_runtime``) --
not just the migration-free ``conformance describe`` subset.

Run via ``make clean-install-smoke`` or directly:
``python tests/smoke/clean_install_smoke.py``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: This repository's own dev environment is built with `uv` (see Makefile:
#: `uv venv` / `uv pip install`), which does not necessarily install `pip`
#: into the resulting venv (`.venv/bin/python -m pip` can be absent). `uv`
#: itself is required on PATH to run this smoke test -- it is what actually
#: builds the wheel (`uv build`) and creates+populates the fresh venv
#: (`uv venv` / `uv pip install`), giving an equally real, isolated,
#: from-scratch installation without assuming pip is bundled anywhere.
UV = shutil.which("uv")
if UV is None:
    raise SystemExit("clean-install-smoke requires `uv` on PATH (https://docs.astral.sh/uv/)")

#: Files that MUST be present inside the built wheel for a pip-installed
#: deployment to run real Alembic migrations without a GitHub checkout.
REQUIRED_MIGRATION_MEMBERS = (
    "skillrewind/persistence/service/migrations/env.py",
    "skillrewind/persistence/service/migrations/script.py.mako",
    "skillrewind/persistence/service/migrations/versions/3d6450d37a15_initial_service_schema.py",
    "skillrewind/persistence/service/migrations/versions/b29242c5289a_derivation_lineage_evidence_and_.py",
    "skillrewind/persistence/service/migrations/versions/de2e581d5efc_service_replay_evidence_and_promotion.py",
)


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="skillrewind-clean-install-"))
    dist_dir = work / "dist"
    venv_dir = work / "venv"
    print(f"working directory: {work}")

    # -- 1: build a real wheel from the repository --
    _run([UV, "build", "--wheel", str(REPO_ROOT), "--out-dir", str(dist_dir)])
    wheels = list(dist_dir.glob("skillrewind-*.whl"))
    assert wheels, "expected exactly one built wheel"
    wheel_path = wheels[0]
    print(f"built wheel: {wheel_path.name}")

    # -- 2: inspect the wheel contents -- fail loudly, before installing
    # anything, if the migration scripts are not packaged as data files --
    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())
    missing = [m for m in REQUIRED_MIGRATION_MEMBERS if m not in names]
    assert not missing, f"wheel is missing required migration files: {missing}"
    print(f"wheel contains all {len(REQUIRED_MIGRATION_MEMBERS)} required migration files")

    # -- 3: install it into a fresh temporary virtual environment, with the
    # Service-mode dependencies -- --
    _run([UV, "venv", "--python", sys.executable, str(venv_dir)])
    venv_python = venv_dir / "bin" / "python"
    _run([UV, "pip", "install", "--python", str(venv_python), f"{wheel_path}[service]"])

    # No PYTHONPATH pointing at the repo checkout, and cwd is NOT the repo
    # -- this is the actual "not just inside the source tree" proof.
    run_dir = work / "run"
    run_dir.mkdir()

    # -- 4: import skillrewind --
    import_result = _run(
        [str(venv_python), "-c", "import skillrewind; print('skillrewind imported OK, file:', skillrewind.__file__)"],
        cwd=run_dir,
    )
    assert str(REPO_ROOT) not in import_result.stdout, "imported skillrewind from the repository checkout, not the installed wheel"

    # -- 5: run CLI help --
    venv_skillrewind = venv_dir / "bin" / "skillrewind"
    _run([str(venv_skillrewind), "--help"], cwd=run_dir)

    # -- 6: initialize a fresh Lite workspace --
    lite_ws = run_dir / "lite-ws"
    _run([str(venv_skillrewind), "--workspace", str(lite_ws), "init"], cwd=run_dir)
    assert lite_ws.is_dir(), "Lite workspace directory was not created"

    sample_file = run_dir / "sample-skill.txt"
    sample_file.write_text("a minimal clean-install-smoke artifact\n")
    _run(
        [
            str(venv_skillrewind), "--workspace", str(lite_ws), "artifact-ingest",
            "--file", str(sample_file), "--kind", "agent-skill", "--name", "smoke-artifact",
        ],
        cwd=run_dir,
    )
    list_result = _run([str(venv_skillrewind), "--workspace", str(lite_ws), "artifact-list"], cwd=run_dir)
    assert "smoke-artifact" in list_result.stdout, "ingested artifact not visible in artifact-list"

    # -- 7: initialize a fresh Service-mode database (just a path -- the
    # file does not exist yet; `db-upgrade` creates it) --
    service_db = run_dir / "service.db"
    service_db_url = f"sqlite:///{service_db}"
    assert not service_db.exists()

    # -- 8: run the REAL Alembic migration path from the installed package
    # -- no alembic.ini, no migrations/ directory, no repo checkout --
    _run([str(venv_skillrewind), "db-upgrade", "--database-url", service_db_url], cwd=run_dir)
    assert service_db.exists(), "Service-mode SQLite database was not created by db-upgrade"

    # -- 9: verify schema-current / readiness, again purely via the
    # installed package's `skillrewind db-current` --
    current_result = _run([str(venv_skillrewind), "db-current", "--database-url", service_db_url], cwd=run_dir)
    assert "schema at head" in current_result.stdout, current_result.stdout

    # -- 10-13: instantiate the API from the installed package, create a
    # bootstrap API key, and exercise a minimal stable-v1 API flow,
    # exercising the worker (step 11) via the durable job queue --
    api_flow_script = f"""
import skillrewind.jobs.handlers  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.queue import JobQueue
from skillrewind.jobs.worker import Worker
from skillrewind.persistence.service.engine import build_engine

config = SkillRewindConfig(mode="service", database_url="{service_db_url}", cas_root="{run_dir / "cas"}")
engine = build_engine(config.database_url)
with Session(engine) as session:
    key = create_api_key(session, name="smoke", actor="clean-install-smoke", scopes=["ingest", "read"]).plaintext

app = create_app(config)
with TestClient(app) as client:
    r = client.post(
        "/api/v1/artifacts", params={{"kind": "agent-skill", "logical_name": "smoke"}},
        headers={{"Authorization": f"Bearer {{key}}"}}, content=b"clean install smoke content",
    )
    assert r.status_code == 201, r.text
    artifact_id = r.json()["artifact_id"]
    r2 = client.get(f"/api/v1/artifacts/{{artifact_id}}", headers={{"Authorization": f"Bearer {{key}}"}})
    assert r2.status_code == 200, r2.text
    print("minimal stable-v1 API flow OK:", artifact_id)

# -- 11: exercise the durable job worker from the installed package (no
# pending jobs is a valid, correct outcome -- proves it constructs and
# runs against the migrated schema) --
worker = Worker(JobQueue(engine), worker_id="clean-install-smoke-worker")
worker.run_once()
print("worker instantiated and ran against installed-package schema OK")
"""
    _run([str(venv_python), "-c", api_flow_script], cwd=run_dir)

    # -- 14: run the FULL conformance self-test from the installed package
    # -- this applies real Alembic migrations internally and exercises
    # ingest/derivation/lineage/candidate-recovery/replay/revocation/
    # rebuild/verification/attestation/SSE end to end --
    self_test_result = _run([str(venv_skillrewind), "conformance", "self-test"], cwd=run_dir)
    assert '"ok": true' in self_test_result.stdout, f"conformance self-test reported failures:\n{self_test_result.stdout}"

    # -- 15: verify no repository files were accessed accidentally: the
    # packaged migrations resolve to a path inside the venv's site-packages,
    # never inside this repository checkout --
    migrations_probe = _run(
        [
            str(venv_python), "-c",
            "from skillrewind.persistence.service.migrations_runtime import migrations_root\n"
            "with migrations_root() as p:\n"
            "    print(p)\n",
        ],
        cwd=run_dir,
    )
    resolved_migrations_path = migrations_probe.stdout.strip()
    assert str(REPO_ROOT) not in resolved_migrations_path, (
        f"packaged migrations resolved inside the repository checkout ({resolved_migrations_path}) "
        "-- this defeats the point of packaging them"
    )
    assert str(venv_dir) in resolved_migrations_path, (
        f"packaged migrations did not resolve inside the fresh venv ({resolved_migrations_path})"
    )
    print(f"migrations resolved from installed package only: {resolved_migrations_path}")

    print("\nclean-install smoke test PASSED")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
