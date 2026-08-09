"""Versioned verification suites and safe verifier checks.

Verifiers only ever compare already-produced, stored structured output
(a rebuilt artifact's fixture/probe output and metadata) against declared
expectations. There is no arbitrary host command execution from suite
definitions -- the only "executable" check type
(``python-unit-test-fixture``) is restricted to allowlisted sandbox fixture
scripts via :mod:`skillrewind.replay.sandbox`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..domain.enums import LifecycleStatus
from ..workspace import timestamp
from ..workspace_protocol import WorkspaceLike

SUITE_SCHEMA_VERSION = "0.2"


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    check_type: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationSuite:
    suite_id: str
    version: str
    canary_keys: tuple[str, ...] = ()
    forbid_support_status: tuple[str, ...] = (LifecycleStatus.REVOKED.value, LifecycleStatus.QUARANTINED.value)
    utility_retention_threshold: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "version": self.version,
            "schema_version": SUITE_SCHEMA_VERSION,
            "canary_keys": list(self.canary_keys),
            "forbid_support_status": list(self.forbid_support_status),
            "utility_retention_threshold": self.utility_retention_threshold,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    suite_id: str
    suite_version: str
    artifact_id: str
    checks: tuple[CheckResult, ...]
    status: str  # "pass" | "fail" | "partial"
    target_behavior_observed: dict[str, Any]
    clean_utility_score: Optional[float]
    retained_utility_ratio: Optional[float]
    limitations: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "artifact_id": self.artifact_id,
            "checks": [
                {"check_id": c.check_id, "check_type": c.check_type, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "status": self.status,
            "target_behavior_observed": self.target_behavior_observed,
            "clean_utility_score": self.clean_utility_score,
            "retained_utility_ratio": self.retained_utility_ratio,
            "limitations": list(self.limitations),
            "created_at": self.created_at,
        }


def run_suite(
    workspace: WorkspaceLike,
    artifact_id: str,
    suite: VerificationSuite,
    *,
    fixture_output: dict[str, Any],
    baseline_utility: Optional[dict[str, float]] = None,
) -> VerificationReport:
    checks: list[CheckResult] = []

    for key in suite.canary_keys:
        observed = bool(fixture_output.get(key, False))
        checks.append(
            CheckResult(
                check_id=f"canary-absent:{key}",
                check_type="canary-absent",
                passed=not observed,
                detail=f"{key}={observed}",
            )
        )

    clean_support_edges = [
        e for e in workspace.edges.incoming(artifact_id) if e.relation.value == "clean-support"
    ]
    forbidden_found = []
    for edge in clean_support_edges:
        try:
            support = workspace.artifacts.get(edge.source)
        except Exception:
            continue
        if support.status.value in suite.forbid_support_status:
            forbidden_found.append(edge.source)
    checks.append(
        CheckResult(
            check_id="predecessor-closure-no-forbidden-support",
            check_type="predecessor-closure",
            passed=not forbidden_found,
            detail=f"forbidden_support={forbidden_found}" if forbidden_found else "no forbidden support present",
        )
    )

    utility = fixture_output.get("_utility", {})
    clean_utility_score = utility.get("task_success") if isinstance(utility, dict) else None
    retained_ratio = None
    if baseline_utility and clean_utility_score is not None:
        baseline_score = baseline_utility.get("task_success")
        if baseline_score:
            retained_ratio = clean_utility_score / baseline_score
            checks.append(
                CheckResult(
                    check_id="utility-retention",
                    check_type="utility-retention-threshold",
                    passed=retained_ratio >= suite.utility_retention_threshold,
                    detail=f"retained_ratio={retained_ratio:.4f}",
                )
            )

    all_passed = all(c.passed for c in checks)
    any_passed = any(c.passed for c in checks)
    status = "pass" if all_passed else ("partial" if any_passed else "fail")

    return VerificationReport(
        suite_id=suite.suite_id,
        suite_version=suite.version,
        artifact_id=artifact_id,
        checks=tuple(checks),
        status=status,
        target_behavior_observed=fixture_output,
        clean_utility_score=clean_utility_score,
        retained_utility_ratio=retained_ratio,
        limitations=(
            "Verification runs against deterministic stored probe/fixture output only; "
            "it does not execute the rebuilt artifact against a live, uncontrolled environment.",
        ),
        created_at=timestamp(),
    )


DEFAULT_POISONED_DESCENDANT_SUITE = VerificationSuite(
    suite_id="poisoned-descendant-canary-suite",
    version="0.1.0",
    canary_keys=("mock_disable_verification",),
    utility_retention_threshold=0.9,
)
