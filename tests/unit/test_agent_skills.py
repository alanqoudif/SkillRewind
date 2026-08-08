from __future__ import annotations

from pathlib import Path

import pytest

from skillrewind.adapters.agent_skills import build_manifest, export_skill, ingest_skill_directory
from skillrewind.domain.enums import ArtifactKind
from skillrewind.domain.errors import CASPathTraversalError, LineageFormatError
from skillrewind.workspace import Workspace

FIXTURES = Path(__file__).parent.parent / "fixtures" / "agent-skills"


def test_build_manifest_valid_skill():
    manifest = build_manifest(FIXTURES / "valid-skill")
    assert manifest.name == "fast-http"
    assert "mock_disable_verification" in manifest.body
    paths = {f.relative_path for f in manifest.files}
    assert "SKILL.md" in paths
    assert "scripts/deploy.py" in paths


def test_build_manifest_rejects_missing_name():
    with pytest.raises(LineageFormatError):
        build_manifest(FIXTURES / "malformed-skill")


def test_build_manifest_rejects_symlink_escape():
    with pytest.raises(CASPathTraversalError):
        build_manifest(FIXTURES / "traversal-skill")


def test_ingest_and_export_roundtrip(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    artifact = ingest_skill_directory(ws, FIXTURES / "valid-skill", alias="fast-http")
    assert artifact.kind == ArtifactKind.AGENT_SKILL
    assert artifact.logical_name == "fast-http"

    original_bytes = (FIXTURES / "valid-skill" / "scripts" / "deploy.py").read_bytes()
    exported = export_skill(ws, artifact.artifact_id, tmp_path / "exported")
    assert (exported / "SKILL.md").is_file()
    assert (exported / "scripts" / "deploy.py").read_bytes() == original_bytes

    # ingestion never mutates the source directory
    assert (FIXTURES / "valid-skill" / "SKILL.md").is_file()
    ws.close()


def test_ingest_does_not_write_into_source_dir(tmp_path):
    ws = Workspace.init(tmp_path / "ws")
    before = sorted(p.name for p in (FIXTURES / "valid-skill").rglob("*"))
    ingest_skill_directory(ws, FIXTURES / "valid-skill")
    after = sorted(p.name for p in (FIXTURES / "valid-skill").rglob("*"))
    assert before == after
    ws.close()
