"""Agent Skills directory adapter.

Implements ingestion/export for the open "Agent Skills" directory format: a
directory containing a ``SKILL.md`` with YAML frontmatter (``name``,
``description``, plus arbitrary extra metadata) and a Markdown body,
optionally alongside referenced resource files.

Ingestion is strictly read-only: nothing under the source directory is ever
modified. Every included file is hashed; paths that would escape the skill
root (via ``..`` segments or symlinks) are rejected before any bytes are
read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from ..domain.enums import ArtifactKind
from ..domain.errors import CASPathTraversalError, LineageFormatError
from ..domain.models import Artifact
from ..workspace import Workspace

DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_DEPTH = 12
DEFAULT_MAX_FILES = 500

_FRONTMATTER_DELIM = "---"


@dataclass(frozen=True, slots=True)
class SkillFile:
    relative_path: str
    sha256_hex: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SkillManifest:
    name: str
    description: str
    frontmatter: dict[str, Any]
    body: str
    files: tuple[SkillFile, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "files": [
                {"path": f.relative_path, "sha256": f.sha256_hex, "size": f.size_bytes}
                for f in self.files
            ],
            "warnings": list(self.warnings),
        }


def _parse_frontmatter(text: str, *, location: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise LineageFormatError(f"{location}: SKILL.md must begin with a '---' frontmatter block")
    end_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            end_index = i
            break
    if end_index is None:
        raise LineageFormatError(f"{location}: unterminated frontmatter block")
    frontmatter_text = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :]).lstrip("\n")
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise LineageFormatError(f"{location}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise LineageFormatError(f"{location}: frontmatter must be a YAML mapping")
    return data, body


def _iter_safe_files(
    root: Path, *, max_depth: int, max_files: int
) -> list[Path]:
    root = root.resolve()
    results: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    seen_real: set[Path] = set()
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            raise LineageFormatError(f"skill directory nesting exceeds max_depth={max_depth}")
        try:
            entries = sorted(current.iterdir())
        except OSError as exc:
            raise LineageFormatError(f"cannot read {current}: {exc}") from exc
        for entry in entries:
            if entry.is_symlink():
                real = entry.resolve()
                if root not in real.parents and real != root:
                    raise CASPathTraversalError(
                        f"symlink escapes skill root: {entry} -> {real}"
                    )
                if real in seen_real:
                    continue  # avoid symlink cycles
                seen_real.add(real)
                if real.is_dir():
                    stack.append((real, depth + 1))
                    continue
                entry = real
            if entry.is_dir():
                stack.append((entry, depth + 1))
                continue
            resolved = entry.resolve()
            if root not in resolved.parents:
                raise CASPathTraversalError(f"file escapes skill root: {entry}")
            results.append(resolved)
            if len(results) > max_files:
                raise LineageFormatError(f"skill directory exceeds max_files={max_files}")
    return sorted(results)


def build_manifest(
    skill_dir: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
) -> SkillManifest:
    root = Path(skill_dir).resolve()
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise LineageFormatError(f"{root}: missing required SKILL.md")

    frontmatter, body = _parse_frontmatter(
        skill_md.read_text(encoding="utf-8"), location=str(skill_md)
    )
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise LineageFormatError(f"{skill_md}: frontmatter 'name' is required and must be non-empty")
    if not isinstance(description, str) or not description.strip():
        raise LineageFormatError(
            f"{skill_md}: frontmatter 'description' is required and must be non-empty"
        )

    warnings: list[str] = []
    files: list[SkillFile] = []
    for path in _iter_safe_files(root, max_depth=max_depth, max_files=max_files):
        size = path.stat().st_size
        if size > max_file_bytes:
            warnings.append(f"{path.relative_to(root)}: {size} bytes exceeds max_file_bytes={max_file_bytes}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            SkillFile(relative_path=str(path.relative_to(root)), sha256_hex=digest, size_bytes=size)
        )

    return SkillManifest(
        name=name.strip(),
        description=description.strip(),
        frontmatter=frontmatter,
        body=body,
        files=tuple(files),
        warnings=tuple(warnings),
    )


def ingest_skill_directory(
    workspace: Workspace,
    skill_dir: str | Path,
    *,
    alias: Optional[str] = None,
    creator: Optional[str] = None,
) -> Artifact:
    """Ingest an Agent Skills directory as an immutable ``agent-skill`` artifact.

    Every referenced file is hashed and stored in CAS individually; the
    artifact's own content is the canonical manifest referencing those file
    digests plus the frontmatter and body. Ingestion never writes into
    ``skill_dir``.
    """

    manifest = build_manifest(skill_dir)
    root = Path(skill_dir).resolve()

    for skill_file in manifest.files:
        data = (root / skill_file.relative_path).read_bytes()
        stored = workspace.cas.put_bytes(data)
        if stored.digest_hex != skill_file.sha256_hex:  # pragma: no cover - defensive
            raise LineageFormatError(f"digest mismatch while storing {skill_file.relative_path}")

    manifest_bytes = _manifest_json_bytes(manifest)
    artifact = workspace.ingest_artifact(
        manifest_bytes,
        kind=ArtifactKind.AGENT_SKILL,
        logical_name=manifest.name,
        mime_type="application/vnd.skillrewind.agent-skill-manifest+json",
        creator=creator,
        metadata={
            "agent_skills_manifest": manifest.to_dict(),
            "source_directory": str(root),
        },
        alias=alias,
    )
    workspace.audit.append(
        "artifact.skill-ingested",
        creator or workspace.config.actor,
        {"artifact_id": artifact.artifact_id, "file_count": len(manifest.files), "warnings": list(manifest.warnings)},
    )
    return artifact


def _manifest_json_bytes(manifest: SkillManifest) -> bytes:
    from ..canonical.json import canonical_bytes

    return canonical_bytes(manifest.to_dict())


def export_skill(workspace: Workspace, artifact_id: str, output_dir: str | Path) -> Path:
    """Reconstruct a valid Agent Skills directory from a previously ingested artifact."""

    artifact = workspace.artifacts.get(artifact_id)
    if artifact.kind != ArtifactKind.AGENT_SKILL:
        raise LineageFormatError(f"{artifact_id} is not an agent-skill artifact")
    manifest = artifact.metadata.get("agent_skills_manifest")
    if manifest is None:
        raise LineageFormatError(f"{artifact_id} has no stored Agent Skills manifest")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    frontmatter_yaml = yaml.safe_dump(manifest["frontmatter"], sort_keys=True, allow_unicode=True)
    (out / "SKILL.md").write_text(
        f"---\n{frontmatter_yaml}---\n{manifest['body']}", encoding="utf-8"
    )
    for entry in manifest["files"]:
        rel = Path(entry["path"])
        if rel.name == "SKILL.md" and str(rel) == "SKILL.md":
            continue
        dest = (out / rel).resolve()
        if out.resolve() not in dest.parents:
            raise CASPathTraversalError(f"manifest entry escapes export root: {entry['path']}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = workspace.cas.get_bytes(entry["sha256"])
        dest.write_bytes(data)
    return out
