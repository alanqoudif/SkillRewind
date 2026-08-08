"""SkillRewind workspace: the local persistence root used by the CLI and capture SDK.

A workspace bundles: a SQLite database (WAL mode), a local content-addressed
store, and a hash-chained audit log stored in the same database. This is the
"Lite mode" deployment target. There is no service-mode (PostgreSQL/worker)
implementation in this session; see ``STATUS.md``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .audit.log import AuditLog
from .canonical.json import sha256_hex
from .cas.local import LocalCAS
from .config import SkillRewindConfig, load_config
from .domain.enums import ArtifactKind, LifecycleStatus
from .domain.errors import NotFoundError
from .domain.ids import build_artifact_id
from .domain.models import Artifact
from .persistence.database import connect, get_schema_version
from .persistence.repositories import (
    ArtifactRepository,
    DerivationRepository,
    EdgeRepository,
    ReplayRepository,
    RevocationRepository,
    WaiverRepository,
)

_SCHEME_BY_KIND = {
    ArtifactKind.AGENT_SKILL: "skill",
    ArtifactKind.MEMORY: "memory",
    ArtifactKind.TRAJECTORY: "trace",
    ArtifactKind.PROMPT_PATCH: "patch",
    ArtifactKind.SOURCE_CODE: "code",
    ArtifactKind.CONFIGURATION: "config",
    ArtifactKind.TEMPLATE: "template",
    ArtifactKind.WORKFLOW: "workflow",
    ArtifactKind.TOOL_RESULT: "tool-result",
    ArtifactKind.TASK_SNAPSHOT: "task",
    ArtifactKind.WORKSPACE_SNAPSHOT: "workspace",
    ArtifactKind.ENVIRONMENT_SNAPSHOT: "env",
    ArtifactKind.VALIDATION_REPORT: "validation",
    ArtifactKind.REPLAY_RUN: "replay",
    ArtifactKind.ATTESTATION: "attestation",
    ArtifactKind.OTHER: "artifact",
}


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class Workspace:
    def __init__(self, config: SkillRewindConfig, connection: sqlite3.Connection) -> None:
        self.config = config
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.cas = LocalCAS(config.resolved_cas_root, max_object_bytes=config.max_object_bytes)
        self.audit = AuditLog(self.conn)
        self.artifacts = ArtifactRepository(self.conn)
        self.derivations = DerivationRepository(self.conn)
        self.edges = EdgeRepository(self.conn)
        self.replays = ReplayRepository(self.conn)
        self.revocations = RevocationRepository(self.conn)
        self.waivers = WaiverRepository(self.conn)

    @classmethod
    def init(cls, workspace_dir: str | Path, *, config: Optional[SkillRewindConfig] = None) -> "Workspace":
        workspace_dir = Path(workspace_dir)
        cfg = config or load_config()
        cfg.workspace_dir = str(workspace_dir)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        conn = connect(cfg.db_path)
        ws = cls(cfg, conn)
        ws.audit.append("workspace.initialized", cfg.actor, {"workspace_dir": str(workspace_dir)})
        return ws

    @classmethod
    def open(cls, workspace_dir: str | Path, *, config: Optional[SkillRewindConfig] = None) -> "Workspace":
        workspace_dir = Path(workspace_dir)
        if not workspace_dir.is_dir():
            raise NotFoundError(f"workspace not found: {workspace_dir}")
        cfg = config or load_config()
        cfg.workspace_dir = str(workspace_dir)
        conn = connect(cfg.db_path)
        return cls(cfg, conn)

    @property
    def schema_version(self) -> str | None:
        return get_schema_version(self.conn)

    def ingest_artifact(
        self,
        content: bytes,
        *,
        kind: ArtifactKind,
        logical_name: str,
        mime_type: str = "application/octet-stream",
        creator: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        alias: Optional[str] = None,
    ) -> Artifact:
        """Ingest immutable content into CAS and register an artifact record."""

        obj = self.cas.put_bytes(content)
        scheme = _SCHEME_BY_KIND.get(kind, "artifact")
        artifact_id = build_artifact_id(scheme, logical_name, obj.digest_hex)
        artifact = Artifact(
            artifact_id=artifact_id,
            digest_hex=obj.digest_hex,
            kind=kind,
            logical_name=logical_name,
            mime_type=mime_type,
            byte_size=obj.size_bytes,
            created_at=timestamp(),
            storage_ref=f"cas://{obj.digest_hex}",
            creator=creator,
            metadata=metadata or {},
            alias=alias,
        )
        self.artifacts.upsert(artifact)
        self.audit.append(
            "artifact.ingested",
            creator or self.config.actor,
            {"artifact_id": artifact_id, "kind": kind.value, "byte_size": obj.size_bytes},
        )
        return artifact

    def resolve_alias(self, alias: str) -> Optional[Artifact]:
        """Serving-resolution lookup: only ever returns an active artifact, never
        a revoked or quarantined version."""

        artifact = self.artifacts.find_by_alias(alias)
        if artifact is None:
            return None
        if artifact.status != LifecycleStatus.ACTIVE:
            return None
        if self.revocations.is_quarantined(artifact.artifact_id):
            return None
        return artifact

    def close(self) -> None:
        self.conn.close()
