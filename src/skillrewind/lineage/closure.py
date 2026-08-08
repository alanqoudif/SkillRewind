"""Recorded-closure helpers over a workspace."""

from __future__ import annotations

from typing import Sequence

from ..workspace import Workspace
from .graph import LineageGraph


def build_graph(workspace: Workspace) -> LineageGraph:
    """Build the graph used for *recorded* closure/ancestry: only edges with
    ``evidence_class == "recorded"`` are included. Inferred, replay-confirmed,
    rejected, or unresolved edges must never silently expand what counts as
    "recorded" -- that would defeat the exact/deterministic guarantee of
    recorded closure and let unconfirmed hidden-lineage inference leak into
    barrier/serving decisions that are supposed to be based on hard evidence
    only. Use :mod:`skillrewind.lineage.candidates` / the full edge repository
    directly to inspect inferred or replay-tested edges."""

    return LineageGraph(workspace.edges.list_all(status="active", evidence_class="recorded"))


def recorded_descendants(
    workspace: Workspace, roots: Sequence[str], *, include_roots: bool = True
) -> tuple[str, ...]:
    return build_graph(workspace).descendants(roots, include_roots=include_roots)


def recorded_ancestors(
    workspace: Workspace, roots: Sequence[str], *, include_roots: bool = True
) -> tuple[str, ...]:
    return build_graph(workspace).ancestors(roots, include_roots=include_roots)
