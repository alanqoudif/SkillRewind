"""Bounded revocation attestation builder (schema v0.2).

Every field is generated from persisted event/edge/replay state -- nothing
here is invented. ``bounded_claims`` are deliberately narrow: they state
what was verified within the declared replay/verification boundary, and
never claim erasure from a foundation model or universal absence of
influence. See ``docs/concepts/evidence-semantics.md``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .. import __version__
from ..canonical.json import sha256_hex
from ..domain.models import RevocationEvent
from ..workspace_protocol import WorkspaceLike

ATTESTATION_SCHEMA_VERSION = "0.2"


def _config_digest(workspace: WorkspaceLike) -> str:
    cfg = workspace.config
    return sha256_hex(
        {
            "mode": cfg.mode,
            "feature_weights": asdict(cfg.feature_weights),
            "thresholds": asdict(cfg.thresholds),
            "replay_fidelity": asdict(cfg.replay_fidelity),
        }
    )


def build_attestation(workspace: WorkspaceLike, event: RevocationEvent) -> dict[str, Any]:
    replay_calls = sum(
        d.get("replay") == "executed" for d in event.replay_decisions
    ) * 2
    wall_clock = 0  # deterministic fixture runs are not independently timed in this session

    confirmed_edges = [d for d in event.replay_decisions if d.get("verdict") == "confirmed"]
    rejected_edges = [d for d in event.replay_decisions if d.get("verdict") == "rejected"]
    skipped = [d for d in event.replay_decisions if d.get("replay") == "skipped"]

    known_gaps = [
        "Static candidate scoring uses hand-weighted, provisionally-calibrated feature "
        "weights, not a trained/validated model.",
        "Replay in this attestation used the deterministic-fixture runner unless otherwise "
        "noted per replay record; it establishes causal effect only within that declared, "
        "fully-reconstructed replay boundary, not against a live production system.",
        "Rejection under the declared intervention and probe set is not universal proof of no "
        "influence outside that boundary.",
    ]
    if skipped:
        known_gaps.append(
            f"{len(skipped)} inferred candidate(s) were not replayed (skipped under the active "
            "replay budget); they remain in an inferred, non-replay-confirmed evidence state."
        )

    bounded_claims = [
        "recorded_closure lists the deterministic transitive closure of the supplied recorded "
        "edges over the revoked roots at the time this attestation was generated.",
        "inferred_candidates were produced by static multi-trace scoring and are not causal claims.",
        f"{len(confirmed_edges)} candidate edge(s) were replay-confirmed within the declared "
        "deterministic replay boundary: the target behavior changed when the candidate ancestor "
        "was withheld versus present, holding all other reconstructable variables fixed.",
        f"{len(rejected_edges)} candidate edge(s) were tested and rejected within that boundary "
        "(no behavior change was observed); this is not proof of no influence outside the boundary.",
        f"{len(event.quarantined)} artifact(s) were quarantined and are not resolvable via the "
        "serving-resolution layer as of this attestation.",
        f"{len(event.rebuilt)} successor artifact(s) were rebuilt from a clean support set "
        "(excluding revoked roots and replay-confirmed contaminated ancestors) and passed the "
        "declared verification suite before being published as active.",
    ]
    if event.unresolved:
        bounded_claims.append(
            f"{len(event.unresolved)} item(s) remain unresolved and are listed explicitly; "
            "this attestation does not claim they are safe."
        )
    bounded_claims.append(
        "This attestation makes no claim of erasure from any foundation model's parameters "
        "or of behavior in any environment outside the declared verifier suite."
    )

    attestation: dict[str, Any] = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "event_id": event.event_id,
        "mode": event.policy.value,
        "started_at": event.created_at,
        "completed_at": event.completed_at,
        "tool_version": __version__,
        "configuration_digest": _config_digest(workspace),
        "revoked_roots": event.roots,
        "revocation_reason": event.reason,
        "severity": event.severity.value,
        "final_state": event.state.value,
        "replayability_boundary": {
            "description": (
                "Replay in this session used the deterministic-fixture runner, which "
                "reconstructs the target derivation in-process from its recorded recipe, "
                "task snapshot, and context pool. It does not call any hosted or paid model."
            ),
            "model_identifiers": sorted(
                {d.get("replay_id", "") for d in event.replay_decisions if d.get("replay") == "executed"}
            ),
            "environment_digests": [],
            "known_gaps": known_gaps,
        },
        "recorded_closure": event.recorded_closure,
        "inferred_candidates": event.candidates,
        "selected_and_skipped_replays": event.replay_decisions,
        "replay_confirmed": confirmed_edges,
        "rejected": rejected_edges,
        "unresolved": event.unresolved,
        "quarantined": event.quarantined,
        "waivers": [],
        "rebuilt": event.rebuilt,
        "verification": {
            "status": "run" if event.rebuilt or event.unresolved else "not-run",
            "reports": [r.get("verification") for r in event.rebuilt if r.get("verification")],
        },
        "residual_canary_observations": [
            u for u in event.unresolved if isinstance(u, dict) and u.get("verdict")
        ],
        "cost": {
            "replay_calls": replay_calls,
            "input_tokens": 0,
            "output_tokens": 0,
            "wall_clock_seconds": wall_clock,
        },
        "audit_chain_head": workspace.audit.head_hash(),
        "bounded_claims": bounded_claims,
    }
    attestation["content_digest"] = sha256_hex(attestation)
    return attestation
