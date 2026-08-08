"""Artifact identifier scheme.

Production artifact IDs follow ``<scheme>://<logical-name>@sha256:<64-hex>``,
for example ``skill://deploy-fastapi@sha256:9f86d0...``. The scheme segment is
informational (it should reflect the artifact kind's conventional name, e.g.
``skill``, ``trace``, ``memory``) but is not itself validated against
:class:`~skillrewind.domain.enums.ArtifactKind`.

A ``legacy/demo`` compatibility mode accepts short, non-sha256 digests (as
used by the v0.1 recorded-lineage examples) but such IDs must never be
produced by v0.2+ ingestion paths.
"""

from __future__ import annotations

import re

from .errors import InvalidArtifactIdError

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_STRICT_ID = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<name>[^@/]+)@sha256:(?P<digest>[0-9a-f]{64})$"
)
_LOOSE_ID = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<name>[^@/]+)(@(?P<rest>.+))?$")


def build_artifact_id(scheme: str, logical_name: str, digest_hex: str) -> str:
    """Build a strict, production-grade artifact ID.

    Raises :class:`InvalidArtifactIdError` if any component is invalid.
    """

    if not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
        raise InvalidArtifactIdError(f"invalid scheme: {scheme!r}")
    if not logical_name or "@" in logical_name or "/" in logical_name:
        raise InvalidArtifactIdError(f"invalid logical name: {logical_name!r}")
    digest_hex = digest_hex.lower()
    if not _SHA256_HEX.fullmatch(digest_hex):
        raise InvalidArtifactIdError(
            f"digest must be a full 64-character lowercase sha256 hex string, got {digest_hex!r}"
        )
    return f"{scheme}://{logical_name}@sha256:{digest_hex}"


def parse_artifact_id(artifact_id: str, *, allow_legacy: bool = False) -> tuple[str, str, str]:
    """Parse an artifact ID into ``(scheme, logical_name, digest_hex)``.

    When ``allow_legacy`` is false (the default, and required in production
    paths), only strict full-length sha256 IDs are accepted.
    """

    match = _STRICT_ID.fullmatch(artifact_id)
    if match:
        return match.group("scheme"), match.group("name"), match.group("digest")

    if allow_legacy:
        loose = _LOOSE_ID.fullmatch(artifact_id)
        if loose:
            rest = loose.group("rest") or ""
            digest = rest.split(":", 1)[-1] if ":" in rest else rest
            return loose.group("scheme"), loose.group("name"), digest

    raise InvalidArtifactIdError(
        f"{artifact_id!r} is not a valid production artifact ID "
        "(expected scheme://name@sha256:<64-hex>)"
    )


def is_valid_artifact_id(artifact_id: str, *, allow_legacy: bool = False) -> bool:
    try:
        parse_artifact_id(artifact_id, allow_legacy=allow_legacy)
        return True
    except InvalidArtifactIdError:
        return False
