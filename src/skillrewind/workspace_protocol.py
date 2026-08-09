"""Structural type for "anything shaped like a workspace" (Phase C2.3).

`skillrewind.workspace.Workspace` (Lite mode) and `skillrewind.persistence.
service.workspace.ServiceWorkspace` (Service mode) are two different
concrete classes with no common base class -- deliberately, so Service mode
never depends on Lite's raw-sqlite3 internals or vice versa. But a large
body of already-tested domain logic (revocation orchestration, barrier
application, quarantine, waivers, clean-room rebuild, verification,
attestation building) is written entirely in terms of `workspace.<repo>.
<method>()` calls and needs to run unchanged against either one -- that
reuse, not a second parallel implementation, is the point (see
`skillrewind.persistence.service.workspace`'s module docstring).

`WorkspaceLike` is that shared structural contract, used only for type
hints on the reused functions; it is never instantiated. Both concrete
workspace classes already satisfy it without inheriting from it (Python
`Protocol`s check structurally), so this file introduces no coupling
between the two persistence layers.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .config import SkillRewindConfig
from .domain.enums import ArtifactKind


class WorkspaceLike(Protocol):
    config: SkillRewindConfig
    # Declared `Any`, not `ContentAddressedStore`: Protocol attribute checks
    # are invariant, and both concrete CAS implementations (`LocalCAS` for
    # Lite, the Service-mode CAS) are subtypes of that protocol rather than
    # the protocol type itself, which invariance would otherwise reject.
    cas: Any
    artifacts: Any
    derivations: Any
    edges: Any
    replays: Any
    revocations: Any
    waivers: Any
    audit: Any

    def ingest_artifact(
        self,
        content: bytes,
        *,
        kind: ArtifactKind,
        logical_name: str,
        mime_type: str = ...,
        creator: Optional[str] = ...,
        metadata: Optional[dict[str, Any]] = ...,
        alias: Optional[str] = ...,
    ) -> Any: ...

    def resolve_alias(self, alias: str) -> Any: ...
