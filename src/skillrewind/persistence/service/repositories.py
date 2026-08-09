"""Service-mode domain repositories: real read/write access to the
SQLAlchemy schema in `models.py`, backed by a content-addressed store for
artifact bodies.

This is the first slice of the "Service mode must be able to persist and
retrieve artifact metadata and immutable content digests" requirement
(Phase A2/C2). It intentionally covers only artifacts for now -- evidence
edges, candidates, replay/revocation/rebuild/verification/attestation
Service-mode repositories are not yet implemented; see
`docs/completion-matrix-v0.3.md`. Reuses the same artifact-ID scheme
(`skillrewind.domain.ids.build_artifact_id`) and CAS layout Lite mode uses so
a future Lite -> Service import bridge can preserve identity rather than
re-deriving it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ...cas.base import ContentAddressedStore
from ...domain.enums import ArtifactKind
from ...domain.errors import NotFoundError
from ...domain.ids import build_artifact_id
from .models import Artifact

_SCHEME_BY_KIND = {
    "agent-skill": "skill",
    "memory": "memory",
    "trajectory": "trace",
    "prompt-patch": "patch",
    "source-code": "code",
    "configuration": "config",
    "template": "template",
    "workflow": "workflow",
}


class ArtifactRepository:
    """Service-mode artifact metadata + CAS-backed body access."""

    def __init__(self, session: Session, cas: ContentAddressedStore) -> None:
        self.session = session
        self.cas = cas

    def ingest(
        self,
        content: bytes,
        *,
        kind: str,
        logical_name: str,
        mime_type: str = "application/octet-stream",
        creator: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Artifact:
        if kind not in {k.value for k in ArtifactKind}:
            raise ValueError(f"unknown artifact kind: {kind!r}")
        obj = self.cas.put_bytes(content)
        scheme = _SCHEME_BY_KIND.get(kind, "artifact")
        artifact_id = build_artifact_id(scheme, logical_name, obj.digest_hex)

        existing = self.session.get(Artifact, artifact_id)
        if existing is not None:
            # Same (kind, logical_name, content digest) -> same identity.
            # Re-ingesting identical content is idempotent: return the
            # existing row rather than inserting a duplicate or erroring.
            return existing

        row = Artifact(
            artifact_id=artifact_id,
            digest_hex=obj.digest_hex,
            kind=kind,
            logical_name=logical_name,
            mime_type=mime_type,
            byte_size=obj.size_bytes,
            created_at=datetime.now(timezone.utc),
            storage_ref=f"cas://{obj.digest_hex}",
            status="active",
            creator=creator,
            metadata_json=metadata or {},
        )
        self.session.add(row)
        self.session.commit()
        return row

    def get(self, artifact_id: str) -> Artifact:
        row = self.session.get(Artifact, artifact_id)
        if row is None:
            raise NotFoundError(f"no such artifact: {artifact_id}")
        return row

    def get_content(self, artifact_id: str) -> bytes:
        row = self.get(artifact_id)
        return self.cas.get_bytes(row.digest_hex)

    def list(self, *, limit: int = 50, cursor: Optional[str] = None, kind: Optional[str] = None) -> list[Artifact]:
        """Keyset-paginated listing, newest first. `cursor` is an opaque
        `"<iso-created_at>|<artifact_id>"` token from a previous page's last
        row -- bounded by the index on (created_at), never an unbounded
        fetch-then-slice over the whole table."""

        limit = max(1, min(limit, 500))
        stmt = select(Artifact).order_by(Artifact.created_at.desc(), Artifact.artifact_id.desc())
        if kind is not None:
            stmt = stmt.where(Artifact.kind == kind)
        if cursor is not None:
            cursor_created_at_raw, _, cursor_id = cursor.partition("|")
            cursor_created_at = datetime.fromisoformat(cursor_created_at_raw)
            stmt = stmt.where(
                or_(
                    Artifact.created_at < cursor_created_at,
                    and_(Artifact.created_at == cursor_created_at, Artifact.artifact_id < cursor_id),
                )
            )
        stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars())

    @staticmethod
    def cursor_for(artifact: Artifact) -> str:
        return f"{artifact.created_at.isoformat()}|{artifact.artifact_id}"
