# ADR-0013: Service-mode revocation, rebuild, verification, and attestation (Phase C2.3)

## Status
Accepted, implemented 2026-08-09.

## Context
Through Phase C2.2, Service mode could ingest artifacts, capture derivations,
recover hidden-lineage candidates, and run replay to promote evidence to
`replay-confirmed`/`rejected`/`unresolved` — but a replay-confirmed edge had
no path to actually revoke or quarantine anything in Service mode. The
existing `revocation.execute` job handler only opened a **Lite-mode**
`Workspace` on disk, even when triggered from the Service-mode API path; the
milestone's own instructions call this out as unacceptable ("must not
secretly operate on a separate Lite-mode workspace when invoked through the
Service-mode API").

The Lite-mode revocation pipeline (`skillrewind.revocation.service.
run_revocation`, `barrier.apply_barrier`, `quarantine.service/waivers`,
`rebuild.planner/service/clean_room`, `verification.suites`, `attestation.
builder/signing/verify/render`) was already real, tested, and — critically —
already checkpoint-resumable at a per-target granularity (quarantine/rebuild/
replay-decision lists persisted after every item). Rewriting all of that
against Service-mode repositories, the way Phase C2.2 did for replay
(`replay.service_mode`), would have meant either duplicating a large amount
of already-correct domain logic, or a second, parallel implementation of
revocation orchestration — both explicitly forbidden by the milestone.

## Decision
Introduce `ServiceWorkspace` (`skillrewind.persistence.service.workspace`),
a class that duck-types the exact public surface of `skillrewind.workspace.
Workspace` — `.artifacts`, `.derivations`, `.edges`, `.replays`,
`.revocations`, `.waivers`, `.audit`, `.cas`, `.config`, `.ingest_artifact()`,
`.resolve_alias()` — but is backed by the real SQLAlchemy Service-mode schema
and CAS instead of a local SQLite file.

The reused orchestration functions (`run_revocation`, `apply_barrier`,
`quarantine_artifact`/`release_quarantine`, `create_waiver`/`revoke_waiver`,
`plan_rebuild`/`rebuild_artifact`/`publish_successor`, `run_suite`,
`build_attestation`) were never tied to the concrete `Workspace` class — they
only ever call `workspace.<repo>.<method>()`. Their type hints are loosened
from `Workspace` to a new structural `WorkspaceLike` Protocol
(`skillrewind.workspace_protocol`) so both concrete classes satisfy them.
Passing a `ServiceWorkspace` runs the *exact same, unmodified* domain logic
against Service-mode persistence — not a second implementation.

New Service-mode-only pieces, layered on top rather than duplicating this
logic:

- `_ServiceRevocations` / `_ServiceWaivers` / `_ServiceDerivations` /
  `_ServiceArtifacts` adapters inside `ServiceWorkspace`, backed by the
  `revocation_events` / `revocation_transitions` / `quarantine` / `waivers` /
  `derivations` / `artifacts` tables that already existed in the schema
  (from an earlier, never-wired scaffolding pass) — no new migration was
  required. `RevocationEvent.payload_json` is the sole source of truth for a
  domain event, mirroring how Lite mode's own `payload_json` column works;
  `state`/`policy`/`severity` columns are denormalized for indexing only.
  Quarantine release is implemented as `active=False`, not a delete, so
  quarantine history is never silently discarded (an improvement over
  Lite mode's `DELETE`-based release, made possible because the Service
  schema already had an `active` flag).
- `skillrewind.revocation.attestation_service`: projects the reused
  pipeline's results (`event.rebuilt`, `event.unresolved`) into the
  dedicated, independently-queryable `rebuild_plans` / `rebuild_attempts` /
  `verification_reports` / `attestations` tables, idempotently (checked by
  natural key before insert), so each has a stable resource ID reachable
  from its own API endpoint without a second orchestration layer.
- `skillrewind.revocation.service.build_revocation_preview`: a new,
  side-effect-free function (not present in Lite mode) computing the
  balanced/strict/forensic target set, evidence breakdown, and a
  deterministic preview digest from already-existing recorded closure and
  `hidden-influence` edges — never recomputes candidate recovery or replay.
- API routers `revocations.py` / `quarantine.py` / `waivers.py` /
  `attestations.py` under `/api/v1`, and an extended `revocation.execute`
  job handler that branches on payload shape: `{database_url, cas_root,
  event_id}` builds a `ServiceWorkspace`; `{workspace_dir, event_id}` (the
  pre-existing shape) still opens a Lite `Workspace`. Same job kind, same
  `run_revocation` call — this is the "don't fragment job kinds" reuse
  boundary applied to the worker layer too.

## Consequences
- Barrier-first, forensic/balanced/strict policy semantics, and the
  replay-confirmation-does-not-imply-revocation invariant are identical in
  both modes by construction (same code path), not independently
  re-verified.
- A bug found and fixed during this work — forensic-mode revocation was
  appending to `event.quarantined` even though it never called
  `quarantine_artifact` — was a latent defect in the *shared* Lite logic,
  invisible until Service-mode tests exercised the forensic-mode invariant
  directly (no prior Lite test asserted that field for forensic mode). Fixing
  it benefits both modes.
- `GET /api/v1/artifacts/{id}/resolve`'s status-priority order was corrected:
  `superseded` is now checked before `quarantined`, since a superseded
  artifact's original quarantine-history entry is deliberately preserved
  (never deleted) and must not be mistaken for an active block.
- Waivers with `scope != "quarantine-release"` leave the underlying
  quarantine record active and instead surface via the resolver's
  `allowed-by-waiver` branch — a real, distinct, expiry-bounded overlay,
  not a one-time state mutation. The default `scope="quarantine-release"`
  waiver (reused unchanged from Lite) still performs an immediate release;
  building a fully continuous "waiver overlay for every resolution path"
  semantics for *all* scopes is a reduced-depth item — see the completion
  matrix.
- A signed attestation, a passed verification suite, and a clean-room
  rebuild are all bounded exactly as Lite mode already documents them: not
  proof of foundation-model weight erasure, not universal absence of
  influence outside the declared replay/verification boundary. Nothing in
  this phase changes those non-claims.

## Alternatives considered
- **Reimplement revocation orchestration against Service-mode repositories
  directly** (the Phase C2.2 replay pattern). Rejected: replay's science
  functions were pure and stateless per-repetition, which made a from-
  scratch checkpoint-resumable rewrite tractable and worthwhile. Revocation's
  orchestration is much larger (barrier, quarantine, rebuild planning,
  execution, verification, publication, attestation) and was *already*
  checkpoint-resumable; rewriting it would have been strictly more code with
  a real risk of behavioral drift between the two modes.
- **A thin translation layer that copies data between a real Lite workspace
  and Service-mode tables.** Rejected: this is exactly the "secretly
  operates on a separate Lite-mode workspace" anti-pattern the milestone
  explicitly forbids.
