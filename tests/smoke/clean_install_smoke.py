#!/usr/bin/env python3
"""Phase C2.4 section 11: clean-install acceptance smoke test.

Builds a real wheel, installs it into a **fresh temporary virtual
environment** (never the developer's editable checkout), and exercises a
CLI/Lite/Service flow using only what pip installed -- proving SkillRewind
does not silently depend on the source tree being on ``PYTHONPATH``.

Known, documented gap (see ``docs/release-readiness-v0.3.md``): Alembic's
migration scripts (``migrations/``, ``alembic.ini``) live at the repository
root, not inside the ``skillrewind`` package, so they are **not** included
in the wheel built here. This script therefore uses
``skillrewind.persistence.service.engine.create_all()`` (the documented
"first-time local/dev bootstrap" path, not real Alembic migrations) to
stand up the Service-mode schema for its API-flow step. A pip-installed
deployment that needs real Alembic migration history (not just the current
schema) must still check out the migrations directory separately today --
packaging it into the wheel is future work, not solved by this milestone.

Run via ``make clean-install-smoke`` or directly:
``python tests/smoke/clean_install_smoke.py``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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

    # -- 2: install it into a fresh temporary virtual environment --
    _run([UV, "venv", "--python", sys.executable, str(venv_dir)])
    venv_python = venv_dir / "bin" / "python"
    _run([UV, "pip", "install", "--python", str(venv_python), f"{wheel_path}[service]"])

    # No PYTHONPATH pointing at the repo checkout, and cwd is NOT the repo
    # -- this is the actual "not just inside the source tree" proof.
    run_dir = work / "run"
    run_dir.mkdir()

    # -- 3: import skillrewind --
    _run([str(venv_python), "-c", "import skillrewind; print('skillrewind imported OK, file:', skillrewind.__file__)"], cwd=run_dir)

    # -- 4: run CLI help --
    venv_skillrewind = venv_dir / "bin" / "skillrewind"
    _run([str(venv_skillrewind), "--help"], cwd=run_dir)

    # -- 5: initialize a fresh Lite workspace --
    lite_ws = run_dir / "lite-ws"
    _run([str(venv_skillrewind), "--workspace", str(lite_ws), "init"], cwd=run_dir)
    assert lite_ws.is_dir(), "Lite workspace directory was not created"

    # -- 6: run a minimal Lite command --
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

    # -- 7-8: instantiate Service mode against a fresh SQLite service DB and
    # apply the current schema (see the module docstring for why this uses
    # create_all() rather than real Alembic migration history) --
    service_db = run_dir / "service.db"
    bootstrap_script = (
        "from skillrewind.persistence.service.engine import build_engine, create_all\n"
        f"engine = build_engine('sqlite:///{service_db}')\n"
        "create_all(engine)\n"
        "print('service schema created OK')\n"
    )
    _run([str(venv_python), "-c", bootstrap_script], cwd=run_dir)
    assert service_db.exists(), "Service-mode SQLite database was not created"

    # -- 9-10: create an API key and exercise a minimal stable-v1 API flow,
    # all through the installed package only --
    api_flow_script = f"""
import skillrewind.jobs.handlers  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.persistence.service.engine import build_engine

config = SkillRewindConfig(mode="service", database_url="sqlite:///{service_db}", cas_root="{run_dir / "cas"}")
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
"""
    _run([str(venv_python), "-c", api_flow_script], cwd=run_dir)

    # -- 11: conformance describe as a reduced smoke profile (full
    # self-test needs the packaged migrations directory -- see module
    # docstring; describe() is pure Python and always available) --
    _run([str(venv_skillrewind), "conformance", "describe"], cwd=run_dir)

    print("\nclean-install smoke test PASSED")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
