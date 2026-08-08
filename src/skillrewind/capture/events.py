"""Generic trace event type constants for JSONL import."""

from __future__ import annotations

SESSION_STARTED = "session-started"
TASK_LOADED = "task-loaded"
CONTEXT_CANDIDATE_AVAILABLE = "context-candidate-available"
SKILL_ACTIVATED = "skill-activated"
MEMORY_RETRIEVED = "memory-retrieved"
PROMPT_ASSEMBLED = "prompt-assembled"
TOOL_CALLED = "tool-called"
TOOL_RETURNED = "tool-returned"
ARTIFACT_PRODUCED = "artifact-produced"
VALIDATION_COMPLETED = "validation-completed"
DERIVATION_COMPLETED = "derivation-completed"
DERIVATION_FAILED = "derivation-failed"

ALL_EVENT_TYPES = frozenset(
    {
        SESSION_STARTED,
        TASK_LOADED,
        CONTEXT_CANDIDATE_AVAILABLE,
        SKILL_ACTIVATED,
        MEMORY_RETRIEVED,
        PROMPT_ASSEMBLED,
        TOOL_CALLED,
        TOOL_RETURNED,
        ARTIFACT_PRODUCED,
        VALIDATION_COMPLETED,
        DERIVATION_COMPLETED,
        DERIVATION_FAILED,
    }
)
