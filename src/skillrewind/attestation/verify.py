"""Attestation integrity verification: content digest and optional signature."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..canonical.json import sha256_hex
from .signing import verify_signature


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    digest_valid: bool
    signature_valid: Optional[bool]  # None if unsigned

    @property
    def ok(self) -> bool:
        return self.digest_valid and (self.signature_valid is not False)


def verify_attestation(attestation: dict[str, Any], *, public_key_path: str | None = None) -> VerificationOutcome:
    claimed_digest = attestation.get("content_digest")
    recomputed = sha256_hex({k: v for k, v in attestation.items() if k not in ("content_digest", "signature")})
    digest_valid = claimed_digest == recomputed

    signature_valid = None
    if attestation.get("signature"):
        signature_valid = verify_signature(attestation, public_key_path)

    return VerificationOutcome(digest_valid=digest_valid, signature_valid=signature_valid)
