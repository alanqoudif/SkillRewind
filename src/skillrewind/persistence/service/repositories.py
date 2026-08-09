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

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...canonical.json import sha256_hex
from ...cas.base import ContentAddressedStore
from ...domain.enums import DERIVATION_INPUT_RELATIONS, ArtifactKind, EvidenceClass, RelationType
from ...domain.errors import NotFoundError
from ...domain.ids import build_artifact_id
from ...domain.models import InfluenceEdge as InfluenceEdgeDomain
from ...lineage.graph import LineageGraph
from .models import (
    Artifact,
    AuditEvent,
    CandidateScore,
    CandidateScoringRun,
    Derivation,
    DerivationInput,
    FeatureObservation,
    InfluenceEdge,
    ReplayRecord,
    ReplayRun,
)

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_audit_event(
    session: Session,
    *,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    request_id: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> str:
    """Append a hash-chained audit row to the Service-mode ``audit_events``
    table. Every write in this module goes through this function so every
    mutation carries actor, request correlation ID, timestamp, and a stable
    entity identifier (master spec Phase C2.1 section 1: "make all write
    operations auditable"). Chained the same way as Lite mode's file-backed
    log (see ``skillrewind.audit.log``): each hash covers the previous row's
    hash plus this row's own content, so deletion/reordering/tampering with
    any already-appended row is detectable by recomputing the chain."""

    full_payload = dict(payload)
    if request_id is not None:
        full_payload["request_id"] = request_id
    if entity_id is not None:
        full_payload["entity_id"] = entity_id

    previous = session.execute(select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)).scalar_one_or_none()
    previous_hash = previous.event_hash if previous is not None else "0" * 64
    created_at = _now()
    body = {
        "event_type": event_type,
        "actor": actor,
        "payload": full_payload,
        "created_at": created_at.isoformat(),
        "previous_hash": previous_hash,
    }
    event_hash = sha256_hex(body)
    session.add(
        AuditEvent(
            event_hash=event_hash,
            previous_hash=previous_hash,
            event_type=event_type,
            actor=actor,
            payload_json=full_payload,
            created_at=created_at,
        )
    )
    session.commit()
    return event_hash


class DerivationRepository:
    """Service-mode derivation capture: a derivation's recorded parent
    inputs (with exact relation type) and its output artifact. Every
    recorded input is materialized as a corresponding `recorded`-evidence
    row in `edges` as soon as the output artifact is known, so
    `LineageRepository`'s recorded closure sees it -- the `derivation_inputs`
    table and `edges` table are two views onto the same recorded facts, never
    independent sources of truth.
    """

    def __init__(self, session: Session, artifacts: "ArtifactRepository") -> None:
        self.session = session
        self.artifacts = artifacts

    def create(
        self,
        *,
        recipe: str,
        recipe_version: str,
        derivation_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        started_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        actor: str = "unknown",
        request_id: Optional[str] = None,
    ) -> Derivation:
        derivation_id = derivation_id or f"deriv-{uuid.uuid4()}"
        existing = self.session.get(Derivation, derivation_id)
        if existing is not None:
            return existing
        row = Derivation(
            derivation_id=derivation_id,
            recipe=recipe,
            recipe_version=recipe_version,
            target_artifact_id=None,
            payload_json=payload or {},
            started_at=started_at,
            ended_at=ended_at,
        )
        self.session.add(row)
        self.session.commit()
        record_audit_event(
            self.session,
            event_type="derivation.created",
            actor=actor,
            payload={"derivation_id": derivation_id, "recipe": recipe, "recipe_version": recipe_version},
            request_id=request_id,
            entity_id=derivation_id,
        )
        return row

    def get(self, derivation_id: str) -> Derivation:
        row = self.session.get(Derivation, derivation_id)
        if row is None:
            raise NotFoundError(f"no such derivation: {derivation_id}")
        return row

    def list_inputs(self, derivation_id: str) -> list[DerivationInput]:
        stmt = (
            select(DerivationInput)
            .where(DerivationInput.derivation_id == derivation_id)
            .order_by(DerivationInput.parent_artifact_id, DerivationInput.relation)
        )
        return list(self.session.execute(stmt).scalars())

    def add_inputs(
        self,
        derivation_id: str,
        inputs: Sequence[tuple[str, str]],
        *,
        actor: str = "unknown",
        request_id: Optional[str] = None,
    ) -> list[DerivationInput]:
        """Attach parent artifacts to a derivation. `inputs` is a sequence of
        `(parent_artifact_id, relation)`. Rejects references to nonexistent
        artifacts, rejects self-edges (the derivation's own output, once
        known, may never be its own input -- no relation in
        `DERIVATION_INPUT_RELATIONS` permits this), and is idempotent: an
        already-recorded `(derivation_id, parent, relation)` triple is
        skipped without a duplicate row or a duplicate audit event."""

        derivation = self.get(derivation_id)
        created: list[DerivationInput] = []
        for parent_artifact_id, relation in inputs:
            if relation not in DERIVATION_INPUT_RELATIONS:
                raise ValueError(
                    f"unknown derivation-input relation {relation!r}; must be one of {sorted(DERIVATION_INPUT_RELATIONS)}"
                )
            if self.session.get(Artifact, parent_artifact_id) is None:
                raise NotFoundError(f"no such artifact (derivation-input parent): {parent_artifact_id}")
            if derivation.target_artifact_id is not None and parent_artifact_id == derivation.target_artifact_id:
                raise ValueError(f"self-edge not permitted: {parent_artifact_id} cannot be its own derivation input")

            existing = self.session.execute(
                select(DerivationInput).where(
                    and_(
                        DerivationInput.derivation_id == derivation_id,
                        DerivationInput.parent_artifact_id == parent_artifact_id,
                        DerivationInput.relation == relation,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue  # idempotent: already recorded, no duplicate row/audit event

            row = DerivationInput(derivation_id=derivation_id, parent_artifact_id=parent_artifact_id, relation=relation)
            self.session.add(row)
            try:
                self.session.commit()
            except IntegrityError:
                self.session.rollback()
                continue  # lost a race with a concurrent identical insert; not an error
            created.append(row)
            record_audit_event(
                self.session,
                event_type="derivation.input_added",
                actor=actor,
                payload={"derivation_id": derivation_id, "parent_artifact_id": parent_artifact_id, "relation": relation},
                request_id=request_id,
                entity_id=derivation_id,
            )

            if derivation.target_artifact_id is not None:
                self._materialize_edge(
                    source=parent_artifact_id,
                    target=derivation.target_artifact_id,
                    relation=relation,
                    actor=actor,
                    request_id=request_id,
                )
        return created

    def set_output(
        self, derivation_id: str, artifact_id: str, *, actor: str = "unknown", request_id: Optional[str] = None
    ) -> Derivation:
        derivation = self.get(derivation_id)
        if self.session.get(Artifact, artifact_id) is None:
            raise NotFoundError(f"no such artifact (derivation output): {artifact_id}")

        existing_inputs = self.list_inputs(derivation_id)
        if any(i.parent_artifact_id == artifact_id for i in existing_inputs):
            raise ValueError(f"self-edge not permitted: {artifact_id} cannot be its own derivation input")

        if derivation.target_artifact_id is not None and derivation.target_artifact_id != artifact_id:
            raise ValueError(
                f"derivation {derivation_id} already has a different output artifact "
                f"({derivation.target_artifact_id!r}); output is immutable once set"
            )
        already_set = derivation.target_artifact_id == artifact_id
        derivation.target_artifact_id = artifact_id
        self.session.commit()
        if not already_set:
            record_audit_event(
                self.session,
                event_type="derivation.output_set",
                actor=actor,
                payload={"derivation_id": derivation_id, "output_artifact_id": artifact_id},
                request_id=request_id,
                entity_id=derivation_id,
            )

        for inp in existing_inputs:
            self._materialize_edge(
                source=inp.parent_artifact_id, target=artifact_id, relation=inp.relation, actor=actor, request_id=request_id
            )
        return derivation

    def _materialize_edge(
        self, *, source: str, target: str, relation: str, actor: str, request_id: Optional[str]
    ) -> None:
        if source == target:
            raise ValueError(f"self-edge not permitted: {source} -> {target}")
        existing = self.session.execute(
            select(InfluenceEdge).where(
                and_(InfluenceEdge.source == source, InfluenceEdge.target == target, InfluenceEdge.relation == relation)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return  # idempotent: no duplicate row or audit event
        now = _now()
        row = InfluenceEdge(
            source=source,
            target=target,
            relation=relation,
            evidence_class=EvidenceClass.RECORDED.value,
            status="active",
            confidence=None,
            payload_json={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return  # lost a race with a concurrent identical insert
        record_audit_event(
            self.session,
            event_type="lineage.edge_recorded",
            actor=actor,
            payload={"source": source, "target": target, "relation": relation, "evidence_class": "recorded"},
            request_id=request_id,
            entity_id=f"{source}->{target}:{relation}",
        )

    def get_derivation_for_output(self, artifact_id: str) -> Derivation:
        row = self.session.execute(
            select(Derivation).where(Derivation.target_artifact_id == artifact_id)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"no derivation recorded for artifact: {artifact_id}")
        return row

    def get_parents(self, artifact_id: str) -> list[DerivationInput]:
        row = self.session.execute(
            select(Derivation).where(Derivation.target_artifact_id == artifact_id)
        ).scalar_one_or_none()
        if row is None:
            return []
        return self.list_inputs(row.derivation_id)

    def get_children(self, artifact_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(DerivationInput, Derivation)
            .join(Derivation, Derivation.derivation_id == DerivationInput.derivation_id)
            .where(DerivationInput.parent_artifact_id == artifact_id)
            .where(Derivation.target_artifact_id.is_not(None))
            .order_by(Derivation.target_artifact_id, DerivationInput.relation)
        )
        return [
            {"child_artifact_id": deriv.target_artifact_id, "relation": inp.relation, "derivation_id": deriv.derivation_id}
            for inp, deriv in self.session.execute(stmt).all()
        ]


def _to_domain_edge(row: InfluenceEdge) -> InfluenceEdgeDomain:
    return InfluenceEdgeDomain(
        source=row.source,
        target=row.target,
        relation=RelationType(row.relation),
        evidence_class=EvidenceClass(row.evidence_class),
        status=row.status,
        confidence=row.confidence,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        scorer_version=row.scorer_version,
        invalidation_reason=row.invalidation_reason,
    )


class LineageRepository:
    """Service-mode recorded/graph lineage queries over the `edges` table.

    Critical invariant (regression-tested, see
    `tests/integration/test_service_lineage.py`): `descendants`/`ancestors`
    with their default `evidence_classes=("recorded",)` traverse *only*
    recorded-evidence edges. `inferred`, `replay-confirmed`,
    `replay-rejected`/`rejected`, and `unresolved` edges must never silently
    expand what "recorded closure" means -- a caller who wants those must
    use `graph()` with an explicit `evidence_classes` filter, a visibly
    different, explicitly-labeled query.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def load_edges(
        self,
        *,
        evidence_classes: Optional[Sequence[str]] = None,
        relation_types: Optional[Sequence[str]] = None,
        statuses: Sequence[str] = ("active",),
    ) -> list[InfluenceEdgeDomain]:
        stmt = select(InfluenceEdge)
        if statuses:
            stmt = stmt.where(InfluenceEdge.status.in_(statuses))
        if evidence_classes:
            stmt = stmt.where(InfluenceEdge.evidence_class.in_(evidence_classes))
        if relation_types:
            stmt = stmt.where(InfluenceEdge.relation.in_(relation_types))
        rows = self.session.execute(stmt).scalars().all()
        return [_to_domain_edge(r) for r in rows]

    def _bfs(
        self, roots: Sequence[str], adjacency: dict[str, list[str]], *, include_roots: bool, max_depth: Optional[int]
    ) -> list[str]:
        normalized = sorted({r.strip() for r in roots if r.strip()})
        if not normalized:
            raise ValueError("at least one non-empty root is required")
        visited: dict[str, int] = {r: 0 for r in normalized}
        queue: list[tuple[str, int]] = [(r, 0) for r in normalized]
        head = 0
        while head < len(queue):
            node, depth = queue[head]
            head += 1
            if max_depth is not None and depth >= max_depth:
                continue
            for neighbor in adjacency.get(node, ()):
                if neighbor not in visited:
                    visited[neighbor] = depth + 1
                    queue.append((neighbor, depth + 1))
        result = set(visited)
        if not include_roots:
            result.difference_update(normalized)
        return sorted(result)

    def _adjacency(self, edges: list[InfluenceEdgeDomain], *, reverse: bool) -> dict[str, list[str]]:
        adjacency: dict[str, list[str]] = {}
        for e in edges:
            src, dst = (e.target, e.source) if reverse else (e.source, e.target)
            adjacency.setdefault(src, []).append(dst)
        for k in adjacency:
            adjacency[k] = sorted(set(adjacency[k]))
        return adjacency

    def descendants(
        self,
        roots: Sequence[str],
        *,
        include_roots: bool = True,
        max_depth: Optional[int] = None,
        relation_types: Optional[Sequence[str]] = None,
    ) -> list[str]:
        edges = self.load_edges(evidence_classes=[EvidenceClass.RECORDED.value], relation_types=relation_types)
        return self._bfs(roots, self._adjacency(edges, reverse=False), include_roots=include_roots, max_depth=max_depth)

    def ancestors(
        self,
        roots: Sequence[str],
        *,
        include_roots: bool = True,
        max_depth: Optional[int] = None,
        relation_types: Optional[Sequence[str]] = None,
    ) -> list[str]:
        edges = self.load_edges(evidence_classes=[EvidenceClass.RECORDED.value], relation_types=relation_types)
        return self._bfs(roots, self._adjacency(edges, reverse=True), include_roots=include_roots, max_depth=max_depth)

    def direct_parents(self, artifact_id: str) -> list[str]:
        return self.ancestors([artifact_id], include_roots=False, max_depth=1)

    def direct_children(self, artifact_id: str) -> list[str]:
        return self.descendants([artifact_id], include_roots=False, max_depth=1)

    def has_cycle_through(self, artifact_id: str) -> bool:
        return artifact_id in self.descendants([artifact_id], include_roots=False)

    def path(
        self,
        source: str,
        target: str,
        *,
        evidence_classes: Optional[Sequence[str]] = None,
        relation_types: Optional[Sequence[str]] = None,
    ) -> Optional[dict[str, Any]]:
        edges = self.load_edges(evidence_classes=evidence_classes, relation_types=relation_types)
        graph = LineageGraph(edges)
        result = graph.shortest_path(source, target)
        if result is None:
            return None
        return {"nodes": list(result.nodes), "edges": [e.to_dict() for e in result.edges]}

    def graph(
        self,
        artifact_id: str,
        *,
        evidence_classes: Optional[Sequence[str]] = None,
        relation_types: Optional[Sequence[str]] = None,
        max_depth: Optional[int] = None,
    ) -> dict[str, Any]:
        """Neighborhood export (ancestors + descendants of `artifact_id`)
        suitable for the future web UI: evidence classes are exposed per-edge,
        never collapsed into one generic dependency type."""

        edges = self.load_edges(evidence_classes=evidence_classes, relation_types=relation_types)
        fwd = self._adjacency(edges, reverse=False)
        rev = self._adjacency(edges, reverse=True)
        nodes = set(self._bfs([artifact_id], fwd, include_roots=True, max_depth=max_depth))
        nodes |= set(self._bfs([artifact_id], rev, include_roots=True, max_depth=max_depth))
        induced = [e for e in edges if e.source in nodes and e.target in nodes]
        return {"nodes": sorted(nodes), "edges": [e.to_dict() for e in induced]}


class FeatureRepository:
    """Persists per-pair feature-family observations. Insertion is idempotent
    on `(source, target, feature_family, extractor_version, config_digest)`,
    so re-running extraction for an unchanged pair/config never duplicates
    rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        source_artifact_id: str,
        target_artifact_id: str,
        feature_family: str,
        value: Optional[float],
        extractor_version: str,
        config_digest: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> FeatureObservation:
        existing = self.session.execute(
            select(FeatureObservation).where(
                and_(
                    FeatureObservation.source_artifact_id == source_artifact_id,
                    FeatureObservation.target_artifact_id == target_artifact_id,
                    FeatureObservation.feature_family == feature_family,
                    FeatureObservation.extractor_version == extractor_version,
                    FeatureObservation.config_digest == config_digest,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = FeatureObservation(
            source_artifact_id=source_artifact_id,
            target_artifact_id=target_artifact_id,
            feature_family=feature_family,
            value=value,
            extractor_version=extractor_version,
            config_digest=config_digest,
            payload_json=payload or {},
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.execute(
                select(FeatureObservation).where(
                    and_(
                        FeatureObservation.source_artifact_id == source_artifact_id,
                        FeatureObservation.target_artifact_id == target_artifact_id,
                        FeatureObservation.feature_family == feature_family,
                        FeatureObservation.extractor_version == extractor_version,
                        FeatureObservation.config_digest == config_digest,
                    )
                )
            ).scalar_one_or_none()
            assert existing is not None
            return existing
        return row

    def get_pair(self, source_artifact_id: str, target_artifact_id: str) -> list[FeatureObservation]:
        stmt = select(FeatureObservation).where(
            and_(
                FeatureObservation.source_artifact_id == source_artifact_id,
                FeatureObservation.target_artifact_id == target_artifact_id,
            )
        )
        return list(self.session.execute(stmt).scalars())


class CandidateRepository:
    """Candidate-recovery runs and per-candidate scores. A run is uniquely
    identified by `request_key` (root + artifact snapshot digest + scorer
    version + config digest, or an explicit caller-supplied Idempotency-Key)
    so resubmitting the same recovery request is idempotent end to end:
    same run, same candidates, no duplicate audit events."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_run_by_request_key(self, request_key: str) -> Optional[CandidateScoringRun]:
        return self.session.execute(
            select(CandidateScoringRun).where(CandidateScoringRun.request_key == request_key)
        ).scalar_one_or_none()

    def create_run(
        self,
        *,
        run_id: str,
        request_key: str,
        root_artifact_id: str,
        candidate_scope: Optional[dict[str, Any]],
        feature_config: dict[str, Any],
        threshold_config: dict[str, Any],
        scorer_version: str,
        config_digest: str,
        snapshot_digest: str,
        actor: str = "unknown",
        request_id: Optional[str] = None,
    ) -> CandidateScoringRun:
        existing = self.find_run_by_request_key(request_key)
        if existing is not None:
            return existing
        row = CandidateScoringRun(
            run_id=run_id,
            request_key=request_key,
            root_artifact_id=root_artifact_id,
            candidate_scope_json=candidate_scope or {},
            feature_config_json=feature_config,
            threshold_config_json=threshold_config,
            scorer_version=scorer_version,
            config_digest=config_digest,
            snapshot_digest=snapshot_digest,
            status="queued",
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            # Lost a race with a concurrent submission of the same logical
            # request (same request_key) -- return the winner's row rather
            # than creating a second run.
            self.session.rollback()
            existing = self.find_run_by_request_key(request_key)
            assert existing is not None
            return existing
        record_audit_event(
            self.session,
            event_type="lineage.recovery_run_created",
            actor=actor,
            payload={"run_id": run_id, "root_artifact_id": root_artifact_id},
            request_id=request_id,
            entity_id=run_id,
        )
        return row

    def get_run(self, run_id: str) -> CandidateScoringRun:
        row = self.session.get(CandidateScoringRun, run_id)
        if row is None:
            raise NotFoundError(f"no such recovery run: {run_id}")
        return row

    def set_run_job(self, run_id: str, job_id: str) -> None:
        row = self.get_run(run_id)
        row.job_id = job_id
        self.session.commit()

    def update_run_progress(self, run_id: str, *, checkpoint_index: int, candidates_considered: int) -> None:
        row = self.get_run(run_id)
        row.checkpoint_index = checkpoint_index
        row.candidates_considered = candidates_considered
        self.session.commit()

    def finish_run(self, run_id: str, *, status: str, error_message: Optional[str] = None) -> CandidateScoringRun:
        row = self.get_run(run_id)
        row.status = status
        row.error_message = error_message
        row.completed_at = _now()
        row.candidates_found = self.session.execute(
            select(func.count()).select_from(CandidateScore).where(CandidateScore.run_id == run_id)
        ).scalar_one()
        self.session.commit()
        return row

    def already_scored_candidate_ids(self, run_id: str) -> set[str]:
        stmt = select(CandidateScore.candidate_artifact_id).where(CandidateScore.run_id == run_id)
        return set(self.session.execute(stmt).scalars())

    def add_candidate(
        self,
        *,
        run_id: str,
        target_artifact_id: str,
        candidate_artifact_id: str,
        raw_score: float,
        feature_breakdown: dict[str, Any],
        scorer_version: str,
        strict_negative: bool,
        status: str,
        inclusion_reasons: list[str],
        explanation_text: str,
    ) -> Optional[CandidateScore]:
        """Idempotent on `(run_id, candidate_artifact_id)`. Returns None (no
        row, no audit event) if this candidate was already recorded for this
        run -- the checkpoint/resume path relies on this."""

        existing = self.session.execute(
            select(CandidateScore).where(
                and_(CandidateScore.run_id == run_id, CandidateScore.candidate_artifact_id == candidate_artifact_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
        row = CandidateScore(
            run_id=run_id,
            target_artifact_id=target_artifact_id,
            candidate_artifact_id=candidate_artifact_id,
            raw_score=raw_score,
            calibrated_probability=None,  # never fabricate a calibrated probability from a raw score
            strict_negative=strict_negative,
            feature_breakdown_json=feature_breakdown,
            scorer_version=scorer_version,
            evidence_class=EvidenceClass.INFERRED.value,
            status=status,
            explanation_text=explanation_text,
            inclusion_reasons_json=inclusion_reasons,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return None
        return row

    def get_candidate(self, candidate_id: int) -> CandidateScore:
        row = self.session.get(CandidateScore, candidate_id)
        if row is None:
            raise NotFoundError(f"no such candidate: {candidate_id}")
        return row

    def list_candidates(
        self,
        run_id: str,
        *,
        evidence_class: Optional[str] = None,
        min_raw_score: Optional[float] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CandidateScore]:
        stmt = select(CandidateScore).where(CandidateScore.run_id == run_id)
        if evidence_class:
            stmt = stmt.where(CandidateScore.evidence_class == evidence_class)
        if min_raw_score is not None:
            stmt = stmt.where(CandidateScore.raw_score >= min_raw_score)
        if status:
            stmt = stmt.where(CandidateScore.status == status)
        stmt = stmt.order_by(CandidateScore.raw_score.desc(), CandidateScore.candidate_artifact_id.asc())
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars())


class ReplayRepository:
    """Service-mode replay-run persistence (Phase C2.2): a `ReplayRun` groups
    1..N `ReplayRecord` repetitions and holds the atomic final
    classification. Never mutates the originating `CandidateScore` row's
    score/feature-breakdown fields -- only a denormalized `latest_replay_*`
    pointer on that row is updated at finalize time, and only a *new* `edges`
    row (never an update to `candidate_scores`) represents a promotion to
    `replay-confirmed`/`rejected`/`unresolved` evidence. See
    `docs/adr/0012-service-replay-evidence.md`.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_run_by_request_key(self, request_key: str) -> Optional[ReplayRun]:
        return self.session.execute(select(ReplayRun).where(ReplayRun.request_key == request_key)).scalar_one_or_none()

    def create_run(
        self,
        *,
        replay_run_id: str,
        request_key: str,
        candidate_id: int,
        ancestor_artifact_id: str,
        descendant_artifact_id: str,
        target_derivation_id: Optional[str],
        runner_name: str,
        repetitions_requested: int,
        actor: str,
        request_id: Optional[str] = None,
    ) -> ReplayRun:
        existing = self.find_run_by_request_key(request_key)
        if existing is not None:
            return existing
        row = ReplayRun(
            replay_run_id=replay_run_id,
            request_key=request_key,
            candidate_id=candidate_id,
            ancestor_artifact_id=ancestor_artifact_id,
            descendant_artifact_id=descendant_artifact_id,
            target_derivation_id=target_derivation_id,
            runner_name=runner_name,
            repetitions_requested=repetitions_requested,
            status="queued",
            created_by_actor=actor,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            # Lost a race with a concurrent submission of the same logical
            # request -- return the winner's row rather than creating a
            # second run.
            self.session.rollback()
            existing = self.find_run_by_request_key(request_key)
            assert existing is not None
            return existing
        record_audit_event(
            self.session,
            event_type="replay.run_created",
            actor=actor,
            payload={"replay_run_id": replay_run_id, "candidate_id": candidate_id},
            request_id=request_id,
            entity_id=replay_run_id,
        )
        return row

    def get_run(self, replay_run_id: str) -> ReplayRun:
        row = self.session.get(ReplayRun, replay_run_id)
        if row is None:
            raise NotFoundError(f"no such replay run: {replay_run_id}")
        return row

    def set_run_job(self, replay_run_id: str, job_id: str) -> None:
        row = self.get_run(replay_run_id)
        row.job_id = job_id
        row.status = "running"
        self.session.commit()

    def existing_repetitions(self, replay_run_id: str) -> set[int]:
        stmt = select(ReplayRecord.repetition_index).where(ReplayRecord.replay_run_id == replay_run_id)
        return {i for i in self.session.execute(stmt).scalars() if i is not None}

    def add_repetition_record(
        self,
        *,
        replay_run_id: str,
        repetition_index: int,
        replay_id: str,
        target_derivation_id: Optional[str],
        candidate_ancestor_id: str,
        intervention_kind: str,
        runner_id: Optional[str],
        verdict: Optional[str],
        payload: dict[str, Any],
    ) -> Optional[ReplayRecord]:
        """Idempotent on `(replay_run_id, repetition_index)`. Returns None if
        this repetition was already recorded -- the resume path relies on
        this to never duplicate a repetition's evidence."""

        existing = self.session.execute(
            select(ReplayRecord).where(
                and_(ReplayRecord.replay_run_id == replay_run_id, ReplayRecord.repetition_index == repetition_index)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
        row = ReplayRecord(
            replay_id=replay_id,
            target_derivation_id=target_derivation_id,
            candidate_ancestor_id=candidate_ancestor_id,
            intervention_kind=intervention_kind,
            runner_id=runner_id,
            verdict=verdict,
            payload_json=payload,
            replay_run_id=replay_run_id,
            repetition_index=repetition_index,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return None
        return row

    def list_repetitions(self, replay_run_id: str) -> list[ReplayRecord]:
        stmt = (
            select(ReplayRecord)
            .where(ReplayRecord.replay_run_id == replay_run_id)
            .order_by(ReplayRecord.repetition_index.asc())
        )
        return list(self.session.execute(stmt).scalars())

    def update_checkpoint(self, replay_run_id: str, *, checkpoint_repetition: int) -> None:
        row = self.get_run(replay_run_id)
        row.checkpoint_repetition = checkpoint_repetition
        self.session.commit()

    def finalize_run(
        self,
        replay_run_id: str,
        *,
        status: str,
        verdict: Optional[str] = None,
        fidelity_overall: Optional[float] = None,
        effect_estimate: Optional[float] = None,
        error_message: Optional[str] = None,
        promoted_evidence_class: Optional[str] = None,
        actor: str = "worker",
        request_id: Optional[str] = None,
    ) -> ReplayRun:
        """Atomically finalizes a replay run: updates the run row, the
        denormalized `latest_replay_*` pointer on the originating
        `CandidateScore` (never its score/breakdown fields), and -- when
        `promoted_evidence_class` is given -- materializes exactly one new
        `edges` row carrying that evidence class. All of this commits in a
        single transaction, so a caller can never observe a run marked
        `completed` whose promotion write did not also land (requirement:
        "no endpoint may claim success before all database writes commit").
        """

        run = self.get_run(replay_run_id)
        run.status = status
        run.verdict = verdict
        run.fidelity_overall = fidelity_overall
        run.effect_estimate = effect_estimate
        run.error_message = error_message
        run.completed_at = _now()

        candidate = self.session.get(CandidateScore, run.candidate_id)
        if candidate is not None:
            candidate.latest_replay_run_id = replay_run_id
            candidate.latest_replay_verdict = verdict
            candidate.latest_replay_at = run.completed_at

        if promoted_evidence_class is not None:
            existing_edge = self.session.execute(
                select(InfluenceEdge).where(
                    and_(
                        InfluenceEdge.source == run.ancestor_artifact_id,
                        InfluenceEdge.target == run.descendant_artifact_id,
                        InfluenceEdge.relation == RelationType.HIDDEN_INFLUENCE.value,
                    )
                )
            ).scalar_one_or_none()
            now = _now()
            if existing_edge is None:
                self.session.add(
                    InfluenceEdge(
                        source=run.ancestor_artifact_id,
                        target=run.descendant_artifact_id,
                        relation=RelationType.HIDDEN_INFLUENCE.value,
                        evidence_class=promoted_evidence_class,
                        status="active",
                        confidence=effect_estimate,
                        payload_json={"replay_run_id": replay_run_id},
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                # A prior replay attempt against the same pair already left
                # an edge here (e.g. an earlier UNRESOLVED_* outcome). This
                # replay's outcome supersedes it -- never silently keep a
                # stale evidence class -- but the prior evidence entry is
                # preserved in payload_json rather than discarded.
                existing_edge.evidence_class = promoted_evidence_class
                existing_edge.confidence = effect_estimate
                existing_edge.updated_at = now
                prior_runs = list(existing_edge.payload_json.get("prior_replay_run_ids", []))
                if existing_edge.payload_json.get("replay_run_id") not in (None, replay_run_id):
                    prior_runs.append(existing_edge.payload_json["replay_run_id"])
                existing_edge.payload_json = {"replay_run_id": replay_run_id, "prior_replay_run_ids": prior_runs}

        self.session.commit()
        record_audit_event(
            self.session,
            event_type=f"replay.run_{status}",
            actor=actor,
            payload={"replay_run_id": replay_run_id, "verdict": verdict, "promoted_evidence_class": promoted_evidence_class},
            request_id=request_id,
            entity_id=replay_run_id,
        )
        return run
