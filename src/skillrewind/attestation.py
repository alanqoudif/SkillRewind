"""Recorded-evidence attestation generation for the version 0.1 baseline."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from .graph import RecordedLineageGraph
from . import __version__


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def recorded_attestation(
    graph: RecordedLineageGraph,
    roots: list[str],
    *,
    reason: str = "Recorded-lineage forensic analysis",
) -> dict[str, object]:
    """Create a schema-shaped attestation without overstating capabilities."""

    started = _timestamp()
    closure = list(graph.descendants(roots))
    completed = _timestamp()
    return {
        "schema_version": "0.1",
        "event_id": f"recorded-{uuid.uuid4()}",
        "mode": "recorded-only",
        "started_at": started,
        "completed_at": completed,
        "tool_version": __version__,
        "revocation_reason": reason,
        "revoked_roots": sorted(set(roots)),
        "replayability_boundary": {
            "description": "Version 0.1 traversed only the supplied recorded edges.",
            "model_identifiers": [],
            "environment_digests": [],
            "known_gaps": [
                "No hidden-lineage inference was run.",
                "No counterfactual replay, quarantine, rebuild, or behavioral verification was run."
            ]
        },
        "recorded_closure": closure,
        "inferred_candidates": [],
        "replay_confirmed": [],
        "rejected": [],
        "quarantined": [],
        "rebuilt": [],
        "unresolved": [],
        "verification": {
            "status": "not-run",
            "suite_ids": [],
            "target_behavior_observed": None,
            "reports": []
        },
        "cost": {
            "replay_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "wall_clock_seconds": 0
        },
        "bounded_claims": [
            "The listed recorded_closure is the deterministic transitive closure of the supplied edge file.",
            "This artifact makes no claim about missing lineage, behavioral influence, successful quarantine, repair, or unlearning."
        ]
    }
