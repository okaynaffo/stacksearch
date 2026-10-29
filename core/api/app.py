"""The core service application (PLAN.md §10).

Two host modes, one build (PLAN.md §4 "UI host" row, §12):

* **Development** — this service mounts ``app/ui/dist`` at ``/``, so the UI is built
  once and developed in a browser at ``127.0.0.1:8000`` with no webview reload cycle.
* **Shipped** — the Tauri window loads the identical assets in Phase 6.

Nothing is rewritten at that boundary, which is the whole reason the UI is a static build
rather than a Tauri dev server.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.api.events import bus
from core.api.routes import router
from core.config import REPO_ROOT, get_settings
from core.debug_log import (
    log_event,
    log_exception,
    log_paths,
    new_request_id,
    process_info,
    redact_query,
    set_request_id,
    setup_logging,
)
from core.index import open_index

UI_DIST = REPO_ROOT / "app" / "ui" / "dist"

#: Routes the UI polls continuously. Their request/response records are kept at DEBUG
#: level so an investigation can filter them out without losing them.
_QUIET_PATHS = frozenset(
    {"/status", "/health", "/sources", "/events", "/debug/log", "/debug/logs"}
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Phase 3 index worker + watcher alongside the service (§6.6, §6.2)."""
    worker = getattr(app.state, "worker", None)
    watcher = getattr(app.state, "watcher", None)
    settings = get_settings()
    log_event(
        "process",
        "startup",
        **process_info(),
        api_host=settings.api_host,
        api_port=settings.api_port,
        data_dir=str(settings.data_dir),
        index_path=str(settings.index_db_path),
        logs=log_paths(),
        worker=worker is not None,
        watcher=watcher is not None,
        sources=_source_count(getattr(app.state, "index", None)),
    )
    if worker is not None:
        log_event("worker", "starting")
        worker.start()
        log_event("worker", "started", state=worker.state)
    if watcher is not None:
        # Live watching is DISABLED on purpose.
        #
        # watchdog's FSEvents backend calls FSEventStreamCreate -> open() on the root,
        # and for a TCC-gated path like ~/Documents that call blocks while macOS waits
        # on the "Files and Folders" prompt. Critically, the C extension holds the GIL
        # across that blocking syscall, so a background thread cannot help: the GIL is
        # starved and uvicorn's event loop can never accept a connection. The API binds
        # but never answers, and the window shows a blank page.
        #
        # Watching is a convenience (auto-reindex on edit), not a requirement (§6.2):
        # adding a source enqueues an initial scan, the reindex button forces a rescan,
        # and the worker reconciles on a schedule. Correctness does not depend on it.
        # The watcher object is still built so SourceChanged-driven paths keep working.
        pass
    try:
        yield
    finally:
        log_event("process", "shutdown", reason="lifespan-finally")
        if watcher is not None:
            try:
                watcher.stop()
                log_event("watcher", "stopped")
            except Exception as exc:  # noqa: BLE001, S110
                log_exception("watcher", "stop-failed", exc)
        if worker is not None:
            log_event("worker", "stopping", state=worker.state)
            worker.stop()
            log_event("worker", "stopped", state=worker.state)
        # Stop the model server explicitly: it runs in its own session, so the host's
        # process-group signal does not reach it. Leaving it resident both keeps ~3 GB
        # allocated and poisons the next launch, whose new token the old server rejects.
        try:
            from core.llm.server import shutdown_supervisor

            shutdown_supervisor("core shutdown")
        except Exception as exc:  # noqa: BLE001
            log_exception("llm", "shutdown-failed", exc)


def _source_count(index: object | None) -> int | None:
    """Best-effort source count for the startup record."""
    try:
        return index.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def create_app(*, open_database: bool = True) -> FastAPI:
    """Build the FastAPI application.

    ``open_database=False`` skips opening the index, which the API tests use so they do
    not touch the real data directory.
    """
    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    app = FastAPI(
        title="StackSearch",
        version="0.1.0",
        description="Private, local-first personal search engine (PLAN.md).",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _log_http(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Record every HTTP request/response, including duration and outcome.

        This is the backbone of the audit trail: a route that forgets to log its own
        action still leaves a request line behind, and a crash mid-request leaves the
        ``begin`` line without a matching ``end``.
        """
        request_id = new_request_id()
        set_request_id(request_id)
        started = time.perf_counter()
        path = request.url.path
        query = redact_query(request.url.query)
        quiet = path in _QUIET_PATHS
        level = "DEBUG" if quiet else "INFO"
        log_event(
            "api.request",
            "begin",
            level=level,
            method=request.method,
            path=path,
            query=query,
            client=request.client.host if request.client else None,
            content_length=request.headers.get("content-length"),
        )
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 — log, then let FastAPI's handler reply
            log_exception(
                "api.error",
                "unhandled",
                exc,
                method=request.method,
                path=path,
                query=query,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            set_request_id(None)
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        status_code = response.status_code
        response_level = "WARNING" if status_code >= 400 else level
        log_event(
            "api.response",
            "end",
            level=response_level,
            method=request.method,
            path=path,
            query=query,
            status_code=status_code,
            duration_ms=duration_ms,
            response_length=response.headers.get("content-length"),
            streaming=(response.headers.get("content-type", "").startswith("text/event-stream")),
        )
        set_request_id(None)
        return response
    app.state.started_at = time.time()
    app.state.settings = settings
    app.state.index = None
    app.state.llm_supervisor = None
    app.state.embedder = None
    app.state.worker = None
    app.state.watcher = None
    app.state.policy = None

    if open_database:
        # Migrations and the embedding-model reconcile happen here so a dimension change
        # is handled before the first query rather than during one (PLAN.md §5).
        app.state.index = open_index(settings.index_db_path)
        from core.jobs.policy import policy as shared_policy
        from core.jobs.worker import Worker

        app.state.policy = shared_policy

        def _progress(event: str, **data: object) -> None:
            bus.publish_sync(event, **data)

        app.state.worker = Worker(settings.index_db_path, policy=shared_policy, progress=_progress)

        # Live watching (§6.2): one Observer for every enabled file/pdf source.
        # Best effort — a source whose root is missing is skipped, and a total
        # watchdog failure degrades to reconcile scans rather than no startup.
        import json as _json

        from core.connectors.watch import WatchManager

        _watcher = WatchManager(settings.index_db_path)
        try:
            for _row in app.state.index.conn.execute(
                "SELECT id, kind, config_json FROM sources WHERE enabled != 0"
                " AND kind IN ('files', 'pdf')"
            ).fetchall():
                try:
                    _watcher.add_source(_row["id"], _row["kind"], _json.loads(_row["config_json"]))
                except Exception:  # noqa: BLE001, S112
                    continue
        except Exception:  # noqa: BLE001, S110
            pass
        app.state.watcher = _watcher

    app.include_router(router)

    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI) -> None:
    """Serve the static UI build, mounted outside the token requirement.

    The UI's own fetches carry the token (§9); the static assets themselves are not
    sensitive, and requiring a header for an ``<script>`` tag would not work anyway.
    """
    if not UI_DIST.exists():

        @app.get("/")
        async def ui_missing() -> JSONResponse:
            """Placeholder so a bare launch explains itself instead of 404-ing."""
            return JSONResponse(
                {
                    "error": "UI not built",
                    "detail": (
                        f"{UI_DIST} does not exist. Build it with "
                        "`cd app/ui && npm install && npm run build`, or use "
                        "`npm run dev` for hot reload during Phases 1-5."
                    ),
                    "api": "GET /status, GET /events (both require the STACKSEARCH_TOKEN)",
                },
                status_code=503,
            )

        return

    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", response_model=None)
    async def spa_fallback(full_path: str) -> FileResponse | JSONResponse:
        """Serve a UI file, falling back to index.html for client-side routes.

        The fallback is required for any future client-side routing: a deep link like
        ``/settings`` must return the app shell, not a 404.
        """
        if full_path.startswith(
            (
                "status",
                "events",
                "health",
                "search",
                "answer",
                "sources",
                "documents",
                "policy",
                "models",
            )
        ):
            return JSONResponse({"error": "not found"}, status_code=404)

        candidate = (UI_DIST / full_path).resolve()
        # Reject traversal out of the dist directory.
        if full_path and candidate.is_file() and UI_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(UI_DIST / "index.html")


app = create_app()
