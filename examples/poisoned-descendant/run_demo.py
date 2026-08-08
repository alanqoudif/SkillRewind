#!/usr/bin/env python3
"""SkillRewind primary deterministic demo: ``poisoned-descendant``.

Reproduces, end to end and fully offline, the scenario described in the
spec: a root skill's benchmark-only unsafe behavior (a mocked
"disable-cert-verification" canary) propagates into a descendant skill
whose derivation record does not include a recorded edge back to the root.
Recorded-lineage closure alone therefore misses the descendant. This script
recovers it via static hidden-lineage candidate scoring, confirms the
influence via deterministic paired counterfactual replay, revokes the root
under the ``balanced`` policy, quarantines the confirmed descendant,
rebuilds a clean successor, verifies it, publishes it under the original
alias, and emits a signed, verifiable attestation.

Every behavior here is an inert mocked boolean flag (``mock_disable_verification``)
inside a sandboxed deterministic fixture. Nothing here performs a real
network call, executes untrusted code, or contacts a paid/hosted model.
"""

# ruff: noqa: E402 -- sys.path must be extended before the skillrewind imports below
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from skillrewind.adapters.agent_skills import ingest_skill_directory
from skillrewind.attestation import (
    build_attestation,
    generate_keypair,
    render_html,
    render_markdown,
    sign_attestation,
    verify_attestation,
)
from skillrewind.domain.enums import RevocationPolicy, Severity
from skillrewind.domain.models import Derivation
from skillrewind.lineage.candidates import recover_candidates
from skillrewind.lineage.closure import recorded_descendants
from skillrewind.replay.deterministic import register_fixture
from skillrewind.revocation.service import request_revocation, run_revocation
from skillrewind.verification.suites import VerificationSuite
from skillrewind.workspace import Workspace

DEMO_DIR = REPO_ROOT / ".skillrewind-demo"
SKILLS_DIR = Path(__file__).parent / "skills"

RECIPE = "poisoned-descendant-recipe"


def poisoned_descendant_recipe(task_snapshot, available_context, seed):
    """Deterministic fixture: the mocked canary activates iff the root skill's
    influence is present in the reconstructed context. This is the ground
    truth the demo recovers and verifies -- not a hard-coded answer, since
    the same function drives both the replay comparison and the rebuild."""

    root_marker = task_snapshot.get("root_marker")
    canary = root_marker is not None and root_marker in available_context
    return {
        "mock_disable_verification": canary,
        "_behavior_keys": ["mock_disable_verification"],
        "_utility": {"task_success": 1.0},
    }


register_fixture(RECIPE, poisoned_descendant_recipe)


def milestone(n: int, text: str) -> None:
    print(f"[{n:>2}] {text}")


def main() -> int:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)

    print("SkillRewind poisoned-descendant demo")
    print("=" * 60)
    ws = Workspace.init(DEMO_DIR)

    # --- ingest three Agent Skills directories -----------------------------
    root = ingest_skill_directory(ws, SKILLS_DIR / "fast-http", alias="fast-http")
    descendant = ingest_skill_directory(ws, SKILLS_DIR / "deploy-service", alias="deploy-service")
    independent = ingest_skill_directory(ws, SKILLS_DIR / "independent-deploy", alias="independent-deploy")

    # Attach the probe/canary metadata + derivations. In a real capture SDK
    # integration this would come from the agent framework; here it is set
    # explicitly so the demo is self-contained and inspectable.
    for artifact, canary in ((root, True), (descendant, True), (independent, False)):
        artifact.metadata["canary_activated"] = canary
        artifact.metadata["probes"] = {"mock_disable_verification": canary}
        ws.artifacts.upsert(artifact)

    ws.derivations.upsert(
        Derivation(
            derivation_id="deriv-deploy-service",
            recipe=RECIPE,
            recipe_version="0.1",
            target_artifact_id=descendant.artifact_id,
            task_snapshot={"root_marker": root.artifact_id},  # hidden: no recorded edge below
            started_at="2026-01-01T00:05:00Z",
            ended_at="2026-01-01T00:05:01Z",
            seed=1,
        )
    )
    ws.derivations.upsert(
        Derivation(
            derivation_id="deriv-independent-deploy",
            recipe=RECIPE,
            recipe_version="0.1",
            target_artifact_id=independent.artifact_id,
            task_snapshot={"root_marker": None},
            started_at="2026-01-01T00:06:00Z",
            ended_at="2026-01-01T00:06:01Z",
            seed=1,
        )
    )
    milestone(0, f"Ingested 3 Agent Skills artifacts into isolated workspace {DEMO_DIR}")

    # --- 1/2: DeleteRoot / RecordedClosure miss the hidden descendant ------
    closure = recorded_descendants(ws, [root.artifact_id])
    assert descendant.artifact_id not in closure
    milestone(1, "DeleteRoot alone would leave deploy-service's mocked canary active (no recorded edge to sever).")
    milestone(2, f"RecordedClosure({root.logical_name}) = {sorted(a.split('//')[1].split('@')[0] for a in closure)} -- misses deploy-service (edge was never recorded).")

    # --- 3: static multi-trace candidate scoring ----------------------------
    candidates = recover_candidates(ws, descendant.artifact_id)
    root_candidate = next(c for c in candidates if c.candidate_id == root.artifact_id)
    milestone(
        3,
        f"Static multi-trace scoring flags fast-http as a hidden-lineage candidate for deploy-service "
        f"(score={root_candidate.result.score:.3f}, reasons={list(root_candidate.neighborhood_reasons)}).",
    )

    independent_candidates = recover_candidates(ws, independent.artifact_id, persist=False)
    root_vs_independent = next((c for c in independent_candidates if c.candidate_id == root.artifact_id), None)
    if root_vs_independent:
        milestone(
            4,
            f"independent-deploy vs fast-http: score={root_vs_independent.result.score:.3f}, "
            f"high_confidence={root_vs_independent.result.is_high_confidence} "
            f"(strict-negative signal={root_vs_independent.result.strict_negative_signal}).",
        )
    else:
        milestone(4, "independent-deploy did not even enter fast-http's candidate neighborhood.")

    # --- 5-11: balanced revocation: barrier -> replay -> quarantine -> rebuild -> verify -> publish -> attest
    suite = VerificationSuite(suite_id="poisoned-descendant-canary-suite", version="0.1.0", canary_keys=("mock_disable_verification",), utility_retention_threshold=0.9)
    event = request_revocation(
        ws, roots=[root.artifact_id], reason="Unsafe benchmark canary: mock_disable_verification may propagate",
        severity=Severity.HIGH, policy=RevocationPolicy.BALANCED, actor="demo-operator",
        idempotency_key="poisoned-descendant-demo",
    )
    milestone(5, f"Balanced revocation {event.event_id} requested; barrier applied (root revoked, recorded descendants quarantined).")
    event = run_revocation(ws, event, verification_suite=suite)

    confirmed = [d for d in event.replay_decisions if d.get("verdict") == "confirmed" and d["target"] == descendant.artifact_id]
    assert confirmed, "expected deploy-service to be replay-confirmed"
    milestone(6, f"Paired deterministic replay (root present vs withheld) changed deploy-service's canary -> CONFIRMED (replay_id={confirmed[0]['replay_id']}).")
    milestone(7, f"deploy-service quarantined: {ws.revocations.is_quarantined(descendant.artifact_id) or descendant.artifact_id in event.quarantined}.")

    rebuilt_entry = next((r for r in event.rebuilt if r["original"] == descendant.artifact_id), None)
    assert rebuilt_entry is not None, "expected a rebuilt+verified successor for deploy-service"
    successor = ws.artifacts.get(rebuilt_entry["successor"])
    milestone(8, f"Clean-room rebuild excluded the revoked root; successor {successor.artifact_id.split('@')[0]} has mock_disable_verification={successor.metadata['probes']['mock_disable_verification']}.")
    milestone(9, f"Verification suite status: {rebuilt_entry['verification']['status']} (canary absent, utility retained).")

    resolved = ws.resolve_alias("deploy-service")
    assert resolved is not None and resolved.artifact_id == successor.artifact_id
    milestone(10, f"resolve('deploy-service') -> {resolved.artifact_id} (never the quarantined original).")

    independent_resolved = ws.resolve_alias("independent-deploy")
    assert independent_resolved is not None and independent_resolved.artifact_id == independent.artifact_id
    milestone(10, f"resolve('independent-deploy') -> unchanged, {independent_resolved.artifact_id} (strict negative was not falsely quarantined).")

    attestation = build_attestation(ws, event)
    keys = generate_keypair(DEMO_DIR / "keys")
    signed = sign_attestation(attestation, keys.private_key_path)
    outcome = verify_attestation(signed, public_key_path=str(keys.public_key_path))
    assert outcome.ok

    attestation_path = DEMO_DIR / "attestation.json"
    attestation_path.write_text(__import__("json").dumps(signed, indent=2, sort_keys=True, default=str), encoding="utf-8")
    (DEMO_DIR / "attestation.md").write_text(render_markdown(signed), encoding="utf-8")
    (DEMO_DIR / "attestation.html").write_text(render_html(signed), encoding="utf-8")
    milestone(11, f"Attestation generated, signed (Ed25519), and verified -> {attestation_path}")

    audit_ok = ws.audit.verify().ok
    milestone(12, f"Audit chain verified: {audit_ok}. CLI/library expose the same persisted SQLite-backed state (no separate web UI in this session; see STATUS.md).")

    ws.close()
    print("=" * 60)
    print(f"Demo complete. Workspace: {DEMO_DIR}")
    print(f"Attestation JSON:     {attestation_path}")
    print(f"Attestation Markdown: {DEMO_DIR / 'attestation.md'}")
    print(f"Attestation HTML:     {DEMO_DIR / 'attestation.html'}")
    print("Inspect further with, e.g.:")
    print(f"  skillrewind --workspace {DEMO_DIR} revoke-status {event.event_id}")
    print(f"  skillrewind --workspace {DEMO_DIR} audit-verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
