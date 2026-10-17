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

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.api.events import bus
from core.api.routes import router
from core.config import REPO_ROOT, get_settings
from core.index import open_index

UI_DIST = REPO_ROOT / "app" / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Phase 3 index worker + watcher alongside the service (§6.6, §6.2)."""
    worker = getattr(app.state, "worker", None)
    watcher = getattr(app.state, "watcher", None)
    if worker is not None:
        worker.start()
    if watcher is not None:
        try:
            watcher.start()
        except Exception:  # noqa: BLE001 — search works without live watching
            pass
    try:
        yield
    finally:
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        if worker is not None:
            worker.stop()


def create_app(*, open_database: bool = True) -> FastAPI:
    """Build the FastAPI application.

    ``open_database=False`` skips opening the index, which the API tests use so they do
    not touch the real data directory.
    """
    settings = get_settings()
    settings.ensure_dirs()

    app = FastAPI(
        title="StackSearch",
        version="0.1.0",
        description="Private, local-first personal search engine (PLAN.md).",
        lifespan=lifespan,
    )
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
