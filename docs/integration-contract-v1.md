# SkillRewind Integration Contract v1

**Status:** v0.3 alpha / research and integration preview. This document defines the platform-agnostic public
integration surface for external AI-agent platforms. It complements `docs/api-stability-v1.md` (which
categorizes every concrete endpoint) and `docs/event-contract-v1.md` (which defines the event envelope).

SkillRewind is not an AI framework, an agent runtime, or a model host. It is an independent service that a
platform integrates with to make its own persistent learned artifacts traceable, revocable, rebuildable, and
verifiable. This document defines exactly what a platform reports to SkillRewind, what it can ask SkillRewind,
and what it is not required to do.

## 1. Six public integration responsibilities

A platform integration is composed of up to six independent responsibilities. A platform does not need all six —
see [Integration Levels](#integration-levels) below for which subset each level requires.

### 1.1 Artifact ingestion

A platform reports a persistent learned artifact it created — something that will outlive the session/request
that produced it and will influence future behavior. Recognized kinds (`skillrewind.domain.enums.ArtifactKind`,
exposed as the `kind` field): `agent-skill`, `memory`, `prompt-patch`, `generated-code`, `workflow`,
`tool-policy`, `distilled-procedure`, `configuration`.

**Minimum required metadata** (`POST /api/v1/artifacts`):

- `kind` — one of the recognized kinds above.
- `logical_name` — a stable human-readable name the platform uses to refer to this artifact family (not unique
  per version; `alias` is the unique pointer to the current version).
- raw content bytes (the request body) — hashed into the artifact's content-addressed identity.

**Optional metadata:**

- `alias` — a stable logical pointer (e.g. `"my-org/checkout-skill"`) that always resolves to the current
  active/superseded-aware version; required if the platform wants `GET /{artifact_id}/resolve` to track
  supersession automatically rather than tracking artifact IDs itself.
- `mime_type` — defaults to `application/octet-stream`.
- `creator` — actor identity attributed in the audit trail.
- free-form `metadata` (JSON object, bounded size) — platform-specific context (e.g. originating agent run ID,
  model identity, source conversation ID). SkillRewind never inspects the *semantics* of this field; it is
  opaque, versioned storage.

### 1.2 Derivation capture

A platform reports which inputs influenced the creation of a persistent artifact — this is the **recorded**
evidence class, the only evidence class a platform's own instrumentation can directly assert (everything else
SkillRewind derives itself via inference or replay).

Canonical relation types (`skillrewind.domain.enums.RelationType`):

| Relation | Meaning |
|---|---|
| `used-as-input` | The source artifact's content was directly included in the derivation's input context. |
| `derived-from` | The target was produced by transforming the source (paraphrase, refactor, translation). |
| `rebuilt-from` | The target is a clean-room rebuild of the source (SkillRewind-internal; platforms do not report this directly). |
| `replaces` | The target supersedes the source under the same logical alias. |
| `clean-support` | The source was retained as verified-clean rebuild input (SkillRewind-internal). |

A platform calls `POST /api/v1/derivations` with a `recipe` (a stable string identifying *how* the artifact was
produced — e.g. `"skill-distillation-v3"`), then `POST /api/v1/derivations/{id}/inputs` for each recorded input
and `POST /api/v1/derivations/{id}/output` to bind the derivation to its resulting artifact. Recording a
derivation is what makes an edge `recorded` rather than something SkillRewind must later `infer`.

### 1.3 Resolution gate

Before serving or executing a persistent learned artifact, an enforcing platform (Level 2+) calls
`GET /api/v1/artifacts/{artifact_id}/resolve` (or resolves an `alias` first via
`GET /api/v1/artifacts?alias=...`) and receives a `resolution` value — see
[Resolution values](#resolution-values) below. A conformant enforcer treats `revoked` and `quarantined` as hard
denials and never overrides them with local platform logic; see `skillrewind.adapters.protocols
.ResolutionEnforcer` for the reference interface shape.

#### Resolution values

| `resolution` | Meaning | Platform action |
|---|---|---|
| `active` | Eligible for use, no exception in effect. | Serve/execute. |
| `revoked` | Directly revoked; never eligible. | Deny. Never overridable. |
| `quarantined` | Descendant of a revocation, pending investigation/rebuild. | Deny, unless `allowed-by-waiver`. |
| `superseded` | A verified successor exists; `successor_artifact_id` is populated. | Resolve/serve the successor instead. |
| `allowed-by-waiver` | Quarantined, but an active, unexpired, scoped waiver grants an exception. Evaluated dynamically on every call — see `docs/threat-model.md` and the waiver scope rules below. | Serve, but the platform should still surface that this is a waiver-driven exception (e.g. in its own audit UI), since the underlying quarantine reason remains open. |
| `unavailable` | Unknown/unrecognized lifecycle state. | Deny (fail closed). |

Waiver scopes relevant to resolution: `serving` (canonical) / `quarantine-release` (accepted alias) /
`quarantine` (accepted synonym) grant `allowed-by-waiver`. A `rebuild-support`-scoped waiver never affects
resolution — it only affects whether a quarantined artifact can remain in a *rebuild's* support set (see 1.5).

### 1.4 Replay hook

A fully integrated platform (Level 3) may expose a **replay adapter** — the ability to re-run a specific
derivation under a controlled intervention (`present` / `withheld` / `control` on one candidate ancestor) and
report the observed output back to SkillRewind, which classifies the result (`replay-confirmed` /
`replay-rejected` / `unresolved`) against the configured verification suite. See
`skillrewind.adapters.protocols.ReplayProvider` for the reference interface shape and
`POST /api/v1/replay/runs` for the API-side trigger. A platform without a replay hook can still use Level 1/2;
SkillRewind's own deterministic-fixture and sandboxed-subprocess runners exist for cases where the platform
itself does not run replay (see `docs/threat-model.md` for the sandbox's real isolation limits).

### 1.5 Rebuild hook

A fully integrated platform (Level 3) may expose a **clean rebuild mechanism**: given a target artifact and an
explicit clean support set (SkillRewind has already excluded revoked, replay-confirmed-contaminated, and
quarantined-without-a-`rebuild-support`-waiver ancestors — see `skillrewind.rebuild.planner.plan_rebuild`), the
platform reproduces the artifact using only that support set. See
`skillrewind.adapters.protocols.RebuildProvider` for the reference interface shape. Without a rebuild hook,
SkillRewind's own clean-room rebuild (deterministic-fixture-driven) is used instead.

### 1.6 Event consumption

A platform may consume lifecycle events over `GET /api/v1/events/stream` (SSE, resumable via `Last-Event-ID` —
see `docs/event-contract-v1.md` for the envelope) or by implementing `skillrewind.adapters.protocols
.EventConsumer`. Representative event types: `artifact.ingested`, `artifact.quarantined`, `artifact.revoked`,
`revocation.completed`, `rebuild.completed`, `verification.completed`, `successor.published`,
`attestation.signed`. Event consumption is optional at every level — a platform can always poll the relevant
resource endpoint instead.

## Integration Levels

A platform does not need all six responsibilities above for a useful integration. Three explicit, cumulative
Integration Levels define what "SkillRewind is integrated" means at increasing depth. **A platform is not
Level 3 merely because it uploads logs** — it must implement the specific hooks/endpoints listed.

### SkillRewind Integration Level 1 — Audit

The platform ingests artifacts, records derivations, and allows lineage analysis. SkillRewind can analyze
provenance but does not control serving — nothing the platform does is blocked or gated by SkillRewind at this
level.

- **Mandatory capabilities:** `artifact-ingestion`, `derivation-capture`, `lineage-read`.
- **Mandatory endpoints:** `POST /api/v1/artifacts`, `GET /api/v1/artifacts/{artifact_id}`,
  `POST /api/v1/derivations`, `POST /api/v1/derivations/{derivation_id}/output`,
  `GET /api/v1/lineage/{artifact_id}/ancestors`, `GET /api/v1/lineage/{artifact_id}/descendants`.
- **Adapter protocols:** `ArtifactProvider`, `DerivationProvider`.

### SkillRewind Integration Level 2 — Enforcement

Adds the resolution gate and quarantine/revocation enforcement: the platform asks SkillRewind whether an
artifact is eligible before serving or executing it, and treats `revoked`/`quarantined` as binding.

- **Mandatory capabilities:** everything in Level 1, plus `resolution-gate`, `quarantine-enforcement`.
- **Mandatory endpoints:** everything in Level 1, plus `GET /api/v1/artifacts/{artifact_id}/resolve`,
  `GET /api/v1/quarantine`, `GET /api/v1/artifacts/{artifact_id}/quarantine`, `POST /api/v1/revocations`,
  `GET /api/v1/revocations/{revocation_id}`.
- **Adapter protocols:** everything in Level 1, plus `ResolutionEnforcer`.

### SkillRewind Integration Level 3 — Full Rewind

Adds replay, rebuild, verification, successor publication, and attestation — the complete reversible-learning
lifecycle this repository's own demo (`make demo`) exercises end to end.

- **Mandatory capabilities:** everything in Level 2, plus `replay-hook`, `rebuild-hook`, `verification-read`,
  `attestation`.
- **Mandatory endpoints:** everything in Level 2, plus `POST /api/v1/replay/runs`,
  `GET /api/v1/replay/runs/{replay_run_id}`, `GET /api/v1/rebuilds/{rebuild_id}` (+ `/support`, `/exclusions`,
  `/output`), `GET /api/v1/verifications/{verification_id}` (+ `/safety`, `/utility`, `/integrity`),
  `POST /api/v1/attestations`, `GET /api/v1/attestations/{attestation_id}/canonical`,
  `POST /api/v1/attestations/{attestation_id}/verify`, `GET /api/v1/events/stream`.
- **Adapter protocols:** everything in Level 2, plus `ReplayProvider`, `RebuildProvider`, `EventConsumer`.

Machine-readable form of this section lives in `skillrewind.conformance.levels` (`skillrewind conformance
describe`); the reference Python adapter interfaces live in `skillrewind.adapters.protocols`, with one
deterministic in-memory example implementation in `skillrewind.adapters.reference` (see
`tests/unit/test_adapter_protocols.py`). `skillrewind conformance self-test` proves this repository's own
Service-mode API satisfies its own Level 1–3 contract using local, deterministic fixtures.

## What this contract does not promise

- Universal model unlearning or foundation-model weight erasure — see `docs/threat-model.md` and the bounded
  claims embedded in every attestation (`bounded_claims` field).
- Guaranteed causal attribution for `inferred` evidence — inference is explainable static scoring, not proof.
- A production-ready arbitrary-code replay sandbox — see `docs/threat-model.md` for the sandboxed-subprocess
  runner's real isolation limits.
- Indefinite API stability beyond the v1 contract defined in `docs/api-stability-v1.md`.
