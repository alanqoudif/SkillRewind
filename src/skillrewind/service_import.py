"""Lite -> Service mode import bridge (Phase C2.1 section 8).

Reads a Lite-mode workspace on disk (unmodified by this module) and writes
its artifacts and *recorded*-evidence edges into a Service-mode database +
CAS via the same repositories the API uses
(`skillrewind.persistence.service.repositories`). Content is
re-content-addressed through the target CAS's `put_bytes`, which is itself
idempotent by digest, so re-importing already-present content is a no-op
rather than a duplicate write. Only `evidence_class == "recorded"` edges are
imported, and they are imported as `recorded` -- this module never upgrades
an `inferred`/`unresolved`/etc. edge's evidence class, and never imports
non-recorded edges at all (a future increment could add an explicit
`--include-inferred` mode; out of scope here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cas.base import ContentAddressedStore
from .cas.local import LocalCAS
from .domain.errors import NotFoundError
from .persistence.service.engine import build_engine
from .persistence.service.repositories import ArtifactRepository, DerivationRepository, record_audit_event
from .workspace import Workspace


@dataclass(slots=True)
class ImportReport:
    imported: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    missing_content: list[str] = field(default_factory=list)
    invalid_references: list[dict[str, Any]] = field(default_factory=list)
    edges_imported: int = 0
    edges_already_present: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "already_present": self.already_present,
            "rejected": self.rejected,
            "missing_content": self.missing_content,
            "invalid_references": self.invalid_references,
            "edges_imported": self.edges_imported,
            "edges_already_present": self.edges_already_present,
            "dry_run": self.dry_run,
        }


def import_workspace(
    workspace_dir: str,
    *,
    database_url: str,
    cas_root: str,
    dry_run: bool = False,
    actor: str = "service-import",
) -> ImportReport:
    """Import a Lite-mode workspace's artifacts and recorded edges into
    Service mode. Idempotent: running twice against the same workspace and
    target produces the same end state (`already_present` on the second
    run, never a duplicate row).
    """

    report = ImportReport(dry_run=dry_run)
    ws = Workspace.open(workspace_dir)
    try:
        target_cas: ContentAddressedStore = LocalCAS(cas_root)
        engine = build_engine(database_url)
        from sqlalchemy.orm import Session

        session = Session(engine)
        try:
            artifacts = ArtifactRepository(session, target_cas)
            derivations = DerivationRepository(session, artifacts)

            known_ids: set[str] = set()
            for artifact in ws.artifacts.list(limit=100_000):
                known_ids.add(artifact.artifact_id)
                try:
                    content = ws.cas.get_bytes(artifact.digest_hex)
                except Exception:
                    report.missing_content.append(artifact.artifact_id)
                    continue

                import hashlib

                actual_digest = hashlib.sha256(content).hexdigest()
                if actual_digest != artifact.digest_hex:
                    report.rejected.append(
                        {"artifact_id": artifact.artifact_id, "reason": "digest mismatch (CAS corruption)"}
                    )
                    continue

                already = False
                try:
                    artifacts.get(artifact.artifact_id)
                    already = True
                except NotFoundError:
                    already = False

                if already:
                    report.already_present.append(artifact.artifact_id)
                    continue

                if dry_run:
                    report.imported.append(artifact.artifact_id)
                    continue

                imported = artifacts.ingest(
                    content,
                    kind=artifact.kind.value,
                    logical_name=artifact.logical_name,
                    mime_type=artifact.mime_type,
                    creator=artifact.creator,
                    metadata=artifact.metadata,
                )
                report.imported.append(imported.artifact_id)
                record_audit_event(
                    session,
                    event_type="service_import.artifact_imported",
                    actor=actor,
                    payload={"artifact_id": imported.artifact_id, "source_workspace": workspace_dir},
                    entity_id=imported.artifact_id,
                )

            if not dry_run:
                session.commit()

            recorded_edges = [e for e in ws.edges.list_all(status="active", evidence_class="recorded")]
            for edge in recorded_edges:
                if edge.source not in known_ids or edge.target not in known_ids:
                    report.invalid_references.append(
                        {"source": edge.source, "target": edge.target, "relation": edge.relation.value}
                    )
                    continue
                if dry_run:
                    report.edges_imported += 1
                    continue

                deriv_id = f"import-{edge.target}"[:120]
                try:
                    derivation = derivations.get(deriv_id)
                except NotFoundError:
                    derivation = derivations.create(
                        recipe="service-import",
                        recipe_version="1.0",
                        derivation_id=deriv_id,
                        payload={"imported_from": workspace_dir},
                        actor=actor,
                    )
                relation = edge.relation.value
                from .domain.enums import DERIVATION_INPUT_RELATIONS

                normalized_relation = relation if relation in DERIVATION_INPUT_RELATIONS else "imported-dependency"
                before = derivations.list_inputs(deriv_id)
                already_had = any(i.parent_artifact_id == edge.source and i.relation == normalized_relation for i in before)
                try:
                    derivations.add_inputs(deriv_id, [(edge.source, normalized_relation)], actor=actor)
                    if derivation.target_artifact_id is None:
                        derivations.set_output(deriv_id, edge.target, actor=actor)
                except ValueError:
                    report.invalid_references.append(
                        {"source": edge.source, "target": edge.target, "relation": relation, "reason": "self-edge or conflicting output"}
                    )
                    continue
                if already_had:
                    report.edges_already_present += 1
                else:
                    report.edges_imported += 1
        finally:
            session.close()
    finally:
        ws.close()
    return report
