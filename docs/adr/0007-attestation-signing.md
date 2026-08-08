# ADR 0007: Attestation signing and bounded claims

## Status
Accepted (v0.2.0)

## Context
An attestation must be independently verifiable (content-digest and, optionally, signature) and must never overstate what was actually proven.

## Decision
- `content_digest` is `sha256_hex` of the canonical JSON of every other field; `skillrewind.attestation.verify.verify_attestation` recomputes and compares it. Any single-byte mutation of any field is detected (tested).
- Signing is local Ed25519 only (`cryptography` library), never a required step. Private keys are generated with `0600` permissions and a printed warning; they are never logged, transmitted, or embedded in the attestation itself.
- `bounded_claims` is generated entirely from persisted `RevocationEvent` state (counts of confirmed/rejected/quarantined/rebuilt/unresolved items) — never a template string asserting success regardless of outcome. It always includes an explicit "no claim of erasure from any foundation model's parameters" sentence.
- Cosign/Sigstore signing of the attestation blob is documented as a future option (this ADR) but not implemented or required by any test.

## Consequences
Verification has two independent, separately-testable failure modes: `digest_valid` (content integrity) and `signature_valid` (authenticity, `None` if unsigned). A caller can distinguish "this attestation was tampered with" from "this attestation was never signed."
