"""Replay runner abstraction and the approved-runner registry.

Runners are never selected by an arbitrary user-controlled module path (that
would allow an API caller to make the service import and execute arbitrary
code). Instead, callers pass a short registry key (e.g. ``"deterministic-fixture"``)
that is resolved against :data:`APPROVED_RUNNERS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ..domain.enums import InterventionKind
from ..domain.errors import UnapprovedRunnerError


@dataclass(frozen=True, slots=True)
class ReplaySpec:
    target_derivation_id: str
    recipe: str
    task_snapshot: dict[str, Any]
    available_context: frozenset[str]
    candidate_ancestor_id: str
    intervention_kind: InterventionKind
    seed: Optional[int] = None
    repetition_index: int = 0


@dataclass(frozen=True, slots=True)
class RunnerOutput:
    outputs: dict[str, Any]
    target_behavior_vector: dict[str, Any]
    utility_metrics: dict[str, Any] = field(default_factory=dict)
    fidelity_components: dict[str, float] = field(default_factory=dict)
    logs: tuple[str, ...] = field(default_factory=tuple)
    error: Optional[str] = None
    model_identity: Optional[str] = None
    environment_identity: Optional[str] = None


class DerivationRunner(Protocol):
    name: str

    def run(self, spec: ReplaySpec) -> RunnerOutput: ...


_REGISTRY: dict[str, DerivationRunner] = {}


def register_runner(runner: DerivationRunner) -> None:
    _REGISTRY[runner.name] = runner


def get_runner(name: str) -> DerivationRunner:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnapprovedRunnerError(
            f"runner {name!r} is not in the approved registry: {sorted(_REGISTRY)}"
        ) from exc


def approved_runner_names() -> list[str]:
    return sorted(_REGISTRY)
