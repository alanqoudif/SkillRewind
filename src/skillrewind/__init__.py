"""SkillRewind: recovering hidden influence lineage for verified revocation
in self-evolving LLM agents.

Version 0.2.0 (Research Preview) implements a real, tested, fully offline
deterministic vertical slice: ingestion, capture, recorded-lineage closure,
hidden-lineage candidate recovery, paired counterfactual replay, a
barrier-first revocation state machine, quarantine/waivers, clean-room
rebuild, verification, and bounded/signable attestations. See STATUS.md for
the exact implemented/not-implemented boundary (no service-mode API,
worker, PostgreSQL, web dashboard, or Docker/CI in this release).

The v0.1 recorded-lineage-only baseline (``graph.py``,
``attestation/legacy.py``) is preserved unmodified for backward
compatibility.
"""

from .graph import Edge, LineageFormatError, RecordedLineageGraph, load_edges

__all__ = ["Edge", "LineageFormatError", "RecordedLineageGraph", "load_edges"]
__version__ = "0.2.0"
