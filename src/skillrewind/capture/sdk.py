"""Capture SDK: lightweight instrumentation for agent frameworks.

Ergonomics target (see spec section 11)::

    from skillrewind.capture import SkillRewindClient

    client = SkillRewindClient.from_config()
    with client.derivation(
        recipe="skill-distillation", recipe_version="0.2",
        logical_name="deploy-service", task_snapshot=task,
        model={"provider": "local", "id": "deterministic-fixture"},
    ) as run:
        run.record_input(skill_id, relation="used-as-input")
        run.record_context_exposure(other_skill_id)
        run.record_tool_call(name="mock_http", arguments={"url": "local://fixture"})
        run.record_tool_result(name="mock_http", result={"ok": True})
        artifact_id = run.commit_artifact(kind="agent-skill", logical_name="deploy-service",
                                           content=generated_skill_bytes)

Errors inside the ``with`` block create a *failed* derivation record rather
than losing the events recorded before the failure. The client offers no
network writer in this session (an ``HTTP writer`` requires the service-mode
API, which is not implemented) — only the local/offline writer backed by a
:class:`~skillrewind.workspace.Workspace` is available.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..domain.enums import ArtifactKind, EvidenceClass, RelationType
from ..domain.models import Derivation, InfluenceEdge
from ..workspace import Workspace, timestamp
from .redaction import Redactor

_KIND_BY_NAME = {k.value: k for k in ArtifactKind}


class DerivationRun:
    def __init__(
        self,
        client: "SkillRewindClient",
        *,
        recipe: str,
        recipe_version: str,
        logical_name: str,
        task_snapshot: Optional[dict[str, Any]],
        model: Optional[dict[str, Any]],
        seed: Optional[int],
    ) -> None:
        self._client = client
        self._recipe = recipe
        self._recipe_version = recipe_version
        self._logical_name = logical_name
        self._task_snapshot = task_snapshot or {}
        self._model = model or {}
        self._seed = seed
        self.derivation_id = f"deriv-{uuid.uuid4()}"
        self._recorded_inputs: list[tuple[str, str]] = []
        self._context_exposures: list[str] = []
        self._memory_refs: list[str] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._committed_artifact_id: Optional[str] = None
        self._started_at = timestamp()

    def record_input(self, artifact_id: str, *, relation: str = "used-as-input") -> None:
        self._recorded_inputs.append((artifact_id, relation))

    def record_context_exposure(self, artifact_id: str) -> None:
        self._context_exposures.append(artifact_id)

    def record_memory(self, artifact_id: str) -> None:
        self._memory_refs.append(artifact_id)

    def record_tool_call(self, *, name: str, arguments: dict[str, Any]) -> None:
        redacted = self._client.redactor.redact(arguments)
        self._tool_calls.append({"type": "call", "name": name, "arguments": redacted, "at": timestamp()})

    def record_tool_result(self, *, name: str, result: dict[str, Any]) -> None:
        redacted = self._client.redactor.redact(result)
        self._tool_calls.append({"type": "result", "name": name, "result": redacted, "at": timestamp()})

    def commit_artifact(
        self,
        *,
        kind: str,
        logical_name: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        artifact_kind = _KIND_BY_NAME[kind]
        artifact = self._client.workspace.ingest_artifact(
            content,
            kind=artifact_kind,
            logical_name=logical_name,
            mime_type=mime_type,
            creator=self._client.workspace.config.actor,
            metadata=metadata or {},
        )
        self._committed_artifact_id = artifact.artifact_id
        return artifact.artifact_id

    def _finalize(self, *, failed: bool, error: Optional[str]) -> Derivation:
        ended_at = timestamp()
        derivation = Derivation(
            derivation_id=self.derivation_id,
            recipe=self._recipe,
            recipe_version=self._recipe_version,
            target_artifact_id=self._committed_artifact_id,
            recorded_inputs=[a for a, _ in self._recorded_inputs],
            context_exposures=list(self._context_exposures),
            candidate_context_pool=list({*self._context_exposures, *self._memory_refs}),
            model_provider=self._model.get("provider"),
            model_id=self._model.get("id"),
            tool_calls=list(self._tool_calls),
            task_snapshot=self._task_snapshot,
            started_at=self._started_at,
            ended_at=ended_at,
            seed=self._seed,
            status="failed" if failed else "completed",
            replay_limitations=[error] if error else [],
        )
        self._client.workspace.derivations.upsert(derivation)

        if self._committed_artifact_id:
            for source, relation in self._recorded_inputs:
                edge = InfluenceEdge(
                    source=source,
                    target=self._committed_artifact_id,
                    relation=RelationType(relation) if relation in RelationType._value2member_map_ else RelationType.USED_AS_INPUT,
                    evidence_class=EvidenceClass.RECORDED,
                    created_at=ended_at,
                    updated_at=ended_at,
                )
                self._client.workspace.edges.upsert(edge)
            for source in self._context_exposures:
                edge = InfluenceEdge(
                    source=source,
                    target=self._committed_artifact_id,
                    relation=RelationType.CONTEXT_EXPOSURE,
                    evidence_class=EvidenceClass.RECORDED,
                    created_at=ended_at,
                    updated_at=ended_at,
                )
                self._client.workspace.edges.upsert(edge)
            for source in self._memory_refs:
                edge = InfluenceEdge(
                    source=source,
                    target=self._committed_artifact_id,
                    relation=RelationType.MEMORY_REFERENCE,
                    evidence_class=EvidenceClass.RECORDED,
                    created_at=ended_at,
                    updated_at=ended_at,
                )
                self._client.workspace.edges.upsert(edge)

        self._client.workspace.audit.append(
            "derivation.completed" if not failed else "derivation.failed",
            self._client.workspace.config.actor,
            {"derivation_id": self.derivation_id, "target_artifact_id": self._committed_artifact_id},
        )
        return derivation


@dataclass
class SkillRewindClient:
    workspace: Workspace
    redactor: Redactor = field(default_factory=Redactor)

    @classmethod
    def from_config(cls, workspace_dir: str = ".skillrewind") -> "SkillRewindClient":
        return cls(workspace=Workspace.open(workspace_dir))

    def derivation(
        self,
        *,
        recipe: str,
        recipe_version: str,
        logical_name: str,
        task_snapshot: Optional[dict[str, Any]] = None,
        model: Optional[dict[str, Any]] = None,
        seed: Optional[int] = None,
    ) -> "_DerivationContext":
        run = DerivationRun(
            self,
            recipe=recipe,
            recipe_version=recipe_version,
            logical_name=logical_name,
            task_snapshot=task_snapshot,
            model=model,
            seed=seed,
        )
        return _DerivationContext(run)


class _DerivationContext:
    def __init__(self, run: DerivationRun) -> None:
        self._run = run

    def __enter__(self) -> DerivationRun:
        return self._run

    def __exit__(self, exc_type, exc, tb) -> None:
        self._run._finalize(failed=exc_type is not None, error=str(exc) if exc else None)
        return None  # never swallow exceptions
