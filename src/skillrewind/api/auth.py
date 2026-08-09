"""API-key authentication: creation, Argon2id hashing/verification, scopes.

Key shape: ``srw_<12-char prefix>_<43-char secret>``. The prefix is stored in
plaintext for O(1) lookup (it is not secret -- knowing it grants nothing);
only an Argon2id hash of the full key is ever persisted, per master spec
8.5 ("store only a strong hash, preferably Argon2id"). Plaintext is
returned to the caller exactly once, at creation time, and never logged.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.service.models import ApiKey

SCOPES = {"read", "ingest", "replay", "revoke", "waive", "bench", "admin"}

_hasher = PasswordHasher()


@dataclass
class CreatedApiKey:
    key_id: str
    plaintext: str  # shown once; caller must store it
    prefix: str
    name: str
    scopes: list[str]


def generate_key() -> tuple[str, str, str]:
    """Returns (plaintext, prefix, secret_part)."""

    prefix = secrets.token_hex(6)  # 12 hex chars, non-secret
    secret = secrets.token_urlsafe(32)
    plaintext = f"srw_{prefix}_{secret}"
    return plaintext, prefix, secret


def create_api_key(
    session: Session,
    *,
    name: str,
    actor: str,
    scopes: list[str],
    expires_at: Optional[datetime] = None,
) -> CreatedApiKey:
    invalid = set(scopes) - SCOPES
    if invalid:
        raise ValueError(f"unknown scopes: {sorted(invalid)}")
    plaintext, prefix, _secret = generate_key()
    key_hash = _hasher.hash(plaintext)
    row = ApiKey(
        prefix=prefix,
        key_hash=key_hash,
        name=name,
        actor=actor,
        scopes_json=sorted(set(scopes)),
        status="active",
        expires_at=expires_at,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return CreatedApiKey(key_id=row.key_id, plaintext=plaintext, prefix=prefix, name=name, scopes=sorted(set(scopes)))


@dataclass
class AuthenticatedKey:
    key_id: str
    name: str
    actor: str
    scopes: list[str]


def authenticate(session: Session, plaintext: str) -> Optional[AuthenticatedKey]:
    """Constant-time-verified lookup. Returns None on any failure (wrong key,
    revoked, expired) -- callers must not distinguish these in their response
    (spec 8.5: failed-auth handling should not leak which failure occurred)."""

    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != "srw":
        return None
    prefix = parts[1]
    row = session.execute(select(ApiKey).where(ApiKey.prefix == prefix)).scalar_one_or_none()
    if row is None:
        # Still do a dummy hash comparison to avoid a prefix-lookup timing oracle.
        try:
            _hasher.verify(_hasher.hash("dummy"), plaintext)
        except VerifyMismatchError:
            pass
        return None
    try:
        _hasher.verify(row.key_hash, plaintext)
    except VerifyMismatchError:
        return None
    if row.status != "active":
        return None
    if row.expires_at is not None and row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    row.last_used_at = datetime.now(timezone.utc)
    session.commit()
    return AuthenticatedKey(key_id=row.key_id, name=row.name, actor=row.actor, scopes=list(row.scopes_json))


def request_digest(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(method.encode() + b"\n" + path.encode() + b"\n" + body).hexdigest()
