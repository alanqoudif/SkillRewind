"""Temporal-trace features: derivation-time proximity."""

from __future__ import annotations

from datetime import datetime


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def temporal_proximity(
    timestamp_a: str | None, timestamp_b: str | None, *, half_life_seconds: float = 3600.0
) -> float:
    """Decay-based temporal proximity in [0, 1]; 1.0 at zero distance, 0.5 at one half-life."""

    dt_a = _parse_iso(timestamp_a)
    dt_b = _parse_iso(timestamp_b)
    if dt_a is None or dt_b is None:
        return 0.0
    delta_seconds = abs((dt_a - dt_b).total_seconds())
    return 0.5 ** (delta_seconds / half_life_seconds)
