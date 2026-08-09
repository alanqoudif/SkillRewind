"""In-memory rate limiter.

Master spec 8.6: "in-memory limiter allowed only for Lite mode ... PostgreSQL-
backed or clearly documented single-instance limiter for Service mode; Redis
is optional, not mandatory." This implementation is a single-process,
in-memory token bucket. It is honest about being single-instance -- it does
not claim distributed correctness across multiple API processes. A
PostgreSQL-backed limiter for genuinely multi-instance Service-mode
deployments is not implemented in this increment (tracked in
docs/completion-matrix-v0.3.md).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


@dataclass
class RateLimiter:
    capacity: int = 60
    refill_per_second: float = 1.0
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def allow(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""

        now = self.clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity - 1, last_refill=now)
            self._buckets[key] = bucket
            return True, 0.0

        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
        bucket.last_refill = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        retry_after = (1.0 - bucket.tokens) / self.refill_per_second
        return False, retry_after
