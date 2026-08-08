"""Human-readable attestation rendering (Markdown / HTML)."""

from __future__ import annotations

import html
from typing import Any


def render_markdown(attestation: dict[str, Any]) -> str:
    lines = [
        f"# SkillRewind Revocation Attestation `{attestation['event_id']}`",
        "",
        f"- **Schema version:** {attestation['schema_version']}",
        f"- **Mode/policy:** {attestation['mode']}",
        f"- **Severity:** {attestation.get('severity', 'n/a')}",
        f"- **Final state:** {attestation.get('final_state', 'n/a')}",
        f"- **Started:** {attestation.get('started_at')}",
        f"- **Completed:** {attestation.get('completed_at')}",
        f"- **Tool version:** {attestation['tool_version']}",
        f"- **Content digest:** `{attestation['content_digest']}`",
        f"- **Audit chain head:** `{attestation.get('audit_chain_head')}`",
        "",
        "## Revoked roots",
        "",
        *[f"- `{r}`" for r in attestation["revoked_roots"]],
        "",
        f"**Reason:** {attestation['revocation_reason']}",
        "",
        "## Replayability boundary",
        "",
        attestation["replayability_boundary"]["description"],
        "",
        "Known gaps:",
        *[f"- {g}" for g in attestation["replayability_boundary"]["known_gaps"]],
        "",
        "## Evidence summary",
        "",
        f"- Recorded closure: {len(attestation['recorded_closure'])} artifact(s)",
        f"- Inferred candidates: {len(attestation['inferred_candidates'])}",
        f"- Replay-confirmed: {len(attestation['replay_confirmed'])}",
        f"- Rejected: {len(attestation['rejected'])}",
        f"- Unresolved: {len(attestation['unresolved'])}",
        f"- Quarantined: {len(attestation['quarantined'])}",
        f"- Rebuilt successors: {len(attestation['rebuilt'])}",
        "",
        "## Bounded claims",
        "",
        *[f"- {c}" for c in attestation["bounded_claims"]],
        "",
        "## Cost",
        "",
        f"- Replay calls: {attestation['cost']['replay_calls']}",
        f"- Wall-clock seconds: {attestation['cost']['wall_clock_seconds']}",
        "",
    ]
    if attestation.get("signature"):
        lines += ["## Signature", "", f"- Algorithm: {attestation['signature']['algorithm']}",
                   f"- Public key: `{attestation['signature']['public_key_hex']}`", ""]
    return "\n".join(lines)


def render_html(attestation: dict[str, Any]) -> str:
    md = render_markdown(attestation)
    escaped = html.escape(md)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>SkillRewind Attestation {html.escape(attestation['event_id'])}</title></head>"
        "<body><pre style='font-family:ui-monospace,monospace;white-space:pre-wrap;'>"
        f"{escaped}</pre></body></html>"
    )
