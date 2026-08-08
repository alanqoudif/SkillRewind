"""SkillRewind research starter.

Version 0.1 implements only deterministic traversal of recorded lineage.
Hidden-lineage inference, replay, quarantine, and rebuilding are research
milestones and are intentionally not simulated here.
"""

from .graph import Edge, LineageFormatError, RecordedLineageGraph, load_edges

__all__ = ["Edge", "LineageFormatError", "RecordedLineageGraph", "load_edges"]
__version__ = "0.1.0"
