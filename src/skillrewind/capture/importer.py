"""Batch import of generic JSONL derivation traces.

Each JSONL line is an event grouped by ``derivation_id``. Recognized event
types are listed in :mod:`skillrewind.capture.events`. Events are grouped in
file order, converted into a :class:`~skillrewind.domain.models.Derivation`
plus recorded :class:`~skillrewind.domain.models.InfluenceEdge` records for
every ``skill-activated``/``memory-retrieved``/``context-candidate-available``
reference to an existing artifact, and persisted into the workspace. Exact
raw event payloads are preserved verbatim inside the derivation's stored
``task_snapshot``/``tool_calls`` so nothing is silently dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.enums import EvidenceClass, RelationType
from ..domain.errors import LineageFormatError
from ..domain.models import Derivation, InfluenceEdge
from ..workspace import Workspace, timestamp
from .events import (
    ARTIFACT_PRODUCED,
    CONTEXT_CANDIDATE_AVAILABLE,
    MEMORY_RETRIEVED,
    SKILL_ACTIVATED,
    TASK_LOADED,
    TOOL_CALLED,
    TOOL_RETURNED,
)


@dataclass
class ImportSummary:
    derivations_created: int = 0
    edges_created: int = 0
    warnings: list[str] = field(default_factory=list)


def import_jsonl(workspace: Workspace, path: str | Path) -> ImportSummary:
    file_path = Path(path)
    if not file_path.is_file():
        raise LineageFormatError(f"trace file not found: {file_path}")

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LineageFormatError(f"{file_path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(event, dict) or "derivation_id" not in event or "type" not in event:
                raise LineageFormatError(
                    f"{file_path}:{line_number}: event must be an object with 'derivation_id' and 'type'"
                )
            did = event["derivation_id"]
            if did not in groups:
                groups[did] = []
                order.append(did)
            groups[did].append(event)

    summary = ImportSummary()
    for derivation_id in order:
        events = groups[derivation_id]
        summary.edges_created += _import_one(workspace, derivation_id, events, summary)
        summary.derivations_created += 1
    workspace.audit.append(
        "capture.jsonl-imported",
        workspace.config.actor,
        {"path": str(file_path), "derivations": summary.derivations_created, "edges": summary.edges_created},
    )
    return summary


def _import_one(
    workspace: Workspace, derivation_id: str, events: list[dict[str, Any]], summary: ImportSummary
) -> int:
    recipe = "captured-trace"
    recipe_version = "0.1"
    task_snapshot: dict[str, Any] = {}
    tool_calls: list[dict[str, Any]] = []
    context_pool: list[str] = []
    target_artifact_id: str | None = None
    started_at = events[0].get("at") or timestamp()
    ended_at = events[-1].get("at") or started_at

    for event in events:
        etype = event["type"]
        if etype == TASK_LOADED:
            task_snapshot = event.get("task", event.get("payload", {}))
        elif etype in (TOOL_CALLED, TOOL_RETURNED):
            tool_calls.append(event)
        elif etype in (SKILL_ACTIVATED, MEMORY_RETRIEVED, CONTEXT_CANDIDATE_AVAILABLE):
            ref = event.get("artifact_id")
            if isinstance(ref, str):
                context_pool.append(ref)
        elif etype == ARTIFACT_PRODUCED:
            ref = event.get("artifact_id")
            if isinstance(ref, str):
                target_artifact_id = ref

    derivation = Derivation(
        derivation_id=derivation_id,
        recipe=recipe,
        recipe_version=recipe_version,
        target_artifact_id=target_artifact_id,
        context_exposures=list(dict.fromkeys(context_pool)),
        candidate_context_pool=list(dict.fromkeys(context_pool)),
        tool_calls=tool_calls,
        task_snapshot=task_snapshot,
        started_at=started_at,
        ended_at=ended_at,
        status="completed",
    )
    workspace.derivations.upsert(derivation)

    edges_created = 0
    if target_artifact_id:
        for source in dict.fromkeys(context_pool):
            if not workspace_has_artifact(workspace, source):
                summary.warnings.append(f"{derivation_id}: referenced unknown artifact {source}")
                continue
            edge = InfluenceEdge(
                source=source,
                target=target_artifact_id,
                relation=RelationType.CONTEXT_EXPOSURE,
                evidence_class=EvidenceClass.RECORDED,
                created_at=ended_at,
                updated_at=ended_at,
            )
            workspace.edges.upsert(edge)
            edges_created += 1
    return edges_created


def workspace_has_artifact(workspace: Workspace, artifact_id: str) -> bool:
    from ..domain.errors import NotFoundError

    try:
        workspace.artifacts.get(artifact_id)
        return True
    except NotFoundError:
        return False
