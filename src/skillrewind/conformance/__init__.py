"""Machine-readable Integration Level requirements + a local self-test
(Phase C2.4 section 9).

This is the internal foundation for future external conformance testing,
not an SDK: `describe()` returns the same contract every external adapter
implementer would need, and `self_test()` proves SkillRewind's own
Service-mode API satisfies its own stable integration contract (see
`docs/integration-contract-v1.md`) using only local, deterministic
fixtures -- no network beyond the loopback interface, no paid API.
"""

from __future__ import annotations

from .levels import CONTRACT_VERSION, LEVEL_REQUIREMENTS, IntegrationLevel, describe
from .self_test import CheckResult, SelfTestReport, run_self_test

__all__ = [
    "CONTRACT_VERSION",
    "LEVEL_REQUIREMENTS",
    "IntegrationLevel",
    "describe",
    "CheckResult",
    "SelfTestReport",
    "run_self_test",
]
