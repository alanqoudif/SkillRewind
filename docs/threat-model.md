# SkillRewind Threat Model

**Status:** v0.3 alpha. This document defines the trust boundaries assumed by SkillRewind's Service mode and
what SkillRewind does and does not protect against. Read alongside `SECURITY.md` (the reporting policy and a
summary of the same limitations).

## Actors

### Trusted: Service-mode administrator

Whoever runs `skillrewind serve` / owns the database and CAS root. Holds (or can mint) `admin`-scoped API keys,
controls the signing keypair (`skillrewind attest-keygen`), and controls the database connection string. **Full
trust** — SkillRewind does not defend against a malicious administrator; they already have direct database and
filesystem access that supersedes any API-layer control.

### Untrusted: API callers

Anything presenting a bearer API key over `/api/v1/*`. Scoped (`ingest`, `read`, `replay`, `revoke`, `waive`,
`admin`) per `skillrewind.api.deps.require_scope`; a caller can only do what its key's scopes allow. Revoked
keys (`ApiKey.status == "revoked"`) are rejected on every request — there is no cached-validity window (see
`skillrewind.api.auth.authenticate`, which re-queries the database per request).

### Untrusted: artifact content

Anything ingested via `POST /api/v1/artifacts` (raw bytes) or `skillrewind artifact-ingest-skill` (an Agent
Skills directory, including `SKILL.md` frontmatter). SkillRewind treats this as data, never as instructions to
itself: YAML frontmatter is parsed with `yaml.safe_load`, directory ingestion is path-traversal/symlink/zip-slip
hardened (`skillrewind.adapters.agent_skills`), and content is only ever hashed/stored/compared, never executed,
by the ingestion path itself.

### Untrusted: replay content

Task snapshots, recipes, and probe/fixture output involved in a replay run. The **only** two runners that exist
are `DeterministicFixtureRunner` (looks up a pre-registered, allowlisted in-process Python callable by recipe
name — no code from the artifact content itself is ever executed) and `SandboxedSubprocessRunner` (spawns an
allowlisted fixture script as a subprocess with sanitized environment and POSIX rlimits). Neither runner
executes arbitrary code *from* an artifact's own content; both are keyed by a fixed, allowlisted recipe
identifier registered in trusted code, not by anything derived from ingested content.

### Trusted vs. untrusted runner boundary

- `DeterministicFixtureRunner`: trusted. Runs in-process, no subprocess, no filesystem/network access beyond
  what the registered Python callable itself does (and those callables are test/demo fixtures written by the
  repository, never derived from ingested artifact content).
- `SandboxedSubprocessRunner`: **weaker than a container.** It is not a security sandbox in the sense of
  network-namespace isolation — see [Replay isolation limitations](#replay-isolation-limitations) below. Treat
  it as "reduced blast radius" (CPU/memory/process limits, sanitized environment), not "untrusted-code safe."

## What SkillRewind protects against

- **CAS corruption / digest mismatch.** Every object is content-addressed by SHA-256; retrieval verifies the
  digest and raises rather than silently returning tampered content (`skillrewind.cas.local.LocalCAS`).
  Corrupted or missing CAS objects surface as explicit errors up through the artifact-retrieval API, never a
  fabricated empty response.
- **Audit-log tampering.** The audit log is hash-chained (`skillrewind.audit`); `skillrewind audit-verify` /
  `AuditRepository.verify()` detects any row whose content or chain linkage has been altered after the fact.
  This detects tampering; it does not prevent someone with direct database access from rewriting the entire
  chain consistently (that requires the "trusted administrator" assumption above, or a database with its own
  write-once/append-only guarantees, which SkillRewind does not itself provide).
- **Attestation tampering.** Every attestation carries a canonical-JSON content digest; an optional Ed25519
  signature covers that digest. `verify_attestation()` independently recomputes the digest from the persisted
  payload and (if a signature is present) verifies it against the public key — both digest and signature
  tampering are detected (`tests/integration/test_service_revocation_e2e.py`'s tamper-detection assertions).
- **Confused-deputy risks in resolution.** `GET /api/v1/artifacts/{id}/resolve` is side-effect-free and never
  grants access based on anything other than the artifact's own persisted status, active quarantine entry, and
  a dynamically-evaluated, correctly-scoped, unexpired, unrevoked waiver (see `docs/integration-contract-v1.md`
  §1.3 and `skillrewind.quarantine.waivers`). It never trusts a caller-supplied resolution/status value, and a
  waiver never silently upgrades evidence class (`inferred` → `confirmed`, `confirmed` → `rejected`, or
  `revoked` → `clean`) — see `tests/integration/test_waiver_semantics_c24.py` and
  `tests/unit/test_quarantine_waivers.py`.
- **Revoked-key confused deputy.** A revoked API key cannot authenticate a subsequent request (checked fresh,
  every request, against the database) — see [Actors](#untrusted-api-callers) above.
- **Waiver abuse.** A waiver is a policy overlay, not a mutation of recorded/inferred/replay evidence (Phase
  C2.4 gap C fix — see `src/skillrewind/quarantine/waivers.py`'s module docstring). It is scoped
  (`serving`/`quarantine-release`/`quarantine` vs. `rebuild-support`), time-bounded by default (strict-mode
  configuration can forbid waivers outright), individually revocable, and never applies to a directly revoked
  root by default. Expiry and explicit revocation take effect immediately and automatically, with no separate
  "undo" step, because resolution is evaluated dynamically on every call rather than cached into persisted
  state.

## What SkillRewind does not protect against

- **A malicious or compromised Service-mode administrator.** Direct database/filesystem/signing-key access is
  full trust by design (see [Actors](#trusted-service-mode-administrator)).
- **Arbitrary-code execution safety for untrusted replay content.** See
  [Replay isolation limitations](#replay-isolation-limitations) — do not route untrusted, attacker-controlled
  recipes through `SandboxedSubprocessRunner` expecting container-grade isolation.
- **Model unlearning.** SkillRewind revokes/rebuilds artifacts in its own store; it has no mechanism to affect
  a foundation model's parameters, gradients, or any training-time state. Every attestation's `bounded_claims`
  says so explicitly.
- **Guaranteed causal attribution.** `inferred`-evidence-class edges come from explainable static multi-trace
  scoring, not a proof of causation. Only `replay-confirmed` edges have been tested via a controlled
  intervention, and even that is bounded by the declared replay boundary (see
  `skillrewind.verification.suites.VerificationReport.limitations`, which is always populated, never empty).
- **Denial of service / resource exhaustion beyond the built-in rate limiter.** `skillrewind.api.ratelimit
  .RateLimiter` provides basic per-identity request throttling; it is not a substitute for a real
  edge/WAF-level DoS defense in a production deployment.
- **Confidentiality of artifact content from any caller with a valid `read`-scoped key.** SkillRewind does not
  implement per-artifact ACLs beyond scope-based API-key permissions in this milestone — any `read`-scoped key
  can read any artifact's content and metadata. Cross-tenant isolation (multiple untrusting platforms sharing
  one SkillRewind instance) is not implemented; deploy one instance per trust boundary until it is.

## Replay isolation limitations

`SandboxedSubprocessRunner` (`skillrewind.replay.sandbox`) does **not** provide network-namespace isolation.
It:

- sanitizes the subprocess environment (does not inherit the parent's full environment/secrets);
- applies POSIX rlimits (CPU time, memory, process count) where the OS supports them;
- only ever runs an allowlisted fixture script identified by a fixed recipe name registered in trusted code —
  never a script path or command derived from ingested artifact content.

It does **not**:

- block outbound network access — a process inside it can still make network calls if the OS/network policy
  permits;
- provide filesystem isolation beyond normal OS user permissions;
- provide a security boundary equivalent to a container (no cgroup/namespace isolation), let alone a VM.

There is no Docker-based, network-off replay runner in this release. Do not treat `SandboxedSubprocessRunner`
as safe for attacker-controlled code; it exists to reduce blast radius for trusted-but-imperfect fixture
scripts, not to sandbox untrusted code.

## PostgreSQL runtime validation status

Service mode's schema and ORM layer are written to be dialect-portable (SQLite and PostgreSQL both pass through
the same SQLAlchemy models and Alembic migrations) and are exercised against SQLite in this repository's own
test suite, because no Docker daemon is reachable in this development environment (`docker info` fails to
connect to the socket). Two integration tests are explicit, honest skips for this reason:
`tests/integration/test_service_persistence.py::test_postgres_migration_and_schema_current` and its
sibling in `tests/integration/test_service_replay_job_handler.py`. **This means PostgreSQL has not been
live-validated as of this milestone** — only its schema/migration compatibility has been reviewed by code
inspection and by running the identical migration chain against SQLite. Validate against a real PostgreSQL
instance before depending on the PostgreSQL path in production; see `docs/release-readiness-v0.3.md`.
