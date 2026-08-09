"""Revocation state machine: enforced transitions, transactional and audited."""

from __future__ import annotations

from typing import Protocol

from ..domain.enums import ALLOWED_TRANSITIONS, RevocationState
from ..domain.errors import InvalidStateTransitionError
from ..domain.models import RevocationEvent
from ..workspace import timestamp


class RevocationRepositoryLike(Protocol):
    """Structural contract satisfied by both `skillrewind.persistence.
    repositories.RevocationRepository` (Lite mode) and `skillrewind.
    persistence.service.workspace._ServiceRevocations` (Service mode) --
    see `skillrewind.workspace_protocol` for the equivalent whole-workspace
    contract and its reuse rationale."""

    def update(self, event: RevocationEvent) -> None: ...

    def record_transition(self, event_id: str, from_state: str | None, to_state: str, at: str) -> None: ...


def transition(
    repo: RevocationRepositoryLike, event: RevocationEvent, to_state: RevocationState
) -> RevocationEvent:
    if event.state == to_state:
        # Re-entering the state we are already in is a no-op, not an error --
        # this is what makes a resumed/retried job safe to call transition()
        # again for a stage it already reached before a crash.
        return event
    allowed = ALLOWED_TRANSITIONS.get(event.state, ())
    if to_state not in allowed:
        raise InvalidStateTransitionError(
            f"revocation {event.event_id}: cannot transition {event.state.value} -> {to_state.value} "
            f"(allowed: {[s.value for s in allowed]})"
        )
    from_state = event.state
    event.state = to_state
    now = timestamp()
    if to_state in (
        RevocationState.COMPLETED,
        RevocationState.COMPLETED_WITH_UNRESOLVED,
        RevocationState.FAILED,
        RevocationState.CANCELLED_BEFORE_BARRIER,
    ):
        event.completed_at = now
    repo.update(event)
    repo.record_transition(event.event_id, from_state.value, to_state.value, now)
    return event
