"""Integration Level definitions (Phase C2.4 section 5), machine-readable.

Mirrors `docs/integration-contract-v1.md` exactly -- that document is prose
for humans; this module is the same information as data, so a conformance
checker (this package's `self_test`, or a future external one) can assert
against it instead of re-deriving it from documentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class IntegrationLevel:
    level: int
    name: str
    description: str
    required_capabilities: tuple[str, ...]
    required_endpoints: tuple[str, ...]
    adapter_protocols: tuple[str, ...] = field(default_factory=tuple)


LEVEL_1_AUDIT = IntegrationLevel(
    level=1,
    name="SkillRewind Integration Level 1 -- Audit",
    description=(
        "The platform reports learned artifacts and their derivations so SkillRewind can analyze "
        "provenance. SkillRewind does not control serving at this level."
    ),
    required_capabilities=("artifact-ingestion", "derivation-capture", "lineage-read"),
    required_endpoints=(
        "POST /api/v1/artifacts",
        "GET /api/v1/artifacts/{artifact_id}",
        "POST /api/v1/derivations",
        "POST /api/v1/derivations/{derivation_id}/output",
        "GET /api/v1/lineage/{artifact_id}/ancestors",
        "GET /api/v1/lineage/{artifact_id}/descendants",
    ),
    adapter_protocols=("ArtifactProvider", "DerivationProvider"),
)

LEVEL_2_ENFORCEMENT = IntegrationLevel(
    level=2,
    name="SkillRewind Integration Level 2 -- Enforcement",
    description=(
        "Adds the resolution gate and quarantine/revocation enforcement: the platform asks "
        "SkillRewind whether an artifact is eligible before serving or executing it."
    ),
    required_capabilities=LEVEL_1_AUDIT.required_capabilities + ("resolution-gate", "quarantine-enforcement"),
    required_endpoints=LEVEL_1_AUDIT.required_endpoints
    + (
        "GET /api/v1/artifacts/{artifact_id}/resolve",
        "GET /api/v1/quarantine",
        "GET /api/v1/artifacts/{artifact_id}/quarantine",
        "POST /api/v1/revocations",
        "GET /api/v1/revocations/{revocation_id}",
    ),
    adapter_protocols=LEVEL_1_AUDIT.adapter_protocols + ("ResolutionEnforcer",),
)

LEVEL_3_FULL_REWIND = IntegrationLevel(
    level=3,
    name="SkillRewind Integration Level 3 -- Full Rewind",
    description=(
        "Adds replay, rebuild, verification, successor publication, and attestation: the full "
        "reversible-learning lifecycle."
    ),
    required_capabilities=LEVEL_2_ENFORCEMENT.required_capabilities
    + ("replay-hook", "rebuild-hook", "verification-read", "attestation"),
    required_endpoints=LEVEL_2_ENFORCEMENT.required_endpoints
    + (
        "POST /api/v1/replay/runs",
        "GET /api/v1/replay/runs/{replay_run_id}",
        "GET /api/v1/rebuilds/{rebuild_id}",
        "GET /api/v1/rebuilds/{rebuild_id}/support",
        "GET /api/v1/rebuilds/{rebuild_id}/exclusions",
        "GET /api/v1/rebuilds/{rebuild_id}/output",
        "GET /api/v1/verifications/{verification_id}",
        "GET /api/v1/verifications/{verification_id}/safety",
        "GET /api/v1/verifications/{verification_id}/utility",
        "GET /api/v1/verifications/{verification_id}/integrity",
        "POST /api/v1/attestations",
        "GET /api/v1/attestations/{attestation_id}/canonical",
        "POST /api/v1/attestations/{attestation_id}/verify",
        "GET /api/v1/events/stream",
    ),
    adapter_protocols=LEVEL_2_ENFORCEMENT.adapter_protocols + ("ReplayProvider", "RebuildProvider", "EventConsumer"),
)

LEVEL_REQUIREMENTS: dict[int, IntegrationLevel] = {
    1: LEVEL_1_AUDIT,
    2: LEVEL_2_ENFORCEMENT,
    3: LEVEL_3_FULL_REWIND,
}


def describe() -> dict[str, Any]:
    """Machine-readable contract summary -- the payload for `skillrewind
    conformance describe`."""

    return {
        "contract_version": CONTRACT_VERSION,
        "levels": [
            {
                "level": lvl.level,
                "name": lvl.name,
                "description": lvl.description,
                "required_capabilities": list(lvl.required_capabilities),
                "required_endpoints": list(lvl.required_endpoints),
                "adapter_protocols": list(lvl.adapter_protocols),
            }
            for lvl in LEVEL_REQUIREMENTS.values()
        ],
    }
