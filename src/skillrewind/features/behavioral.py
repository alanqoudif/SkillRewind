"""Behavioral-trace features: safe stored probe-output comparison.

Behavioral features must come from *stored* probe outputs (e.g. a
deterministic verification-suite run recorded in a derivation's
``validation_result`` or an artifact's metadata), never from executing an
untrusted artifact on the host as part of feature extraction. Executing
untrusted logic happens only later, inside the sandboxed replay runner
(``skillrewind.replay``), under an explicit intervention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .expression import jaccard


def _probe_vector(source: dict[str, Any]) -> dict[str, Any]:
    """Extract a flat probe-name -> value vector from a stored validation result."""

    probes = source.get("probes") if isinstance(source, dict) else None
    if isinstance(probes, dict):
        return probes
    return {}


@dataclass(frozen=True, slots=True)
class BehavioralScore:
    canary_agreement: float
    probe_agreement: float
    combined: float
    source: str  # "stored-probes" or "unavailable"


def behavioral_similarity(
    metadata_a: Optional[dict[str, Any]], metadata_b: Optional[dict[str, Any]]
) -> BehavioralScore:
    metadata_a = metadata_a or {}
    metadata_b = metadata_b or {}
    probes_a = _probe_vector(metadata_a)
    probes_b = _probe_vector(metadata_b)

    if not probes_a or not probes_b:
        return BehavioralScore(0.0, 0.0, 0.0, source="unavailable")

    shared_keys = set(probes_a) & set(probes_b)
    if not shared_keys:
        return BehavioralScore(0.0, 0.0, 0.0, source="stored-probes")

    agreements = sum(1 for k in shared_keys if probes_a[k] == probes_b[k])
    probe_agreement = agreements / len(shared_keys)

    canary_a = metadata_a.get("canary_activated")
    canary_b = metadata_b.get("canary_activated")
    canary_agreement = 1.0 if (canary_a is not None and canary_a == canary_b) else 0.0

    combined = 0.5 * canary_agreement + 0.5 * probe_agreement
    return BehavioralScore(
        canary_agreement=canary_agreement,
        probe_agreement=probe_agreement,
        combined=combined,
        source="stored-probes",
    )
