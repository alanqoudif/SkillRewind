"""Append-only, hash-chained audit log.

Each event's canonical hash covers its payload plus the previous event's
hash, forming a hash chain (similar in spirit to a blockchain header chain
but with no distributed consensus). This makes deletion, reordering, or
in-place modification of *any* already-appended event detectable by
recomputing the chain from genesis. It is tamper-**evident**, not
tamper-**proof**: an attacker who can rewrite the entire log file and its
externally stored head hash undetected defeats it. Publish or externally
notarize the chain head if a stronger guarantee is required.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..canonical.json import canonical_bytes, sha256_hex
from ..domain.errors import AuditChainError

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    event_id: str
    event_type: str
    actor: str
    payload: dict[str, Any]
    created_at: str
    prev_hash: str
    event_hash: str = field(compare=False)


def _event_hash(
    sequence: int,
    event_id: str,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    created_at: str,
    prev_hash: str,
) -> str:
    body = {
        "sequence": sequence,
        "event_id": event_id,
        "event_type": event_type,
        "actor": actor,
        "payload": payload,
        "created_at": created_at,
        "prev_hash": prev_hash,
    }
    return sha256_hex(body)


class AuditLog:
    """SQLite-backed hash-chained audit log."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _last_event(self) -> tuple[int, str]:
        row = self._conn.execute(
            "SELECT sequence, event_hash FROM audit_log ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return row[0], row[1]

    def append(self, event_type: str, actor: str, payload: dict[str, Any]) -> AuditEvent:
        last_sequence, prev_hash = self._last_event()
        sequence = last_sequence + 1
        event_id = str(uuid.uuid4())
        created_at = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        )
        event_hash = _event_hash(sequence, event_id, event_type, actor, payload, created_at, prev_hash)
        self._conn.execute(
            """
            INSERT INTO audit_log
                (sequence, event_id, event_type, actor, payload_json, created_at, prev_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                event_id,
                event_type,
                actor,
                canonical_bytes(payload).decode("utf-8"),
                created_at,
                prev_hash,
                event_hash,
            ),
        )
        self._conn.commit()
        return AuditEvent(
            sequence=sequence,
            event_id=event_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
            created_at=created_at,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )

    def iter_events(self) -> Iterator[AuditEvent]:
        cursor = self._conn.execute(
            """
            SELECT sequence, event_id, event_type, actor, payload_json, created_at, prev_hash, event_hash
            FROM audit_log ORDER BY sequence ASC
            """
        )
        for row in cursor:
            yield AuditEvent(
                sequence=row[0],
                event_id=row[1],
                event_type=row[2],
                actor=row[3],
                payload=json.loads(row[4]),
                created_at=row[5],
                prev_hash=row[6],
                event_hash=row[7],
            )

    def head_hash(self) -> str:
        _, prev = self._last_event()
        return prev

    def verify(self) -> "AuditVerificationResult":
        expected_prev = GENESIS_HASH
        expected_sequence = 1
        checked = 0
        for event in self.iter_events():
            if event.sequence != expected_sequence:
                return AuditVerificationResult(
                    ok=False,
                    checked=checked,
                    error=(
                        f"sequence gap detected at expected sequence {expected_sequence}, "
                        f"found {event.sequence} (event deleted or reordered)"
                    ),
                )
            if event.prev_hash != expected_prev:
                return AuditVerificationResult(
                    ok=False,
                    checked=checked,
                    error=(
                        f"chain break at sequence {event.sequence}: prev_hash mismatch "
                        "(a prior event was modified, deleted, or reordered)"
                    ),
                )
            recomputed = _event_hash(
                event.sequence,
                event.event_id,
                event.event_type,
                event.actor,
                event.payload,
                event.created_at,
                event.prev_hash,
            )
            if recomputed != event.event_hash:
                return AuditVerificationResult(
                    ok=False,
                    checked=checked,
                    error=f"event hash mismatch at sequence {event.sequence} (event was modified)",
                )
            expected_prev = event.event_hash
            expected_sequence += 1
            checked += 1
        return AuditVerificationResult(ok=True, checked=checked, error=None)

    def export(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8") as handle:
            for event in self.iter_events():
                handle.write(
                    canonical_bytes(
                        {
                            "sequence": event.sequence,
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "actor": event.actor,
                            "payload": event.payload,
                            "created_at": event.created_at,
                            "prev_hash": event.prev_hash,
                            "event_hash": event.event_hash,
                        }
                    ).decode("utf-8")
                    + "\n"
                )


@dataclass(frozen=True, slots=True)
class AuditVerificationResult:
    ok: bool
    checked: int
    error: str | None

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise AuditChainError(self.error or "audit chain verification failed")
