"""Sandboxed subprocess runner for allowlisted benchmark recipes.

This is a **weaker** sandbox than a real container: it uses a subprocess
with a sanitized environment, a fresh temp working directory, wall-clock and
CPU/memory limits via ``resource.setrlimit`` (POSIX only), and a killed
process tree on timeout. It does not provide real network isolation (no
network namespace) — callers must not treat "network disabled" as anything
stronger than "no proxy/credentials are injected into the environment".
Untrusted, non-allowlisted code must never be executed through this runner;
it is disabled for arbitrary recipes by construction (only entries in
``ALLOWLISTED_RECIPES`` may run).

For real isolation, use the Docker-backed path documented in
``docs/deployment`` (not implemented in this session — see ``STATUS.md``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..domain.enums import InterventionKind
from .base import ReplaySpec, RunnerOutput, register_runner

try:
    import resource

    _HAS_RESOURCE = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAS_RESOURCE = False

#: recipe name -> absolute path of an allowlisted Python fixture script.
#: The script receives one JSON argument on argv[1] with
#: {"task_snapshot": ..., "available_context": [...], "seed": ...} and must
#: print a single JSON object to stdout.
ALLOWLISTED_RECIPES: dict[str, str] = {}


def allowlist_recipe(recipe: str, script_path: str | Path) -> None:
    ALLOWLISTED_RECIPES[recipe] = str(Path(script_path).resolve())


def _limit_resources(cpu_seconds: int, memory_bytes: int) -> Any:
    if not _HAS_RESOURCE:
        return None

    def _apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        except (ValueError, OSError):
            pass

    return _apply


@dataclass
class SandboxedSubprocessRunner:
    name: str = "sandboxed-subprocess"
    timeout_seconds: float = 10.0
    cpu_seconds: int = 5
    memory_bytes: int = 256 * 1024 * 1024

    def run(self, spec: ReplaySpec) -> RunnerOutput:
        script = ALLOWLISTED_RECIPES.get(spec.recipe)
        if script is None:
            return RunnerOutput(
                outputs={},
                target_behavior_vector={},
                error=f"recipe {spec.recipe!r} is not allowlisted for sandboxed execution",
            )

        payload = json.dumps(
            {
                "task_snapshot": spec.task_snapshot,
                "available_context": sorted(spec.available_context),
                "seed": spec.seed,
            }
        )

        sanitized_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "SKILLREWIND_SANDBOX": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }

        with tempfile.TemporaryDirectory(prefix="skillrewind-sandbox-") as workdir:
            try:
                completed = subprocess.run(
                    [sys.executable, script, payload],
                    cwd=workdir,
                    env=sanitized_env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    preexec_fn=_limit_resources(self.cpu_seconds, self.memory_bytes) if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired as exc:
                return RunnerOutput(
                    outputs={}, target_behavior_vector={}, error=f"sandbox timeout after {self.timeout_seconds}s: {exc}"
                )
            except OSError as exc:
                return RunnerOutput(outputs={}, target_behavior_vector={}, error=f"sandbox launch failed: {exc}")

        if completed.returncode != 0:
            return RunnerOutput(
                outputs={},
                target_behavior_vector={},
                error=f"sandbox exited {completed.returncode}: {completed.stderr[-2000:]}",
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            return RunnerOutput(outputs={}, target_behavior_vector={}, error=f"sandbox produced invalid JSON: {exc}")

        behavior_keys = result.get("_behavior_keys", list(result.keys()))
        return RunnerOutput(
            outputs=result,
            target_behavior_vector={k: result[k] for k in behavior_keys if k in result},
            utility_metrics=result.get("_utility", {}),
            fidelity_components={
                "task_snapshot_match": 1.0,
                "model_match": 0.8,  # subprocess env is not byte-identical to the original process
                "environment_match": 0.6,  # no network namespace; weaker isolation than a container
                "seed_support": 1.0 if spec.seed is not None else 0.5,
                "candidate_context_reconstruction": 1.0,
            },
            logs=(completed.stderr[-2000:],) if completed.stderr else (),
            model_identity="sandboxed-subprocess",
            environment_identity="subprocess-no-netns",
        )


register_runner(SandboxedSubprocessRunner())
