"""FastAPI dependencies: config, DB session, auth, rate limiting.

All shared server state (engine, rate limiter) lives on `app.state`, set up
once in `create_app()` -- not module globals, so multiple app instances
(e.g. one per test) never share state.
"""

from __future__ import annotations

import uuid
from typing import Iterator, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ..cas.base import ContentAddressedStore
from ..config import SkillRewindConfig
from .auth import AuthenticatedKey, authenticate


def get_config(request: Request) -> SkillRewindConfig:
    return request.app.state.config


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_cas(request: Request) -> ContentAddressedStore:
    return request.app.state.cas


def get_session(request: Request) -> Iterator[Session]:
    session = Session(request.app.state.engine)
    try:
        yield session
    finally:
        session.close()


def get_trace_id(request: Request) -> str:
    existing = request.headers.get("x-trace-id")
    return existing or str(uuid.uuid4())


class ProblemDetail(HTTPException):
    """RFC 9457-shaped error envelope, per master spec 8.3."""

    def __init__(self, status_code: int, title: str, detail: str, type_: str = "about:blank") -> None:
        super().__init__(status_code=status_code, detail={"type": type_, "title": title, "status": status_code, "detail": detail})


def current_key(
    request: Request,
    config: SkillRewindConfig = Depends(get_config),
    session: Session = Depends(get_session),
) -> Optional[AuthenticatedKey]:
    """Returns the authenticated key, or None if auth is disabled (Lite/dev
    mode only -- see require_scope, which is the actual enforcement point)."""

    if config.api_auth_disabled:
        return None
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ProblemDetail(401, "Unauthorized", "missing or malformed Authorization: Bearer <key> header")
    plaintext = header[7:].strip()
    key = authenticate(session, plaintext)
    if key is None:
        raise ProblemDetail(401, "Unauthorized", "invalid, revoked, or expired API key")
    return key


def require_scope(scope: str):
    def _dependency(
        config: SkillRewindConfig = Depends(get_config),
        key: Optional[AuthenticatedKey] = Depends(current_key),
    ) -> Optional[AuthenticatedKey]:
        if config.api_auth_disabled:
            return None
        assert key is not None  # current_key raises before returning None when auth is enabled
        if scope not in key.scopes and "admin" not in key.scopes:
            raise ProblemDetail(403, "Forbidden", f"API key lacks required scope: {scope}")
        return key

    return _dependency
