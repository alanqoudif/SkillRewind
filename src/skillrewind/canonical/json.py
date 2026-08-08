"""Deterministic canonical JSON serialization.

This is a project-specific canonicalization profile, not a claim of
conformance to any external canonical-JSON standard (e.g. JCS/RFC 8785).
The profile is:

- UTF-8 output;
- object keys sorted lexicographically by their UTF-8 code points;
- stable, minimal separators (``,`` and ``:``), no extraneous whitespace;
- datetimes must already be ISO-8601 strings normalized to UTC (``...Z``);
  this module does not silently convert naive datetimes;
- ``NaN`` and ``Infinity`` are rejected, not silently coerced;
- enums are serialized via their ``.value``;
- bytes are rejected: callers must pass a digest reference (e.g.
  ``sha256:<hex>``) instead of embedding raw binary content.
"""

from __future__ import annotations

import enum
import hashlib
import json
from typing import Any


class CanonicalizationError(ValueError):
    """Raised when a value cannot be canonicalized deterministically."""


def _default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, bytes):
        raise CanonicalizationError(
            "raw bytes are not canonicalizable; store content in CAS and reference its digest"
        )
    if isinstance(value, (set, frozenset)):
        raise CanonicalizationError("sets are not canonicalizable; use a sorted list")
    raise CanonicalizationError(f"object of type {type(value).__name__} is not canonicalizable")


def _reject_non_finite(obj: Any) -> None:
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # NaN/Infinity
            raise CanonicalizationError("NaN and Infinity are not permitted in canonical JSON")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"canonical JSON object keys must be strings, got {key!r}")
            _reject_non_finite(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _reject_non_finite(item)


def canonical_bytes(value: Any) -> bytes:
    """Serialize ``value`` to canonical JSON bytes."""

    _reject_non_finite(value)
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to a canonical JSON string."""

    return canonical_bytes(value).decode("utf-8")


def canonical_hash(value: Any, *, algorithm: str = "sha256") -> str:
    """Return ``<algorithm>:<hex-digest>`` of the canonical serialization of ``value``."""

    digest = hashlib.new(algorithm, canonical_bytes(value)).hexdigest()
    return f"{algorithm}:{digest}"


def sha256_hex(value: Any) -> str:
    """Return the bare lowercase hex sha256 digest of the canonical serialization."""

    return hashlib.sha256(canonical_bytes(value)).hexdigest()
