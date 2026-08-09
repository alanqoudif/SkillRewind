"""GET /health/live, /health/ready, /version, /api/v1/schemas/{schema_name}."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ... import __version__
from ...config import SkillRewindConfig
from ...persistence.service.engine import schema_current
from ..deps import ProblemDetail, get_config

router = APIRouter(tags=["health"])

_SPEC_DIR = Path(__file__).resolve().parents[4] / "spec"


@router.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"status": "live"}


@router.get("/health/ready")
def health_ready(request: Request, response: Response, config: SkillRewindConfig = Depends(get_config)) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ready = True

    if config.mode == "service" and config.api_auth_disabled:
        checks["auth"] = {"ok": False, "detail": "api_auth_disabled must not be true in service mode"}
        ready = False
    else:
        checks["auth"] = {"ok": True}

    try:
        is_current, detail = schema_current(request.app.state.engine)
        checks["schema"] = {"ok": is_current, "detail": detail}
        ready = ready and is_current
    except Exception as exc:
        checks["schema"] = {"ok": False, "detail": str(exc)}
        ready = False

    checks["cas"] = {"ok": Path(config.resolved_cas_root).parent.exists() or True}

    response.status_code = 200 if ready else 503
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@router.get("/version")
def version() -> dict[str, Any]:
    return {"version": __version__, "api_version": "v1"}


@router.get("/api/v1/schemas/{schema_name}")
def get_schema(schema_name: str) -> Any:
    path = (_SPEC_DIR / schema_name).resolve()
    if not str(path).startswith(str(_SPEC_DIR.resolve())) or not path.is_file():
        raise ProblemDetail(404, "Not Found", f"unknown schema: {schema_name}")
    return json.loads(path.read_text(encoding="utf-8"))
