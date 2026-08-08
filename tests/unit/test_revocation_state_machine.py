from __future__ import annotations

import pytest

from skillrewind.domain.enums import RevocationPolicy, RevocationState, Severity
from skillrewind.domain.errors import InvalidStateTransitionError
from skillrewind.domain.models import RevocationEvent
from skillrewind.persistence.repositories import RevocationRepository
from skillrewind.persistence.database import connect_memory
from skillrewind.revocation.state_machine import transition


def _new_event() -> tuple[RevocationRepository, RevocationEvent]:
    conn = connect_memory()
    repo = RevocationRepository(conn)
    event = RevocationEvent(
        event_id="e1", roots=["skill://x"], reason="test", severity=Severity.HIGH,
        policy=RevocationPolicy.BALANCED, actor="tester", idempotency_key="k1",
        created_at="2026-01-01T00:00:00Z",
    )
    repo.insert(event)
    return repo, event


def test_valid_transition_sequence_succeeds():
    repo, event = _new_event()
    event = transition(repo, event, RevocationState.BARRIER_APPLIED)
    assert event.state == RevocationState.BARRIER_APPLIED
    event = transition(repo, event, RevocationState.CANDIDATE_RECOVERY)
    assert event.state == RevocationState.CANDIDATE_RECOVERY


def test_invalid_transition_raises():
    repo, event = _new_event()
    with pytest.raises(InvalidStateTransitionError):
        transition(repo, event, RevocationState.COMPLETED)


def test_completed_states_are_terminal():
    repo, event = _new_event()
    event = transition(repo, event, RevocationState.BARRIER_APPLIED)
    event = transition(repo, event, RevocationState.CANDIDATE_RECOVERY)
    event = transition(repo, event, RevocationState.COMPLETED)
    assert event.completed_at is not None
    with pytest.raises(InvalidStateTransitionError):
        transition(repo, event, RevocationState.QUARANTINE_APPLIED)


def test_cancel_before_barrier_is_terminal_and_never_reactivates():
    repo, event = _new_event()
    event = transition(repo, event, RevocationState.CANCELLED_BEFORE_BARRIER)
    with pytest.raises(InvalidStateTransitionError):
        transition(repo, event, RevocationState.BARRIER_APPLIED)
