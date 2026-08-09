"""Idempotency-Key handling for mutating endpoints (master spec 8.4).

A reused key with an identical request returns the original result. A
reused key with a different request returns conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..persistence.service.models import IdempotencyRecord


class IdempotencyConflict(Exception):
    pass


@dataclass
class IdempotencyOutcome:
    is_replay: bool
    response_reference: Optional[str]


def check(session: Session, *, key: str, actor: str, scope: str, request_digest: str) -> Optional[IdempotencyOutcome]:
    """Returns an outcome if this key has been seen before. Raises
    IdempotencyConflict if the same key was used with a different request.
    Returns None if this is a brand-new key (caller should proceed and then
    call `record`)."""

    existing = session.get(IdempotencyRecord, key)
    if existing is None:
        return None
    if existing.request_digest != request_digest or existing.operation_scope != scope:
        raise IdempotencyConflict(f"idempotency key {key!r} was already used with a different request")
    return IdempotencyOutcome(is_replay=True, response_reference=existing.response_reference)


def record(
    session: Session,
    *,
    key: str,
    actor: str,
    scope: str,
    request_digest: str,
    response_reference: Optional[str],
    ttl_hours: int = 24,
) -> None:
    row = IdempotencyRecord(
        key=key,
        actor=actor,
        operation_scope=scope,
        request_digest=request_digest,
        response_reference=response_reference,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
    )
    session.add(row)
    session.commit()
