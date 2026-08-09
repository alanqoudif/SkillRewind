"""Job error taxonomy: retryable vs. permanent, per master spec section 7.2."""

from __future__ import annotations


class JobError(Exception):
    """Base class for errors a handler raises to report structured job failure."""

    error_code = "unknown"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.sanitized_message = message


class RetryableJobError(JobError):
    """Transient failure -- the queue should retry with backoff, up to max_attempts."""

    error_code = "retryable"


class PermanentJobError(JobError):
    """Non-retryable failure -- the job goes straight to the dead letter (`failed`)."""

    error_code = "permanent"
