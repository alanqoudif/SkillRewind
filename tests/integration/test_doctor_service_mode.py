"""`skillrewind doctor` must fail readiness when Service mode's schema is stale.

Exercises the actual CLI as a subprocess (not the internal function directly)
so this proves the exit-code contract a deployment's readiness probe would
depend on -- see master spec section 6.1 ("readiness failure when schema is
behind") and section 8.1 ("`ready` must verify ... current schema").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


_SKILLREWIND_BIN = str(Path(sys.executable).parent / "skillrewind")


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SKILLREWIND_BIN, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_doctor_lite_mode_reports_service_mode_not_applicable(tmp_path):
    ws = tmp_path / "ws"
    env = dict(os.environ)
    env.pop("SKILLREWIND_MODE", None)
    env.pop("SKILLREWIND_DATABASE_URL", None)
    subprocess.run([_SKILLREWIND_BIN, "--workspace", str(ws), "init"], cwd=REPO_ROOT, env=env, check=True)
    result = _run_cli(["--workspace", str(ws), "doctor"], env)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["checks"]["service_mode"]["applicable"] is False


def test_doctor_service_mode_fails_readiness_before_migrations(tmp_path):
    ws = tmp_path / "ws"
    db_path = tmp_path / "svc.db"
    env = dict(os.environ)
    env.pop("SKILLREWIND_MODE", None)
    env.pop("SKILLREWIND_DATABASE_URL", None)
    subprocess.run([_SKILLREWIND_BIN, "--workspace", str(ws), "init"], cwd=REPO_ROOT, env=env, check=True)

    env["SKILLREWIND_MODE"] = "service"
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    result = _run_cli(["--workspace", str(ws), "doctor"], env)
    report = json.loads(result.stdout)
    assert report["checks"]["service_mode"]["applicable"] is True
    assert report["checks"]["service_mode"]["schema_current"] is False
    assert result.returncode == 1


def test_doctor_service_mode_passes_readiness_after_migrations(tmp_path):
    ws = tmp_path / "ws"
    db_path = tmp_path / "svc.db"
    env = dict(os.environ)
    env.pop("SKILLREWIND_MODE", None)
    env.pop("SKILLREWIND_DATABASE_URL", None)
    subprocess.run([_SKILLREWIND_BIN, "--workspace", str(ws), "init"], cwd=REPO_ROOT, env=env, check=True)

    migrate_env = dict(env)
    migrate_env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=migrate_env,
        check=True,
        capture_output=True,
    )

    env["SKILLREWIND_MODE"] = "service"
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    result = _run_cli(["--workspace", str(ws), "doctor"], env)
    report = json.loads(result.stdout)
    assert report["checks"]["service_mode"]["schema_current"] is True
    assert result.returncode == 0
