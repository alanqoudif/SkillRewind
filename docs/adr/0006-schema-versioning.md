# ADR 0006: Schema versioning and backward compatibility

## Status
Accepted (v0.2.0)

## Context
The v0.1 research starter already had committed schemas (`spec/*.schema.json`), example artifacts, and a working CLI (`skillrewind closure --edges ...`, `skillrewind attest --edges ...`). Breaking these silently while building v0.2 would violate the explicit backward-compatibility requirement.

## Decision
- `src/skillrewind/graph.py` (v0.1 recorded-lineage graph) and `src/skillrewind/attestation/legacy.py` (v0.1 `recorded_attestation`) are preserved byte-for-byte in behavior; `tests/test_graph.py` (the original v0.1 test file) still runs unmodified and still passes.
- The v0.1 CLI commands are preserved as dual-mode branches inside the v0.2 command names: `skillrewind closure --edges <file> --root <id>` still works exactly as before (detected by the presence of `--edges` and routed to the legacy graph/CLI code path), and `skillrewind attest --edges <file> --root <id> --output <file>` is a distinct, still-supported subcommand.
- New v0.2 persistence uses `schema_version` in a `schema_meta` table (`skillrewind.persistence.database.SCHEMA_VERSION`), additive-only so far; no destructive migration has been needed yet.
- New artifact IDs use the strict `scheme://name@sha256:<64-hex>` form; the v0.1 example IDs (e.g. `skill://fast-http-deploy@0.1.0`) remain valid only through the `allow_legacy=True` path, never produced by v0.2 ingestion.

## Consequences
Two nearly-identical concepts (v0.1 `Edge`/`RecordedLineageGraph` vs. v0.2 `InfluenceEdge`/`LineageGraph`) coexist in the codebase. This is intentional: converting v0.1's frozen, tested baseline into the new evidence-typed model risked silently changing its documented behavior. The overlap is confined to two small, clearly-labeled modules (`graph.py`, `attestation/legacy.py`).
