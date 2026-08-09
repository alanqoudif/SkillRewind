# SkillRewind v0.3 Core Release Readiness Report

**As of:** Phase C2.4 (API freeze + integration contract), commit on branch `feat/v0.3-completion`. Supersedes
the older, now-stale `docs/completion-matrix-v0.3.md` (written before Phase C2's Service-mode API existed).

Statuses: **complete** / **partial** / **blocked** / **not-started**. A component is only marked complete when
it is real, tested, and verified in this session — not when it merely exists.

| Component | Status | Evidence | Blocker | Required before v0.4 |
|---|---|---|---|---|
| Lite core (CLI, SQLite workspace, CAS, audit) | complete | `src/skillrewind/workspace.py`, `tests/unit/`, `make demo` passes | — | — |
| Service persistence (SQLAlchemy models/repositories) | complete | `src/skillrewind/persistence/service/`, exercised by all `tests/integration/test_service_*.py` | — | — |
| Migrations (Alembic, SQLite-validated) | complete | 3 migrations apply cleanly against SQLite in every integration test fixture; packaged as data inside `skillrewind.persistence.service.migrations` (see `pyproject.toml`'s `[tool.setuptools.package-data]`) and driven via `importlib.resources` in `skillrewind.persistence.service.migrations_runtime`; `skillrewind db-upgrade`/`db-current` and `tests/smoke/clean_install_smoke.py` prove real Alembic history applies from a pip-installed wheel with no repo checkout | — | — |
| CAS (content-addressed store) | complete | digest verification, corruption detection tested (`tests/unit/test_cas*.py` family) | — | — |
| Auth (API keys, scopes, Argon2id hashing) | complete | `src/skillrewind/api/auth.py`, wrong-scope/revoked-key tests in `tests/integration/test_service_revocation_extras.py` and this milestone's new tests | — | — |
| Durable worker / job queue | complete | `src/skillrewind/jobs/`, crash/resume test in `tests/integration/test_service_revocation_extras.py::test_service_mode_crash_mid_rebuild_then_resume_no_duplicates` | — | — |
| Artifacts (ingest/get/content/resolve) | complete | full round trip in `tests/integration/test_service_revocation_e2e.py` and `tests/integration/test_conformance_self_test.py` | — | — |
| Derivations | complete | same as above | — | — |
| Lineage (ancestors/descendants/graph/cycles) | complete | recorded-closure-excludes-inferred/replay-confirmed invariants tested | — | — |
| Candidate recovery | complete | async job + API tested end to end | — | — |
| Replay (deterministic + sandboxed-subprocess) | complete for what's implemented | confirmed/rejected/unresolved paths tested; no Docker-isolated runner exists (see `docs/threat-model.md`) | Docker-isolated runner not implemented | Real network-isolated replay runner if untrusted replay content is ever accepted |
| Revocation (barrier-first, forensic/balanced/strict) | complete | `tests/integration/test_service_revocation_e2e.py`, `test_service_revocation_extras.py` | — | — |
| Quarantine | complete | history never deleted (only deactivated), tested | — | — |
| Waivers | complete (Phase C2.4 gap C fixed this milestone) | policy-overlay semantics (no evidence/status mutation), scope enforcement, expiry, explicit revocation, restart persistence, concurrent resolution, rebuild-support-scope interaction all tested in `tests/integration/test_waiver_semantics_c24.py` + `tests/unit/test_quarantine_waivers.py` | — | — |
| Rebuild (clean-room) | complete | plan/execute/publish tested; standalone read API added this milestone | — | — |
| Verification | complete | suite execution + standalone read API (safety/utility/integrity never collapsed to one boolean) added this milestone | — | — |
| Resolution (serving gate) | complete | dynamic waiver evaluation, revoked/quarantined/superseded/allowed-by-waiver/unavailable all tested | — | — |
| Attestations | complete | build/render/sign/verify, tamper detection tested | — | — |
| Audit chain | complete | hash-chain verify tested | No HTTP endpoint exposes the audit log directly in this milestone | Consider a `GET /api/v1/audit` read endpoint if external consumers need it over HTTP rather than CLI-only |
| API contract (stable-v1 surface + freeze doc) | complete | `docs/api-stability-v1.md`, `docs/integration-contract-v1.md` | — | — |
| OpenAPI export | complete | `make openapi`, `docs/openapi-v1.json` generated from the live app, staleness-checked by `tests/unit/test_openapi_not_stale.py` | — | — |
| SSE (job events, resumable) | complete | real HTTP resume test against a genuine socket in `tests/integration/test_sse_resume_http.py` (proved `httpx.ASGITransport`/`TestClient` cannot exercise this — see that test's module docstring) | SSE today streams raw job-queue event names, not yet the canonical `event_type` vocabulary in `docs/event-contract-v1.md` | Re-map SSE payloads to the canonical event envelope |
| Event contract (canonical envelope) | partial | `docs/event-contract-v1.md` defines the envelope and type vocabulary | Not yet wired into the live SSE stream (see above); no webhook delivery exists | Wire the canonical envelope into SSE; webhook delivery is separate future work |
| Conformance foundation | complete | `skillrewind.conformance` (describe + self-test), `skillrewind conformance describe`/`self-test` CLI, self-test passes 16/16 checks against the local API | — | — |
| Reference adapter interfaces | complete | `skillrewind.adapters.protocols` (6 Protocols, no internal-class coupling, verified by `tests/unit/test_adapter_protocols.py`) + one deterministic reference adapter | — | — |
| Clean install | complete | `make clean-install-smoke` builds a real wheel, inspects it for the packaged migration files, installs into a fresh `uv`-managed venv, runs real Alembic migrations (`skillrewind db-upgrade`), and runs the full `skillrewind conformance self-test` -- all with no repo `PYTHONPATH` dependency | — | — |
| PostgreSQL runtime | blocked | schema/ORM layer is dialect-portable; migrations + full CRUD paths run against SQLite in every test in this environment | No Docker daemon reachable in this development environment (`docker info` fails to connect) — 2 tests honestly skip with this exact reason | Run the full Service-mode test suite against a real PostgreSQL instance before any production claim |
| Replay isolation | partial | deterministic + weak-subprocess-sandbox runners exist and are documented as such | No network-namespace-isolated (Docker) runner | Build a real isolated runner before accepting untrusted replay content from an external platform |
| SDKs | not-started | reference Python adapter Protocols exist; no packaged Python or TypeScript SDK | — | Explicitly out of scope for this milestone per its own instructions |
| Conformance (external) | not-started | local self-test proves SkillRewind satisfies its own contract; no external-platform conformance harness exists yet | — | Build once a real external adapter (e.g. Mibyan) exists |
| External integration (Mibyan, LangGraph, etc.) | not-started | explicitly out of scope for this milestone | — | Future milestone |
| React dashboard | not-started | explicitly out of scope for this milestone | — | Future milestone |

## Test/quality-gate summary

See the final report delivered alongside this document (or `git log` on this commit) for the exact test counts,
skip reasons, and lint/typecheck output from this milestone's quality-gate run — this table is the structural
readiness summary; it does not duplicate that raw output so the two cannot drift out of sync silently.

## Bugs discovered and fixed this milestone

1. **Waiver was a one-time evidence/state mutation, not a policy overlay** (Phase C2.4 gap C). Creating a
   `"quarantine-release"`-scoped waiver used to permanently flip `artifact.status` to `active` and deactivate
   the quarantine entry. Once the waiver later expired or was explicitly revoked, nothing reverted that state —
   the artifact stayed servable forever. Fixed by making resolution dynamically evaluate active waivers on
   every call instead of caching the decision into persisted state; the quarantine entry and audit history are
   now never mutated by waiver creation. See `src/skillrewind/quarantine/waivers.py`'s module docstring and
   `tests/integration/test_waiver_semantics_c24.py`.
2. **`httpx.ASGITransport` (and therefore Starlette's `TestClient`) cannot test a genuinely open-ended SSE
   stream** — it buffers the entire response body until the ASGI app coroutine returns, so any test against a
   non-terminal job hangs forever. This is a testing-infrastructure finding, not a production bug (the real
   `uvicorn`-served endpoint streams correctly), but it explains why the milestone's instruction to test the
   *real* HTTP endpoint (not just `JobQueue.events()`) mattered: an ASGITransport-based test would have
   silently either hung or been written to avoid the open-ended case entirely, hiding whether real resume
   semantics actually work over the wire. Fixed by testing against a real `uvicorn.Server` on a loopback socket
   (`tests/integration/test_sse_resume_http.py`).
3. **`LocalCAS.get_bytes()`/`open_stream()` never verified the digest of bytes read from disk against the
   requested `digest_hex`** — only the separate, explicitly-invoked `verify_integrity()` (used by
   `skillrewind artifact-verify`) did. This meant `GET /api/v1/artifacts/{id}/content` silently returned
   tampered/corrupted CAS content as a `200 OK` with no integrity check at all on the normal read path,
   directly contradicting `docs/threat-model.md`'s "CAS corruption" protection claim. Fixed by hashing the
   bytes read on every `get_bytes()`/`open_stream()` call and raising `CASIntegrityError` on mismatch; the API
   layer maps that to a `500 Integrity Error` Problem Details response instead of serving the tampered bytes.
   See `src/skillrewind/cas/local.py` and
   `tests/integration/test_safety_invariants_c24.py::test_corrupted_cas_object_surfaces_as_an_error_not_silent_wrong_content`.
4. **`find_by_alias` filtered `status = 'active'` at the SQL level** in both Lite and Service repositories,
   which silently made the waiver-overlay fix above impossible (a quarantined artifact could never be found by
   alias at all, regardless of an active waiver). Broadened to `status IN ('active', 'quarantined')`, with the
   actual eligibility decision left entirely to `resolve_alias`.

## Gap noted (not fixed this milestone): two independent candidate-recovery scorers can disagree

While building `tests/integration/test_public_contract_freeze_e2e.py`, generic-but-plausible artifact content
that `skillrewind.lineage.service_recovery.run_candidate_recovery` (used by the async
`POST /api/v1/lineage/recovery-runs` API) scored as a real candidate was scored *below* the candidate
threshold by `skillrewind.lineage.candidates.recover_candidates` (the separate implementation
`skillrewind.revocation.service.run_revocation` calls internally to re-derive candidates during an actual
revocation). Both are real, tested, non-fabricated scorers -- they are simply two independently written
implementations of the same "hidden-lineage candidate scoring" concept (one Lite-workspace-shaped, one
Service-mode-native), and they can disagree on borderline content. This means a candidate an operator observes
via `GET /api/v1/lineage/recovery-runs/{id}/candidates` is not strictly guaranteed to be the same set
`run_revocation` acts on when a revocation is later submitted for the same root. Reconciling these into one
scorer (or making `run_revocation` reuse already-persisted candidate rows/replay results instead of
re-deriving both from scratch) is real, scoped work for a future milestone -- not attempted here to avoid
destabilizing the tested revocation pipeline this late in the session.

## Answering the milestone's closing question

**Can an unrelated AI platform begin integrating SkillRewind using only the public contract after this
milestone?**

**YES — with the following stated alpha limitations:**

- Level 1 (Audit) and Level 2 (Enforcement) integration are fully supported today: every required endpoint in
  `docs/integration-contract-v1.md` exists, is `stable-v1`-categorized in `docs/api-stability-v1.md`, is
  covered by `docs/openapi-v1.json`, and is exercised end-to-end by `skillrewind conformance self-test`.
- Level 3 (Full Rewind) is also fully supported for the artifact/rebuild/verification/attestation surface. The
  one honest gap: SSE event payloads are not yet re-mapped to the canonical `event_type` vocabulary in
  `docs/event-contract-v1.md` (they currently carry raw job-queue event names) — a platform building an
  `EventConsumer` today should treat the SSE payload shape as provisional until that re-mapping lands.
- A platform that needs a from-scratch, pip-installed deployment (not this repository's checkout) can install
  and run the CLI/Lite/Service-mode Python API today, including real Alembic migrations via
  `skillrewind db-upgrade` — no separate `migrations/`/`alembic.ini` checkout is required
  (`make clean-install-smoke` proves this end to end, including the full conformance self-test).
- PostgreSQL itself has not been live-validated in this environment (Docker unavailable) — a platform planning
  a PostgreSQL-backed deployment should validate against a real instance before going further than this
  milestone's SQLite-validated schema/ORM portability review.
- No Python/TypeScript SDK ships yet — integration today is direct HTTP calls against the documented
  `stable-v1` surface, or a hand-written adapter implementing the reference Protocols in
  `skillrewind.adapters.protocols`.

None of these are blockers to *beginning* integration; they are scoped, documented gaps to close before a
production-grade external integration (e.g. Mibyan) is attempted.
