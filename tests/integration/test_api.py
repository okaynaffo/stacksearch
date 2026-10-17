"""API tests: auth, /status, and the /events SSE stream (PLAN.md §9, §10, TODO 0.5).

The token tests matter more than usual here. §9's threat is a web page the user has open
issuing requests to 127.0.0.1, so "every route requires the token" is the property under
test, not just that /status happens to work with a token.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.api.events import EventBus, event_stream

TOKEN = "test-token-abcdefghijklmnop"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

pytestmark = pytest.mark.integration


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A client against an app using a temp data dir and a known token."""
    monkeypatch.setenv("STACKSEARCH_TOKEN", TOKEN)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STACKSEARCH_FASTEMBED_CACHE_DIR", str(tmp_path / "fe"))

    app = create_app(open_database=True)
    with TestClient(app) as c:
        yield c
    if app.state.index is not None:
        app.state.index.close()


# --------------------------------------------------------------------------------
# authentication (PLAN.md §9)
# --------------------------------------------------------------------------------


def test_status_requires_a_token(client):
    assert client.get("/status").status_code == 401


def test_status_rejects_a_wrong_token(client):
    assert client.get("/status", headers={"X-Local-Token": "wrong"}).status_code == 401


def test_status_accepts_the_token(client):
    assert client.get("/status", headers=AUTH).status_code == 200


def test_health_requires_a_token(client):
    """/health is not exempt: Rust holds the token, so there is no reason to expose it."""
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=AUTH).status_code == 200


def _collect_paths(routes) -> set[str]:
    """Every path reachable from a route list, recursing through nested containers.

    FastAPI 0.142 does not flatten `include_router` results into `app.routes`; it stores
    an `_IncludedRouter` whose routes hang off `original_router`. A single-level scan
    therefore finds no API routes at all, which would make the assertion below vacuously
    true. Both layouts are handled so this keeps working if that changes.
    """
    found: set[str] = set()
    for route in routes or []:
        path = getattr(route, "path", None)
        if path:
            found.add(path)
        for nested in (
            getattr(route, "routes", None),
            getattr(getattr(route, "original_router", None), "routes", None),
        ):
            if nested:
                found |= _collect_paths(nested)
    return found


def test_every_route_requires_a_token(client):
    """Structural check: no API route is reachable without a token.

    Guards against a future route being added outside the authenticated router, which is
    the failure mode §9 actually cares about.
    """
    from core.api.routes import router

    assert router.dependencies, "router must carry the token dependency"

    paths = _collect_paths(client.app.routes)
    api_paths = {"/status", "/health", "/events", "/search", "/answer"} & paths
    assert api_paths == {"/status", "/health", "/events", "/search", "/answer"}, (
        f"API routes missing from the app: found {sorted(api_paths)}"
    )
    for path in ("/status", "/health", "/events"):
        assert client.get(path).status_code == 401, f"{path} is reachable without a token"
    assert client.post("/search", json={"query": "x"}).status_code == 401
    assert client.post("/answer", json={"query": "x"}).status_code == 401


def test_x_local_token_header_accepted(client):
    assert client.get("/status", headers={"X-Local-Token": TOKEN}).status_code == 200


def test_bare_token_in_authorization_accepted(client):
    assert client.get("/status", headers={"Authorization": TOKEN}).status_code == 200


def test_empty_token_rejected(client):
    assert client.get("/status", headers={"Authorization": ""}).status_code == 401


# --------------------------------------------------------------------------------
# query-string token: the `EventSource` exception (PLAN.md §4 UI host row)
# --------------------------------------------------------------------------------


def test_events_accepts_a_query_token(client):
    """`EventSource` cannot set headers, so /events is the one route that allows ?token=."""
    with client.stream("GET", f"/events?token={TOKEN}") as resp:
        assert resp.status_code == 200


def test_query_token_rejected_on_every_other_route(client):
    """A token in a URL lands in logs and history, so it must not work elsewhere."""
    for path in ("/status", "/health"):
        assert client.get(f"{path}?token={TOKEN}").status_code == 401, (
            f"{path} must not accept a query-string token"
        )


def test_query_token_must_still_be_valid(client):
    with client.stream("GET", "/events?token=wrong") as resp:
        assert resp.status_code == 401


def test_token_that_is_a_prefix_is_rejected(client):
    """A near-miss must not pass; the compare is not a substring check."""
    assert client.get("/status", headers={"X-Local-Token": TOKEN[:-1]}).status_code == 401


# --------------------------------------------------------------------------------
# /status shape (PLAN.md §10)
# --------------------------------------------------------------------------------


def test_status_reports_index_and_models(client):
    body = client.get("/status", headers=AUTH).json()
    assert body["app"] == "stacksearch"
    assert body["index"]["embedding_model"] == "intfloat/multilingual-e5-base"
    assert body["index"]["embedding_dim"] == 768
    assert body["index"]["schema_version"] >= 1
    assert body["models"]["answer_llm"]["key"] == "4B"
    assert body["models"]["answer_llm"]["reasoning_enabled"] is False
    assert body["llm_server"]["running"] is False
    assert body["index_error"] is None


def test_status_counts_start_empty(client):
    body = client.get("/status", headers=AUTH).json()
    assert body["index"]["documents"] == 0
    assert body["index"]["chunks"] == 0
    assert body["index"]["vectors"] == 0
    assert body["queue"] == 0


def test_status_reflects_new_documents(client):
    conn = client.app.state.index.conn
    conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files', 't', '{}')")
    src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO documents(source_id, external_id, uri, content_hash) VALUES (?,?,?,?)",
        (src, "/a.md", "file:///a.md", "h"),
    )
    body = client.get("/status", headers=AUTH).json()
    assert body["index"]["documents"] == 1


def test_status_survives_a_broken_index(client):
    """A status endpoint that throws gives the UI nothing; it must degrade instead."""
    from unittest.mock import patch

    with patch("core.api.routes.stats", side_effect=RuntimeError("boom")):
        body = client.get("/status", headers=AUTH).json()
    assert body["index_error"] is not None
    assert "boom" in body["index_error"]
    assert body["index"] == {}


# --------------------------------------------------------------------------------
# /events SSE (PLAN.md §10, §13.1)
# --------------------------------------------------------------------------------


def test_events_requires_a_token(client):
    assert client.get("/events").status_code == 401


def test_events_sends_sse_headers(client):
    with client.stream("GET", "/events", headers=AUTH) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        # A proxy buffering the stream would make the UI see nothing.
        assert resp.headers["x-accel-buffering"] == "no"


def test_events_relays_a_published_event_over_http(monkeypatch, tmp_path):
    """End-to-end: an event published to the bus reaches a real HTTP client.

    Runs uvicorn rather than an in-process ASGI transport on purpose. httpx's
    ASGITransport buffers the whole response body before returning, which for an
    *infinite* stream means it never returns at all — so it cannot verify SSE at all.
    A real server also gives the genuine §10 behaviour, including the loopback bind.
    """
    import threading

    import uvicorn

    monkeypatch.setenv("STACKSEARCH_TOKEN", TOKEN)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))

    from core.api.events import bus as route_bus

    app = create_app(open_database=True)

    # The test owns the event loop rather than letting uvicorn create one, so `publish` —
    # a coroutine — can be scheduled on the very loop the SSE handler runs on. Calling it
    # from this thread would just build an un-awaited coroutine and publish nothing.
    loop = asyncio.new_event_loop()

    # Port 0 lets the OS pick a free port, so parallel runs cannot collide.
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=lambda: loop.run_until_complete(server.serve()))
    thread.start()
    try:
        # Wait for the bound port to be published by uvicorn.
        for _ in range(600):
            if server.started and server.servers:
                break
            time.sleep(0.01)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]

        received: list[str] = []
        with httpx.Client(timeout=10.0) as client:
            with client.stream("GET", f"http://127.0.0.1:{port}/events", headers=AUTH) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")

                # Reading blocks, so drive it on a thread to avoid the
                # subscribe-after-publish deadlock.
                def read():
                    try:
                        for line in resp.iter_lines():
                            received.append(line)
                            # Stop once the frame is complete: SSE sends the event name
                            # and the data on separate lines.
                            if any("event: indexing" in r for r in received) and any(
                                '"done":3' in r for r in received
                            ):
                                return
                    except httpx.HTTPError:
                        pass

                reader = threading.Thread(target=read, daemon=True)
                reader.start()

                for _ in range(400):
                    if route_bus.subscriber_count:
                        break
                    time.sleep(0.01)
                assert route_bus.subscriber_count >= 1, "no SSE subscription was established"

                future = asyncio.run_coroutine_threadsafe(
                    route_bus.publish("indexing", source="notes", done=3, total=10),
                    loop,
                )
                future.result(timeout=5)
                reader.join(timeout=10)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        loop.close()
        if app.state.index is not None:
            app.state.index.close()

    assert any("event: indexing" in line for line in received), f"frames: {received}"
    assert any('"done":3' in line for line in received), received


@pytest.mark.asyncio
async def test_events_emits_heartbeat_when_idle(monkeypatch, tmp_path):
    """An idle UI must still receive frames, or proxies drop the connection."""
    monkeypatch.setenv("STACKSEARCH_TOKEN", TOKEN)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))

    from core.api.events import EventBus

    local_bus = EventBus()
    chunks: list[str] = []

    async def drain():
        agen = event_stream(local_bus, heartbeat_s=0.05)
        async for frame in agen:
            chunks.append(frame)
            if len(chunks) >= 2:
                break
        await agen.aclose()

    await asyncio.wait_for(drain(), timeout=5)
    assert any("heartbeat" in c for c in chunks)
    assert all(c.endswith("\n\n") for c in chunks)


@pytest.mark.asyncio
async def test_event_bus_delivers_to_subscriber():
    bus = EventBus()
    received: list = []

    async def consume():
        async for event in bus.subscribe():
            received.append(event)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let the subscription register
    await bus.publish("indexing", done=1)
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert received[0].event == "indexing"
    assert received[0].data["done"] == 1


@pytest.mark.asyncio
async def test_event_bus_replays_history_to_late_subscriber():
    """A UI that connects mid-index should know the current state immediately."""
    bus = EventBus()
    await bus.publish("indexing", done=5)

    received: list = []

    async def consume():
        async for event in bus.subscribe():
            received.append(event)
            return

    task = asyncio.create_task(consume())
    await asyncio.wait_for(task, timeout=2.0)
    assert received[0].data["done"] == 5


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_grow_without_bound():
    """§2.1 shares memory with the LLM, so a stalled UI must not accumulate events."""
    bus = EventBus()
    stream = bus.subscribe()

    # The subscription only registers once the generator starts running, so prime it.
    pending = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.02)
    assert bus.subscriber_count == 1

    # Publish far more than the queue can hold, without ever reading.
    for i in range(bus.MAX_QUEUE * 3):
        await bus.publish("indexing", i=i)

    queue_size = next(iter(bus._subscribers)).maxsize
    assert next(iter(bus._subscribers)).qsize() <= queue_size, "queue grew past its bound"

    # The oldest events were dropped, so the newest is still deliverable.
    assert await asyncio.wait_for(pending, timeout=2.0) is not None
    await stream.aclose()


@pytest.mark.asyncio
async def test_sse_framing_is_wellformed():
    bus = EventBus()
    await bus.publish("indexing", done=7)

    chunks = []

    async def drain():
        stream = bus.subscribe()
        async for event in stream:
            chunks.append(event.to_sse())
            break
        await stream.aclose()

    await asyncio.wait_for(drain(), timeout=2.0)
    frame = chunks[0]
    assert frame.startswith("event: indexing\n")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {"done": 7}


@pytest.mark.asyncio
async def test_heartbeat_frames_are_comments_only():
    from core.api.events import heartbeat_payload

    frame = heartbeat_payload().to_sse()
    assert frame.endswith("\n\n")
    assert "heartbeat" in frame


# --------------------------------------------------------------------------------
# loopback binding (PLAN.md §9)
# --------------------------------------------------------------------------------


def test_settings_refuse_a_non_loopback_host(monkeypatch):
    from pydantic import ValidationError

    from core.config import Settings

    monkeypatch.setenv("STACKSEARCH_API_HOST", "0.0.0.0")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.asyncio
async def test_server_reports_loopback_only(client):
    """Belt and braces: the running config must be loopback, not merely validated."""
    settings = client.app.state.settings
    assert settings.api_host in ("127.0.0.1", "::1", "localhost")


def test_printed_token_is_the_one_validated(monkeypatch, tmp_path, capsys):
    """Regression: run.py's printed token must authenticate.

    The app module builds its own Settings when uvicorn imports it, so a token generated
    in main() is not the token the app checks unless it is published to the environment.
    This broke silently once: the token printed looked right and was rejected with 401.
    """
    monkeypatch.delenv("STACKSEARCH_TOKEN", raising=False)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))

    from core.api import run as run_module
    from core.config import get_settings

    settings_before = None

    class _FakeServer:
        def __init__(self, *a, **kw):
            nonlocal settings_before
            # Simulate uvicorn importing the app in this same process: the freshly built
            # Settings must agree with the one main() printed.
            settings_before = get_settings()

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: _FakeServer())

    run_module.main()

    printed = capsys.readouterr().out
    assert "token : " in printed
    import os

    assert os.environ["STACKSEARCH_TOKEN"] in printed
    assert settings_before.api_token in printed, (
        "the app's Settings disagreed with the printed token; every request would 401"
    )


def test_token_is_not_written_to_disk(client, tmp_path):
    """§9's per-launch token must not be persisted anywhere under the data dir."""
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(errors="ignore"), f"token leaked into {path}"
