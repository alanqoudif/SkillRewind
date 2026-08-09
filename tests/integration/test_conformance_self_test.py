"""Phase C2.4 section 9: `skillrewind.conformance` proves the local
Service-mode API satisfies its own stable integration contract."""

from __future__ import annotations

from skillrewind.conformance import CONTRACT_VERSION, LEVEL_REQUIREMENTS, describe, run_self_test


def test_describe_reports_three_levels_with_growing_requirements() -> None:
    payload = describe()
    assert payload["contract_version"] == CONTRACT_VERSION
    levels = {lvl["level"]: lvl for lvl in payload["levels"]}
    assert set(levels) == {1, 2, 3}
    assert set(levels[1]["required_capabilities"]) < set(levels[2]["required_capabilities"])
    assert set(levels[2]["required_capabilities"]) < set(levels[3]["required_capabilities"])
    assert set(levels[1]["required_endpoints"]) < set(levels[2]["required_endpoints"])
    assert set(levels[2]["required_endpoints"]) < set(levels[3]["required_endpoints"])


def test_level_requirements_module_matches_describe() -> None:
    for level, spec in LEVEL_REQUIREMENTS.items():
        assert spec.level == level
        assert spec.required_capabilities
        assert spec.required_endpoints


def test_self_test_passes_against_local_service_mode_api(tmp_path) -> None:
    report = run_self_test(workdir=tmp_path / "conformance")
    failed = [c for c in report.checks if not c.ok]
    assert not failed, failed
    assert report.ok
    assert len(report.checks) >= 12
