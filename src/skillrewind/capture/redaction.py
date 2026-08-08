"""Redaction of secret-shaped values before they reach logs, metadata, or telemetry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_DEFAULT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_SENSITIVE_KEYS = {"password", "api_key", "apikey", "secret", "token", "authorization", "private_key"}
REDACTED = "[REDACTED]"


@dataclass
class Redactor:
    """Redacts common credential patterns and sensitive key names, recursively."""

    extra_patterns: list[re.Pattern[str]] = field(default_factory=list)

    def _patterns(self) -> list[re.Pattern[str]]:
        return [*_DEFAULT_PATTERNS, *self.extra_patterns]

    def _redact_string(self, value: str) -> str:
        for pattern in self._patterns():
            value = pattern.sub(REDACTED, value)
        return value

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                    result[key] = REDACTED
                else:
                    result[key] = self.redact(item)
            return result
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value
