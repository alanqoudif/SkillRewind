"""Phase C2.4 section 7: the committed `docs/openapi-v1.json` must always
match what the real, live FastAPI app currently generates -- never a
hand-maintained document that silently drifts from the actual API. Run
`make openapi` to regenerate it after any route/schema change."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_committed_openapi_matches_live_app() -> None:
    import skillrewind.jobs.handlers  # noqa: F401  (registers job handlers referenced by route docs)
    from skillrewind.api.app import create_app
    from skillrewind.config import SkillRewindConfig

    committed_path = REPO_ROOT / "docs" / "openapi-v1.json"
    assert committed_path.exists(), "docs/openapi-v1.json is missing -- run `make openapi`"
    committed = json.loads(committed_path.read_text())

    app = create_app(SkillRewindConfig(mode="service", database_url="sqlite:///:memory:", cas_root=str(REPO_ROOT / ".skillrewind-openapi-check-cas")))
    live = app.openapi()

    assert committed == live, (
        "docs/openapi-v1.json is stale relative to the live FastAPI app's OpenAPI document. "
        "Run `make openapi` to regenerate it and commit the result."
    )
