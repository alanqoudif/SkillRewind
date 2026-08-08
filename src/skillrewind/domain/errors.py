"""Domain-level exceptions."""

from __future__ import annotations


class SkillRewindError(Exception):
    """Base class for all SkillRewind domain errors."""


class InvalidArtifactIdError(SkillRewindError):
    """Raised when an artifact ID does not match the required scheme."""


class CanonicalizationError(SkillRewindError):
    """Raised when a value cannot be canonicalized deterministically."""


class CASIntegrityError(SkillRewindError):
    """Raised when content-addressed storage detects corruption or a digest mismatch."""


class CASPathTraversalError(SkillRewindError):
    """Raised when a request would escape the CAS root or a skill root."""


class AuditChainError(SkillRewindError):
    """Raised when the hash-chained audit log fails verification."""


class LineageFormatError(SkillRewindError):
    """Raised when an edge/artifact record is malformed."""


class InvalidStateTransitionError(SkillRewindError):
    """Raised when a revocation state machine transition is not permitted."""


class PolicyViolationError(SkillRewindError):
    """Raised when an action violates the active revocation policy."""


class NotFoundError(SkillRewindError):
    """Raised when a requested domain object does not exist."""


class UnapprovedRunnerError(SkillRewindError):
    """Raised when a replay/rebuild runner is not in the approved registry."""
