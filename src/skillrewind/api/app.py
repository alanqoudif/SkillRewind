"""FastAPI application factory for SkillRewind Service mode (Phase C/C2, partial).

Endpoints backed by real, tested state: health/readiness, API-key
administration, job management, RewindBench-run submission through the real
job queue, and now artifact ingest/retrieval backed by a real SQLAlchemy
repository + CAS (Phase C2). `revocation.execute` exists as a job handler
(see `skillrewind.jobs.handlers`) but has no API endpoint yet.
Lineage/candidate/replay/rebuild/verification/attestation endpoints from the
master spec's section 8.2 are NOT implemented here yet -- see
docs/completion-matrix-v0.3.md for what remains.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..cas.local import LocalCAS
from ..config import SkillRewindConfig, load_config
from ..persistence.service.engine import build_engine
from .ratelimit import RateLimiter
from .routers import (
    admin,
    artifacts,
    attestations,
    bench,
    candidates,
    derivations,
    events,
    health,
    jobs,
    lineage,
    quarantine,
    replay,
    revocations,
    waivers,
)


def create_app(config: SkillRewindConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    if not cfg.database_url:
        raise ValueError("Service-mode API requires database_url to be configured")

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        app.state.engine.dispose()

    app = FastAPI(title="SkillRewind API", version="v1", lifespan=_lifespan)
    app.state.config = cfg
    app.state.engine = build_engine(cfg.database_url)
    app.state.cas = LocalCAS(cfg.resolved_cas_root, max_object_bytes=cfg.max_object_bytes)
    app.state.rate_limiter = RateLimiter(
        capacity=cfg.rate_limit_capacity, refill_per_second=cfg.rate_limit_refill_per_second
    )

    origins = [o.strip() for o in cfg.cors_allow_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
        )

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next):
        limiter: RateLimiter = request.app.state.rate_limiter
        identity = request.headers.get("authorization") or (request.client.host if request.client else "anonymous")
        allowed, retry_after = limiter.allow(identity)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"type": "about:blank", "title": "Too Many Requests", "status": 429, "detail": "rate limit exceeded"},
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        return await call_next(request)

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(jobs.router)
    app.include_router(bench.router)
    app.include_router(events.router)
    # derivations.router owns more specific `/artifacts/{id}/parents` and
    # `/artifacts/{id}/children` suffix routes under the same
    # `/api/v1/artifacts` prefix as artifacts.router's catch-all
    # `/{artifact_id:path}` route; it must be registered first so Starlette
    # (which matches in registration order) tries the specific suffix routes
    # before the greedy catch-all swallows the suffix into `artifact_id`.
    app.include_router(derivations.router)
    # quarantine.router and waivers.router both add more specific
    # `/artifacts/{id}/quarantine` and `/artifacts/{id}/waivers` suffix
    # routes under the same prefix artifacts.router's catch-all
    # `/{artifact_id:path}` route also matches -- register them first for the
    # same reason derivations.router is registered before artifacts.router
    # above (Starlette matches in registration order).
    app.include_router(quarantine.router)
    app.include_router(waivers.router)
    app.include_router(artifacts.router)
    app.include_router(lineage.router)
    app.include_router(candidates.router)
    app.include_router(replay.router)
    app.include_router(revocations.router)
    app.include_router(attestations.router)

    @app.exception_handler(StarletteHTTPException)
    async def _problem_detail_handler(request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            body = detail
        else:
            body = {"type": "about:blank", "title": "Error", "status": exc.status_code, "detail": str(detail)}
        headers = getattr(exc, "headers", None)
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "type": "about:blank",
                "title": "Unprocessable Entity",
                "status": 422,
                "detail": "request validation failed",
                "errors": exc.errors(),
            },
        )

    return app
