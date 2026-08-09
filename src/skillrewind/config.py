"""Typed configuration with documented precedence.

Precedence (highest to lowest): CLI flags > ``SKILLREWIND_*`` environment
variables > ``skillrewind.toml`` project config > built-in safe defaults.
Service-mode settings (Postgres URL, auth, CORS origins, OpenTelemetry
exporters) are represented here for forward compatibility but are not wired
to a running service in this session — see ``STATUS.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

_ENV_PREFIX = "SKILLREWIND_"


@dataclass(slots=True)
class FeatureWeights:
    expression: float = 0.20
    implementation: float = 0.25
    operational: float = 0.20
    behavioral: float = 0.20
    graph: float = 0.10
    temporal: float = 0.05


@dataclass(slots=True)
class ScoringThresholds:
    candidate: float = 0.35
    high_confidence: float = 0.80


@dataclass(slots=True)
class ReplayFidelityConfig:
    minimum_confirmed: float = 0.70
    minimum_rejected: float = 0.70


@dataclass(slots=True)
class SkillRewindConfig:
    mode: str = "lite"
    workspace_dir: str = ".skillrewind"
    database_url: str = ""  # empty => derive sqlite path from workspace_dir
    cas_root: str = ""  # empty => derive from workspace_dir
    max_object_bytes: int = 200 * 1024 * 1024
    actor: str = "local-user"
    feature_weights: FeatureWeights = field(default_factory=FeatureWeights)
    thresholds: ScoringThresholds = field(default_factory=ScoringThresholds)
    replay_fidelity: ReplayFidelityConfig = field(default_factory=ReplayFidelityConfig)
    default_replay_budget_calls: int = 20
    strict_forbids_waivers: bool = True
    balanced_quarantine_unresolved_high_severity: bool = False
    # Service-mode API settings (Phase C). api_auth_disabled must only ever be
    # set true in an explicit Lite/dev deployment bound to a safe interface --
    # /health/ready fails Service mode if it is true (master spec 8.5).
    api_auth_disabled: bool = False
    cors_allow_origins: str = ""  # comma-separated; empty = no CORS origins allowed
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0

    @property
    def db_path(self) -> str:
        if self.database_url:
            return self.database_url
        return str(Path(self.workspace_dir) / "skillrewind.db")

    @property
    def resolved_cas_root(self) -> str:
        if self.cas_root:
            return self.cas_root
        return str(Path(self.workspace_dir) / "cas")


def _apply_env(config: SkillRewindConfig) -> None:
    for f in fields(config):
        env_name = f"{_ENV_PREFIX}{f.name.upper()}"
        if env_name in os.environ:
            raw = os.environ[env_name]
            current = getattr(config, f.name)
            if isinstance(current, bool):
                setattr(config, f.name, raw.lower() in {"1", "true", "yes", "on"})
            elif isinstance(current, int):
                setattr(config, f.name, int(raw))
            elif isinstance(current, float):
                setattr(config, f.name, float(raw))
            elif isinstance(current, str):
                setattr(config, f.name, raw)


def _apply_toml(config: SkillRewindConfig, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "feature_weights" and isinstance(value, dict):
            for wk, wv in value.items():
                if hasattr(config.feature_weights, wk):
                    setattr(config.feature_weights, wk, float(wv))
        elif key == "thresholds" and isinstance(value, dict):
            for tk, tv in value.items():
                if hasattr(config.thresholds, tk):
                    setattr(config.thresholds, tk, float(tv))
        elif key == "replay_fidelity" and isinstance(value, dict):
            for rk, rv in value.items():
                if hasattr(config.replay_fidelity, rk):
                    setattr(config.replay_fidelity, rk, float(rv))
        elif hasattr(config, key):
            setattr(config, key, value)


def load_config(
    *, project_config_path: str | Path = "skillrewind.toml", overrides: dict[str, Any] | None = None
) -> SkillRewindConfig:
    config = SkillRewindConfig()
    path = Path(project_config_path)
    if path.is_file():
        with path.open("rb") as handle:
            _apply_toml(config, tomllib.load(handle))
    _apply_env(config)
    if overrides:
        for key, value in overrides.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)
    return config
