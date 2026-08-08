"""Repository classes over the SQLite schema in :mod:`skillrewind.persistence.database`."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from ..canonical.json import canonical_bytes
from ..domain.enums import (
    EvidenceClass,
    LifecycleStatus,
    RelationType,
    RevocationPolicy,
    RevocationState,
    Severity,
)
from ..domain.errors import NotFoundError
from ..domain.models import Artifact, Derivation, InfluenceEdge, ReplayRecord, RevocationEvent, Waiver


def _dumps(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


class ArtifactRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, artifact: Artifact) -> None:
        self._conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, digest_hex, kind, logical_name, mime_type, byte_size,
                created_at, storage_ref, status, creator, metadata_json, schema_version,
                supersedes, superseded_by, alias
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                status=excluded.status, superseded_by=excluded.superseded_by,
                alias=excluded.alias, metadata_json=excluded.metadata_json
            """,
            (
                artifact.artifact_id,
                artifact.digest_hex,
                artifact.kind.value,
                artifact.logical_name,
                artifact.mime_type,
                artifact.byte_size,
                artifact.created_at,
                artifact.storage_ref,
                artifact.status.value,
                artifact.creator,
                _dumps(artifact.metadata),
                artifact.schema_version,
                artifact.supersedes,
                artifact.superseded_by,
                artifact.alias,
            ),
        )
        self._conn.commit()

    def _row_to_artifact(self, row: sqlite3.Row) -> Artifact:
        from ..domain.enums import ArtifactKind

        return Artifact(
            artifact_id=row["artifact_id"],
            digest_hex=row["digest_hex"],
            kind=ArtifactKind(row["kind"]),
            logical_name=row["logical_name"],
            mime_type=row["mime_type"],
            byte_size=row["byte_size"],
            created_at=row["created_at"],
            storage_ref=row["storage_ref"],
            status=LifecycleStatus(row["status"]),
            creator=row["creator"],
            metadata=json.loads(row["metadata_json"]),
            schema_version=row["schema_version"],
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            alias=row["alias"],
        )

    def get(self, artifact_id: str) -> Artifact:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"artifact not found: {artifact_id}")
        return self._row_to_artifact(row)

    def find_by_alias(self, alias: str) -> Optional[Artifact]:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE alias = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (alias,),
        ).fetchone()
        return self._row_to_artifact(row) if row else None

    def list(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]:
        self._conn.row_factory = sqlite3.Row
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM artifacts {where} ORDER BY created_at ASC, artifact_id ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    def set_status(self, artifact_id: str, status: LifecycleStatus) -> None:
        self._conn.execute(
            "UPDATE artifacts SET status = ? WHERE artifact_id = ?", (status.value, artifact_id)
        )
        self._conn.commit()

    def set_alias(self, artifact_id: str, alias: str) -> None:
        self._conn.execute(
            "UPDATE artifacts SET alias = ? WHERE artifact_id = ?", (alias, artifact_id)
        )
        self._conn.commit()

    def clear_alias(self, alias: str) -> None:
        self._conn.execute("UPDATE artifacts SET alias = NULL WHERE alias = ?", (alias,))
        self._conn.commit()

    def set_superseded_by(self, artifact_id: str, successor_id: str) -> None:
        self._conn.execute(
            "UPDATE artifacts SET status='superseded', superseded_by=? WHERE artifact_id=?",
            (successor_id, artifact_id),
        )
        self._conn.commit()


class DerivationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, derivation: Derivation) -> None:
        self._conn.execute(
            """
            INSERT INTO derivations (derivation_id, recipe, recipe_version, target_artifact_id,
                                      payload_json, started_at, ended_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(derivation_id) DO UPDATE SET
                payload_json=excluded.payload_json, ended_at=excluded.ended_at,
                target_artifact_id=excluded.target_artifact_id
            """,
            (
                derivation.derivation_id,
                derivation.recipe,
                derivation.recipe_version,
                derivation.target_artifact_id,
                _dumps(derivation.to_dict()),
                derivation.started_at,
                derivation.ended_at,
            ),
        )
        self._conn.commit()

    def get(self, derivation_id: str) -> Derivation:
        row = self._conn.execute(
            "SELECT payload_json FROM derivations WHERE derivation_id = ?", (derivation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"derivation not found: {derivation_id}")
        data = json.loads(row[0])
        return Derivation(**data)

    def find_by_target(self, target_artifact_id: str) -> Optional[Derivation]:
        row = self._conn.execute(
            "SELECT payload_json FROM derivations WHERE target_artifact_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (target_artifact_id,),
        ).fetchone()
        return Derivation(**json.loads(row[0])) if row else None

    def list_all(self) -> list[Derivation]:
        rows = self._conn.execute("SELECT payload_json FROM derivations ORDER BY started_at ASC").fetchall()
        return [Derivation(**json.loads(r[0])) for r in rows]


class EdgeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, edge: InfluenceEdge) -> None:
        self._conn.execute(
            """
            INSERT INTO edges (source, target, relation, evidence_class, status, confidence,
                                payload_json, created_at, updated_at, scorer_version, invalidation_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, target, relation) DO UPDATE SET
                evidence_class=excluded.evidence_class, status=excluded.status,
                confidence=excluded.confidence, payload_json=excluded.payload_json,
                updated_at=excluded.updated_at, scorer_version=excluded.scorer_version,
                invalidation_reason=excluded.invalidation_reason
            """,
            (
                edge.source,
                edge.target,
                edge.relation.value,
                edge.evidence_class.value,
                edge.status,
                edge.confidence,
                _dumps(edge.to_dict()),
                edge.created_at,
                edge.updated_at,
                edge.scorer_version,
                edge.invalidation_reason,
            ),
        )
        self._conn.commit()

    def _row_to_edge(self, payload_json: str) -> InfluenceEdge:
        data = dict(json.loads(payload_json))
        data["relation"] = RelationType(data["relation"])
        data["evidence_class"] = EvidenceClass(data["evidence_class"])
        return InfluenceEdge(**data)

    def list_all(
        self, *, evidence_class: Optional[str] = None, status: Optional[str] = None
    ) -> list[InfluenceEdge]:
        clauses, params = [], []
        if evidence_class:
            clauses.append("evidence_class = ?")
            params.append(evidence_class)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT payload_json FROM edges {where} ORDER BY source ASC, target ASC", params
        ).fetchall()
        return [self._row_to_edge(r[0]) for r in rows]

    def outgoing(self, source: str) -> list[InfluenceEdge]:
        rows = self._conn.execute(
            "SELECT payload_json FROM edges WHERE source = ? ORDER BY target ASC", (source,)
        ).fetchall()
        return [self._row_to_edge(r[0]) for r in rows]

    def incoming(self, target: str) -> list[InfluenceEdge]:
        rows = self._conn.execute(
            "SELECT payload_json FROM edges WHERE target = ? ORDER BY source ASC", (target,)
        ).fetchall()
        return [self._row_to_edge(r[0]) for r in rows]


class ReplayRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ReplayRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO replay_records (replay_id, target_derivation_id, candidate_ancestor_id,
                                         intervention_kind, verdict, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                record.replay_id,
                record.target_derivation_id,
                record.candidate_ancestor_id,
                record.intervention_kind,
                record.verdict.value if record.verdict else None,
                _dumps(record.to_dict()),
                record.created_at,
            ),
        )
        self._conn.commit()

    def list_for_candidate(self, target_derivation_id: str, candidate_ancestor_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload_json FROM replay_records WHERE target_derivation_id=? AND candidate_ancestor_id=?"
            " ORDER BY created_at ASC",
            (target_derivation_id, candidate_ancestor_id),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, replay_id: str) -> dict:
        row = self._conn.execute(
            "SELECT payload_json FROM replay_records WHERE replay_id = ?", (replay_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"replay record not found: {replay_id}")
        return json.loads(row[0])


class RevocationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, event: RevocationEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO revocation_events (event_id, idempotency_key, state, policy, severity,
                                            payload_json, created_at, completed_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                event.event_id,
                event.idempotency_key,
                event.state.value,
                event.policy.value,
                event.severity.value,
                _dumps(event.to_dict()),
                event.created_at,
                event.completed_at,
            ),
        )
        self._conn.commit()

    def update(self, event: RevocationEvent) -> None:
        self._conn.execute(
            """
            UPDATE revocation_events SET state=?, payload_json=?, completed_at=?
            WHERE event_id=?
            """,
            (event.state.value, _dumps(event.to_dict()), event.completed_at, event.event_id),
        )
        self._conn.commit()

    def record_transition(self, event_id: str, from_state: Optional[str], to_state: str, at: str) -> None:
        self._conn.execute(
            "INSERT INTO revocation_transitions (event_id, from_state, to_state, at) VALUES (?,?,?,?)",
            (event_id, from_state, to_state, at),
        )
        self._conn.commit()

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[RevocationEvent]:
        row = self._conn.execute(
            "SELECT payload_json FROM revocation_events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return self._row_to_event(row[0]) if row else None

    def _row_to_event(self, payload_json: str) -> RevocationEvent:
        data = dict(json.loads(payload_json))
        data["severity"] = Severity(data["severity"])
        data["policy"] = RevocationPolicy(data["policy"])
        data["state"] = RevocationState(data["state"])
        return RevocationEvent(**data)

    def get(self, event_id: str) -> RevocationEvent:
        row = self._conn.execute(
            "SELECT payload_json FROM revocation_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"revocation event not found: {event_id}")
        return self._row_to_event(row[0])

    def list_all(self) -> list[RevocationEvent]:
        rows = self._conn.execute(
            "SELECT payload_json FROM revocation_events ORDER BY created_at ASC"
        ).fetchall()
        return [self._row_to_event(r[0]) for r in rows]

    def add_quarantine(self, artifact_id: str, revocation_event_id: str, reason: str, created_at: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO quarantine (artifact_id, revocation_event_id, reason, created_at) "
            "VALUES (?,?,?,?)",
            (artifact_id, revocation_event_id, reason, created_at),
        )
        self._conn.commit()

    def remove_quarantine(self, artifact_id: str) -> None:
        self._conn.execute("DELETE FROM quarantine WHERE artifact_id = ?", (artifact_id,))
        self._conn.commit()

    def is_quarantined(self, artifact_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM quarantine WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return row is not None

    def list_quarantine(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT artifact_id, revocation_event_id, reason, created_at FROM quarantine ORDER BY created_at ASC"
        ).fetchall()
        return [
            {"artifact_id": r[0], "revocation_event_id": r[1], "reason": r[2], "created_at": r[3]}
            for r in rows
        ]


class WaiverRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, waiver: Waiver) -> None:
        self._conn.execute(
            """
            INSERT INTO waivers (waiver_id, artifact_id, actor, reason, scope, created_at,
                                  expires_at, revocation_event_id, revoked)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                waiver.waiver_id,
                waiver.artifact_id,
                waiver.actor,
                waiver.reason,
                waiver.scope,
                waiver.created_at,
                waiver.expires_at,
                waiver.revocation_event_id,
                int(waiver.revoked),
            ),
        )
        self._conn.commit()

    def revoke(self, waiver_id: str) -> None:
        self._conn.execute("UPDATE waivers SET revoked = 1 WHERE waiver_id = ?", (waiver_id,))
        self._conn.commit()

    def active_for_artifact(self, artifact_id: str, *, now: str) -> list[Waiver]:
        rows = self._conn.execute(
            "SELECT waiver_id, artifact_id, actor, reason, scope, created_at, expires_at, "
            "revocation_event_id, revoked FROM waivers WHERE artifact_id = ? AND revoked = 0",
            (artifact_id,),
        ).fetchall()
        result = []
        for r in rows:
            if r[6] is not None and r[6] < now:
                continue
            result.append(
                Waiver(
                    waiver_id=r[0], artifact_id=r[1], actor=r[2], reason=r[3], scope=r[4],
                    created_at=r[5], expires_at=r[6], revocation_event_id=r[7], revoked=bool(r[8]),
                )
            )
        return result
