# SkillRewind Event Contract v1

**Status:** v0.3 alpha. Defines the canonical event envelope and event-type vocabulary shared across SSE
(`GET /api/v1/events/stream`, implemented today), future webhooks, message-bus publication, and SDK callbacks
(`skillrewind.adapters.protocols.EventConsumer`). SkillRewind does not implement outbound webhook delivery in
this milestone — this document defines the envelope so any of those transports can carry the same event shape
without a redesign later.

## Envelope

Every event, regardless of transport, is a JSON object with this shape:

```json
{
  "event_id": "evt_01J...",
  "event_type": "artifact.revoked",
  "schema_version": "1.0",
  "timestamp": "2026-08-09T12:00:00Z",
  "actor": "reviewer@example.com",
  "correlation_id": "revocation-event-id-or-job-id",
  "resource_type": "artifact",
  "resource_id": "skill://checkout@sha256:...",
  "metadata": { "...bounded, event-specific fields..." }
}
```

| Field | Required | Notes |
|---|---|---|
| `event_id` | yes | Monotonically ordered within a `correlation_id` (the SSE transport uses the persisted job-event `event_id` as `Last-Event-ID`). |
| `event_type` | yes | One of the [event types](#event-types) below. New types may be added; consumers must ignore unknown ones. |
| `schema_version` | yes | Envelope schema version — `"1.0"` for this document. Independent of `docs/api-stability-v1.md`'s API version. |
| `timestamp` | yes | ISO-8601 UTC. |
| `actor` | when known | The authenticated actor that caused the event, when there is one and it is safe to disclose (never a secret/token). Omitted for system-internal transitions with no human actor. |
| `correlation_id` | yes | Ties related events together — typically a `revocation_id` or `job_id`. |
| `resource_type` | yes | `artifact` \| `revocation` \| `rebuild` \| `verification` \| `waiver` \| `attestation` \| `candidate` \| `replay`. |
| `resource_id` | yes | The stable ID of `resource_type`, resolvable via the corresponding `stable-v1` GET endpoint. |
| `metadata` | no | Bounded, event-specific fields (see per-event-type notes below). Never raw artifact bytes, prompts, or secrets — see [What must never appear](#what-must-never-appear-in-a-public-event). |

## Event types

| `event_type` | `resource_type` | Emitted when | Current internal name (audit log) |
|---|---|---|---|
| `artifact.ingested` | `artifact` | An artifact is durably stored. | `artifact.ingested` |
| `derivation.created` | `artifact` | A derivation is recorded (`POST /derivations/{id}/output`). | `derivation.completed` |
| `candidate.recovered` | `candidate` | Candidate recovery finds an inferred hidden-lineage edge. | `candidates.recovered` |
| `replay.completed` | `replay` | A replay run finishes (any verdict). | `replay.completed` |
| `replay.confirmed` | `replay` | A replay run's verdict is `confirmed`. | `replay.completed` (verdict-filtered) |
| `replay.rejected` | `replay` | A replay run's verdict is `rejected`. | `replay.completed` (verdict-filtered) |
| `artifact.revoked` | `artifact` | An artifact's status transitions to `revoked`. | `revocation.finalized` (root-scoped) |
| `artifact.quarantined` | `artifact` | An artifact is quarantined as a revocation descendant. | `artifact.quarantined` |
| `waiver.created` | `waiver` | A waiver is created (policy overlay — see `docs/integration-contract-v1.md` §1.3). | `waiver.created` |
| `waiver.revoked` | `waiver` | A waiver is explicitly revoked. | `waiver.revoked` |
| `rebuild.started` | `rebuild` | A clean-room rebuild plan is executed. | `artifact.rebuilt` (start-of-attempt) |
| `rebuild.completed` | `rebuild` | A rebuild attempt reaches a terminal status (`succeeded` / `failed` / `completed-not-published`). | derived from `rebuild_attempts.status` |
| `verification.completed` | `verification` | A verification report is produced (`pass` / `fail` / `partial`). | derived from `verification_reports.status` |
| `successor.published` | `artifact` | A verified successor is atomically promoted (alias moved, original marked superseded). | `artifact.successor-published` |
| `attestation.created` | `attestation` | An unsigned bounded attestation is built. | recorded via `attestations` table insert |
| `attestation.signed` | `attestation` | An attestation is Ed25519-signed. | recorded via `attestations.signature_json` set |

Events not yet mapped to a distinct SSE/webhook `event_type` today are visible through the corresponding
resource's own GET endpoint and through `skillrewind.audit` (the hash-chained audit log, `GET` not yet exposed
over HTTP in this milestone — see `docs/release-readiness-v0.3.md`).

## What must never appear in a public event

- Raw artifact content bytes.
- Full prompt/task text beyond what the resource's own `stable-v1` GET endpoint would already return to an
  authorized caller.
- API keys, signing keys, or any other secret.
- Unbounded `metadata` — event payloads are deliberately small pointers (`resource_id` + a few scalar fields);
  a consumer that needs the full resource calls the corresponding `GET` endpoint.

## Transports

| Transport | Status |
|---|---|
| SSE (`GET /api/v1/events/stream`) | Implemented (`stable-v1`). Resumable via `Last-Event-ID`; see `tests/integration/test_sse_resume_http.py` for the resume contract proof. Today it streams raw job-queue events (`job.enqueued` / `job.claimed` / `job.succeeded` / ...), not yet re-mapped to the canonical `event_type` vocabulary above — that re-mapping is tracked as a `docs/release-readiness-v0.3.md` gap, not implemented in this milestone. |
| Webhooks | Not implemented. This document defines the envelope so a future webhook delivery system reuses it unchanged. |
| Message bus (e.g. Kafka/SQS bridge) | Not implemented. Same envelope applies. |
| SDK callbacks (`EventConsumer.on_event`) | Reference interface only (`skillrewind.adapters.protocols.EventConsumer`); no SDK package ships in this milestone. |
