"""Policy-specific quarantine decisions for unresolved/rejected candidates."""

from __future__ import annotations

from ..config import SkillRewindConfig
from ..domain.enums import RevocationPolicy, Severity

_HIGH_SEVERITY = {Severity.HIGH, Severity.CRITICAL}


def should_quarantine_unresolved(
    *, policy: RevocationPolicy, severity: Severity, config: SkillRewindConfig
) -> bool:
    """Whether an unresolved (non-confirmed, non-rejected) candidate should be
    quarantined even without replay confirmation.

    - ``forensic``: never (no serving-state changes at all).
    - ``strict``: always for high/critical severity (fail closed).
    - ``balanced``: only if explicitly configured to do so for high/critical severity.
    """

    if policy == RevocationPolicy.FORENSIC:
        return False
    if severity not in _HIGH_SEVERITY:
        return False
    if policy == RevocationPolicy.STRICT:
        return True
    return config.balanced_quarantine_unresolved_high_severity
