"""Revocation state machine: enforced transitions, transactional and audited."""

from __future__ import annotations

from ..domain.enums import ALLOWED_TRANSITIONS, RevocationState
from ..domain.errors import InvalidStateTransitionError
from ..domain.models import RevocationEvent
from ..persistence.repositories import RevocationRepository
from ..workspace import timestamp


def transition(
    repo: RevocationRepository, event: RevocationEvent, to_state: RevocationState
) -> RevocationEvent:
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
