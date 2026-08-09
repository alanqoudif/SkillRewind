"""Phase C2.4 gap D: end-to-end HTTP SSE resume test against the real
FastAPI `/api/v1/events/stream` endpoint over a real socket (not just
`JobQueue.events()` directly, and not through `httpx.ASGITransport` /
`TestClient`, which buffer the entire response body until the ASGI
coroutine returns and therefore cannot exercise a genuinely open-ended SSE
stream -- see the module-level note below). Proves the documented resume
contract over the wire:

    1. A client connects.
    2. It receives persisted job events.
    3. It disconnects after event N.
    4. More events are persisted.
    5. It reconnects with `Last-Event-ID: N`.
    6. It receives only events N+1 onward.
    7. Ordering is preserved.
    8. No duplicate event is returned because of resume behavior.

Uses a real `uvicorn.Server` on a loopback socket in a background thread so
the client can genuinely half-close its connection while the server-side
generator (which polls forever for a non-terminal job) keeps running
independently -- exactly the scenario the milestone spec asks for.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy.orm import Session

from skillrewind.api.app import create_app
from skillrewind.api.auth import create_api_key
from skillrewind.config import SkillRewindConfig
from skillrewind.jobs.queue import JobQueue
from skillrewind.persistence.service.engine import build_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrate(db_path: Path) -> None:
    env = dict(os.environ)
    env["SKILLREWIND_DATABASE_URL"] = f"sqlite:///{db_path}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread:
    """Runs a real uvicorn server for the app on a background thread so the
    test can hold a genuinely open socket to a still-running, non-terminal
    SSE stream and independently mutate the database from the main thread."""

    def __init__(self, app) -> None:
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started, "uvicorn server failed to start within 10s"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "sse.db"
    _migrate(db_path)
    return SkillRewindConfig(mode="service", database_url=f"sqlite:///{db_path}", cas_root=str(tmp_path / "cas"))


@pytest.fixture
def read_key(config):
    engine = build_engine(config.database_url)
    with Session(engine) as session:
        created = create_api_key(session, name="sse-key", actor="sse-tester", scopes=["read"])
    return created.plaintext


@pytest.fixture
def server(config):
    app = create_app(config)
    srv = _ServerThread(app)
    srv.start()
    yield srv
    srv.stop()


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _parse_sse(raw_text: str) -> list[dict]:
    events: list[dict] = []
    current: dict = {}
    for line in raw_text.splitlines():
        if line == "":
            if current:
                events.append(current)
                current = {}
            continue
        if line.startswith(":"):
            continue  # comment/heartbeat
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "id":
            current["id"] = int(value)
        elif field == "event":
            current["event"] = value
        elif field == "data":
            current.setdefault("data", "")
            current["data"] += value
    if current:
        events.append(current)
    return events


def test_http_sse_resume_no_duplicates_no_gaps(server, config, read_key):
    engine = build_engine(config.database_url)
    queue = JobQueue(engine)

    # -- 1: create a job with one persisted event ("job.enqueued") --
    job_id = queue.enqueue("test.sse-resume-noop", {})

    # -- 2-3: client connects over a real socket, receives the persisted
    # event while the job is still non-terminal (so the server-side
    # generator is genuinely still polling), then disconnects --
    collected_raw = ""
    last_seen_id: int
    with httpx.Client(base_url=server.base_url, timeout=10.0) as client:
        with client.stream(
            "GET", "/api/v1/events/stream", params={"job_id": job_id, "poll_interval": 0.05}, headers=_auth(read_key)
        ) as response:
            assert response.status_code == 200
            for chunk in response.iter_text():
                collected_raw += chunk
                if _parse_sse(collected_raw):
                    break  # got at least one full event -- now disconnect
    first_events = _parse_sse(collected_raw)
    assert len(first_events) >= 1
    last_seen_id = first_events[-1]["id"]
    for e in first_events:
        json.loads(e["data"])  # data must be valid JSON

    # -- 4: more events are persisted on the job after the client
    # disconnected (server keeps running independently on its thread) --
    queue.claim("resume-test-worker")  # emits "job.claimed"
    queue.complete(job_id, "resume-test-worker")  # emits "job.succeeded" (terminal)

    all_events_via_queue = queue.events(job_id, after_event_id=0)
    assert len(all_events_via_queue) >= 3, "expected enqueued + claimed + succeeded events to be persisted"

    # -- 5-6: reconnect with Last-Event-ID; must receive only events after
    # last_seen_id. The job is now terminal, so this stream naturally ends. --
    with httpx.Client(base_url=server.base_url, timeout=10.0) as client:
        with client.stream(
            "GET",
            "/api/v1/events/stream",
            params={"job_id": job_id, "poll_interval": 0.05},
            headers={**_auth(read_key), "Last-Event-ID": str(last_seen_id)},
        ) as response:
            assert response.status_code == 200
            resumed_raw = response.read().decode()
    resumed_events = _parse_sse(resumed_raw)

    assert resumed_events, "resumed stream must deliver the events persisted after disconnect"
    resumed_ids = [e["id"] for e in resumed_events]

    # -- 7: ordering is preserved --
    assert resumed_ids == sorted(resumed_ids)

    # -- 6/8: strictly greater than the last seen id, and no duplicates --
    assert all(rid > last_seen_id for rid in resumed_ids), (
        f"resumed stream must never re-deliver event <= {last_seen_id}, got {resumed_ids}"
    )
    assert len(resumed_ids) == len(set(resumed_ids)), "resumed stream must never duplicate an event id"

    # -- cross-check against the full ground-truth event log: first +
    # resumed batches together must equal the full ordered log, no gaps --
    full_ids = [e["event_id"] for e in all_events_via_queue]
    combined_ids = sorted({e["id"] for e in first_events} | set(resumed_ids))
    assert combined_ids == full_ids, (first_events, resumed_events, all_events_via_queue)
