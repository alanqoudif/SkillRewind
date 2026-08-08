"""Recorded-closure helpers over a workspace."""

from __future__ import annotations

from typing import Sequence

from ..workspace import Workspace
from .graph import LineageGraph


def build_graph(workspace: Workspace) -> LineageGraph:
    return LineageGraph(workspace.edges.list_all(status="active"))


def recorded_descendants(
    workspace: Workspace, roots: Sequence[str], *, include_roots: bool = True
) -> tuple[str, ...]:
    return build_graph(workspace).descendants(roots, include_roots=include_roots)


def recorded_ancestors(
    workspace: Workspace, roots: Sequence[str], *, include_roots: bool = True
) -> tuple[str, ...]:
    return build_graph(workspace).ancestors(roots, include_roots=include_roots)
