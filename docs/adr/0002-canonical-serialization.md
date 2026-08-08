# ADR 0002: Canonical serialization and SHA-256 artifact IDs

## Status
Accepted (v0.2.0)

## Context
Hashing, IDs, audit-log entries, and attestations all need a byte-identical serialization of the same logical object, regardless of dict insertion order or float formatting.

## Decision
`skillrewind.canonical.json` defines a project-specific canonical JSON profile (UTF-8, sorted keys, minimal separators, no NaN/Infinity, enums via `.value`, bytes rejected in favor of digest references) — not a claim of conformance to JCS/RFC 8785 or any other external standard. `canonical_hash`/`sha256_hex` are the only hashing entry points used across CAS, the audit log, and attestations, so a single bug fix or profile change propagates everywhere consistently.

Artifact IDs follow `scheme://logical-name@sha256:<64-hex>` (`skillrewind.domain.ids`), with a `allow_legacy=True` compatibility mode for the v0.1 example IDs (`skill://name@0.1.0`-style), never used by v0.2+ ingestion paths.

## Consequences
A real bug was caught by this discipline during RewindBench-core implementation: oracle case artifact IDs were initially computed from `f"{name}:{seed}:{index}"`, unrelated to the actual content-addressed ID that `Workspace.ingest_artifact` computes when materializing the same artifact into a real workspace (`json.dumps({"name":..., "body":...})` → SHA-256). Every prediction/oracle comparison silently failed (0 recall across the board) until the oracle generator was changed to compute IDs via the identical serialization the workspace uses. This is now a regression-tested invariant (`tests/unit/test_bench.py::test_oracle_ids_match_materialized_workspace_ids`).
