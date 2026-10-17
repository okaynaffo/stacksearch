"""Routes for the local API (PLAN.md §10).

Phase 0 implements the two routes the acceptance criterion names: ``GET /status``
(§10) and ``GET /events`` (§10, needed early by §13.1's WKWebView SSE test). The
remaining routes in §10 land with the phases that own them, so nothing here is a stub
that would mislead the UI about what exists.
"""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from core.api.deps import require_token
from core.api.events import bus, event_stream
from core.config import (
    EMBEDDING_MODELS,
    LLM_MODELS,
    get_settings,
    model_path,
)
from core.index import stats

#: Token enforcement lives on the router, not on individual routes (PLAN.md §9). Every
#: route in this module therefore requires the per-launch token, and a route added later
#: cannot accidentally be public.
router = APIRouter(dependencies=[Depends(require_token)])


def _reranker_loaded() -> bool:
    """Whether the reranker is resident — peeked, never loaded, for /status."""
    import core.models.reranker as _rm

    with _rm._reranker_lock:
        return _rm._reranker is not None and _rm._reranker.is_loaded


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Index stats, queue length, and model status (PLAN.md §10).

    The shape is designed so Phase 0's Tauri window can render it as raw JSON and Phase 6
    can show real progress from the same payload.
    """
    settings = get_settings()
    index = request.app.state.index

    index_stats: dict[str, Any] = {}
    index_error: str | None = None
    if index is not None:
        try:
            index_stats = stats(index.conn)
        except Exception as exc:  # noqa: BLE001
            # A status endpoint that throws gives the UI nothing to report, which is
            # worse than reporting that the index is unavailable.
            index_error = f"{type(exc).__name__}: {exc}"

    llm = getattr(request.app.state, "llm_supervisor", None)
    embedder = getattr(request.app.state, "embedder", None)
    worker = getattr(request.app.state, "worker", None)
    app_policy = getattr(request.app.state, "policy", None)
    llm_key = llm.model_key if llm else settings.answer_llm
    gguf = model_path(llm_key)

    return {
        "app": settings.app_name,
        "version": "0.1.0",
        "phase": 0,
        "uptime_s": round(time.time() - request.app.state.started_at, 1),
        "index": index_stats,
        "index_error": index_error,
        "index_path": str(settings.index_db_path),
        "models": {
            "embedding": {
                "key": settings.embedding_model,
                "id": EMBEDDING_MODELS[settings.embedding_model].model_id,
                "dim": EMBEDDING_MODELS[settings.embedding_model].dim,
                "loaded": bool(embedder and embedder.is_loaded),
                "size_gb": EMBEDDING_MODELS[settings.embedding_model].size_gb,
            },
            "answer_llm": {
                "key": llm_key,
                "label": LLM_MODELS[llm_key].label,
                "size_gb": LLM_MODELS[llm_key].size_gb,
                "present": gguf.exists(),
                "path": str(gguf),
                "reasoning_enabled": settings.llm_enable_thinking,
            },
            "reranker": {
                "key": settings.reranker,
                "enabled": settings.reranker not in {"", "none", "off", "noop"},
                "loaded": _reranker_loaded(),
            },
        },
        "llm_server": llm.state.as_dict() if llm else {"running": False},
        "queue": index_stats.get("jobs_pending", 0) if index_stats else 0,
        "queue_paused_reason": app_policy.paused_reason() if app_policy else None,
        "worker_state": worker.state if worker else "disabled",
        "events_subscribers": bus.subscriber_count,
    }


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """SSE stream of indexing progress for the UI (PLAN.md §10)."""
    return StreamingResponse(
        event_stream(bus),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this, nginx or any future proxy would buffer the stream and the UI
            # would see nothing until the response ended.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/search")
async def search(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Ranked hybrid results with snippets (PLAN.md §10, TODO 2.2).

    Phase 2 fuses BM25 keyword search with vector kNN (RRF, k=60). Response
    shape matches §10 so the UI contract from Phase 1 does not change; rows
    additionally carry `vec_rank` / `fused_score` in `signals` when the vector
    path contributed. `filters` in the body overrides parsed filters field by
    field (UI chips); `warnings` reports a degraded BM25-only fallback.
    """
    import time as _time

    from core.query.filters import filters_from_dict
    from core.query.hybrid import hybrid_search

    started = _time.perf_counter()
    query = str(body.get("query", "")).strip()
    limit = int(body.get("limit", 20) or 20)
    limit = max(1, min(limit, 100))
    if not query:
        return {"results": [], "latency_ms": 0}
    app_policy = getattr(request.app.state, "policy", None)
    if app_policy is not None:
        # §6.7 state 2 (heuristic): the indexer yields within one batch while a
        # query runs, and the page cache is trimmed on the way out.
        with app_policy.query_pause():
            outcome = hybrid_search(
                request.app.state.index.conn,
                query,
                limit=limit,
                filters=filters_from_dict(body.get("filters")),
            )
            try:
                from core.index.db import trim_page_cache

                trim_page_cache(request.app.state.index.conn)
            except Exception:  # noqa: BLE001, S110 — best effort
                pass
    else:
        outcome = hybrid_search(
            request.app.state.index.conn,
            query,
            limit=limit,
            filters=filters_from_dict(body.get("filters")),
        )
    latency_ms = int((_time.perf_counter() - started) * 1000)
    response: dict[str, Any] = {"results": outcome["results"], "latency_ms": latency_ms}
    if outcome["warnings"]:
        response["warnings"] = outcome["warnings"]
    return response


@router.post("/answer")
async def answer(request: Request, body: dict[str, Any]) -> StreamingResponse:
    """Stream a grounded answer with verified citations (PLAN.md §10, TODO 4.2).

    Two-speed (§7.4): the UI already has ``POST /search`` results; this route
    re-retrieves for citation stability, emits a ``sources`` frame (the
    numbered ``[n]`` map) first, streams ``delta`` text, and closes with a
    ``done`` frame carrying the §8.3-verified text, citations, and flags.

    ``app.state.llm_client`` overrides the shared client when set — the
    mocked-LLM tests use this so they never need a running ``llama-server``.
    """
    from core.llm.answer import iter_answer_stream
    from core.query.filters import filters_from_dict

    query = str(body.get("query", "")).strip()
    raw_limit = body.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 8
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 8))
    filters = filters_from_dict(body.get("filters"))
    policy = getattr(request.app.state, "policy", None)
    client = getattr(request.app.state, "llm_client", None)

    pause = policy.query_pause() if policy is not None else nullcontext()

    def generate():  # sync generator: Starlette runs it in a threadpool
        with pause:
            try:
                # Empty query flows through the same path: no candidates means
                # the sources frame is empty and the done frame is a refusal.
                for frame in iter_answer_stream(
                    query,
                    request.app.state.index.conn,
                    client=client,
                    limit=limit,
                    filters=filters,
                ):
                    yield f"event: {frame['event']}\ndata: {json.dumps(frame['data'])}\n\n"
            finally:
                try:
                    from core.index.db import trim_page_cache

                    trim_page_cache(request.app.state.index.conn)
                except Exception:  # noqa: BLE001, S110 — best effort
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sources/{source_id}/reindex")
async def reindex_source(request: Request, source_id: int) -> dict[str, Any]:
    """Enqueue a user-triggered rescan of one source (PLAN.md §10, priority 1)."""
    from core.jobs import queue as job_queue

    settings = get_settings()
    row = request.app.state.index.conn.execute(
        "SELECT id FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"unknown source {source_id}")
    job_id = job_queue.enqueue(
        request.app.state.index.conn,
        "scan",
        {"source_id": source_id, "child_priority": settings.queue.priority_live_change},
        priority=settings.queue.priority_reindex,
    )
    return {"job_id": job_id, "source_id": source_id}


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for the Tauri supervisor (PLAN.md §13.1).

    Token-protected like every other route, and that is deliberate: §13.1 has Rust polling
    this to decide whether the child process came up, and Rust already holds the token it
    generated. There is no reason to expose even a "the app is running" signal to any
    process on the machine that does not.
    """
    return {"status": "ok"}
