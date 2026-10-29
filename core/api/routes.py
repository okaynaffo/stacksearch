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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.api.deps import require_token
from core.api.events import bus, event_stream
from core.config import (
    EMBEDDING_MODELS,
    LLM_MODELS,
    get_settings,
    model_path,
)
from core.debug_log import (
    log_event,
    log_exception,
    log_paths,
    sanitize,
    write_ui_records,
)
from core.index import forget_source, stats

#: Valid source kinds (PLAN.md §6.2).
SOURCE_KINDS = ("files", "pdf", "email", "email_imap", "browser", "chat", "code")

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
            log_exception("api.status", "index-stats-failed", exc)

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
            # The §11.3 model picker shows the §2.1 memory cost of each choice before the
            # user switches, which the single `answer_llm` entry above cannot.
            "answer_llm_options": [
                {
                    "key": key,
                    "label": model.label,
                    "size_gb": model.size_gb,
                    "present": model_path(key).exists(),
                }
                for key, model in LLM_MODELS.items()
            ],
            "reranker": {
                "key": settings.reranker,
                "enabled": settings.reranker not in {"", "none", "off", "noop"},
                "loaded": _reranker_loaded(),
            },
        },
        "llm_server": llm.state.as_dict() if llm else {"running": False},
        "queue": index_stats.get("jobs_pending", 0) if index_stats else 0,
        "queue_paused_reason": app_policy.paused_reason() if app_policy else None,
        # The switch in Settings reflects the policy signal itself, not the derived pause
        # reason (which is null whenever the queue happens to be drained).
        "battery_low": app_policy.battery_paused if app_policy else False,
        "idle_timeout_minutes": round(llm.idle_timeout_s / 60) if llm else None,
        "worker_state": worker.state if worker else "disabled",
        "events_subscribers": bus.subscriber_count,
        "logs": log_paths(),
    }


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """SSE stream of indexing progress for the UI (PLAN.md §10)."""
    log_event("api.events", "subscribe", subscribers=bus.subscriber_count + 1)
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
    log_event("search", "begin", query=query, limit=limit, filters=body.get("filters"))
    if not query:
        log_event("search", "end", query=query, results=0, latency_ms=0, note="empty query")
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
    log_event(
        "search",
        "end",
        query=query,
        results=len(outcome["results"]),
        latency_ms=latency_ms,
        warnings=outcome["warnings"],
        top_titles=[r.get("title") for r in outcome["results"][:5]],
    )
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
    log_event("answer", "begin", query=query, limit=limit, filters=body.get("filters"))

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
                    event = frame.get("event")
                    data = frame.get("data") or {}
                    if event == "sources":
                        log_event(
                            "answer",
                            "sources",
                            query=query,
                            count=len(data.get("sources") or []),
                            warnings=data.get("warnings") or [],
                        )
                    elif event == "done":
                        log_event(
                            "answer",
                            "end",
                            query=query,
                            grounded=data.get("grounded"),
                            citations=data.get("citations"),
                            flags=data.get("flags"),
                            warnings=data.get("warnings"),
                            support_rate=data.get("support_rate"),
                            latency_ms=data.get("latency_ms"),
                            text_chars=len(str(data.get("text") or "")),
                            text=data.get("text"),
                        )
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            except GeneratorExit:
                # The UI aborted the stream (new query, Escape, window closed). This is
                # normal, but the record is exactly what distinguishes a user abort from
                # a mid-answer crash when reconstructing a quit.
                log_event("answer", "cancelled", level="WARNING", query=query)
                raise
            except Exception as exc:  # noqa: BLE001 — log, then let the client see EOF
                log_exception("answer", "failed", exc, query=query)
                raise
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

        log_event("source", "reindex-not-found", level="WARNING", source_id=source_id)
        raise HTTPException(status_code=404, detail=f"unknown source {source_id}")
    job_id = job_queue.enqueue(
        request.app.state.index.conn,
        "scan",
        {"source_id": source_id, "child_priority": settings.queue.priority_live_change},
        priority=settings.queue.priority_reindex,
    )
    log_event("source", "reindex", source_id=source_id, job_id=job_id)
    return {"job_id": job_id, "source_id": source_id}


class SourceCreate(BaseModel):
    """Body for ``POST /sources`` (PLAN.md §10)."""

    kind: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SourcePatch(BaseModel):
    """Body for ``PATCH /sources/{id}`` — only the provided fields change."""

    name: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


def _source_summary(conn, row) -> dict[str, Any]:
    """One source row plus the counts the settings UI shows (§11.3)."""
    sid = row["id"]
    docs = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE source_id = ? AND status != 'deleted'", (sid,)
    ).fetchone()[0]
    errors = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE source_id = ? AND status = 'error'", (sid,)
    ).fetchone()[0]
    return {
        "id": sid,
        "kind": row["kind"],
        "name": row["name"],
        "config": json.loads(row["config_json"]),
        "enabled": bool(row["enabled"]),
        "last_sync_at": row["last_sync_at"],
        "documents": docs,
        "errors": errors,
    }


@router.get("/sources")
async def list_sources(request: Request) -> dict[str, Any]:
    """Every configured source with per-source counts (PLAN.md §10, §11.3)."""
    rows = request.app.state.index.conn.execute(
        "SELECT * FROM sources ORDER BY id"
    ).fetchall()
    sources = [_source_summary(request.app.state.index.conn, r) for r in rows]
    log_event("source", "list", level="DEBUG", count=len(sources))
    return {"sources": sources}


@router.post("/sources", status_code=201)
async def create_source(request: Request, body: SourceCreate) -> dict[str, Any]:
    """Add a source and kick off its first scan (PLAN.md §10, §11.1)."""
    from core.jobs import queue as job_queue

    if body.kind not in SOURCE_KINDS:
        log_event(
            "source",
            "create-rejected",
            level="WARNING",
            kind=body.kind,
            name=body.name,
            reason="unknown kind",
        )
        raise HTTPException(
            status_code=422,
            detail=f"unknown source kind {body.kind!r}; expected one of {SOURCE_KINDS}",
        )
    log_event(
        "source",
        "create-begin",
        kind=body.kind,
        name=body.name,
        config=sanitize(body.config),
        enabled=body.enabled,
    )
    conn = request.app.state.index.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO sources(kind, name, config_json, enabled) VALUES (?, ?, ?, ?)",
            (body.kind, body.name, json.dumps(body.config), int(body.enabled)),
        )
        source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    settings = get_settings()
    job_id = None
    if body.enabled:
        job_id = job_queue.enqueue(
            conn,
            "scan",
            {"source_id": source_id, "child_priority": settings.queue.priority_bulk},
            priority=settings.queue.priority_bulk,
        )
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    log_event("source", "created", source_id=source_id, kind=body.kind, job_id=job_id)
    return {"source": _source_summary(conn, row), "job_id": job_id}


@router.patch("/sources/{source_id}")
async def patch_source(request: Request, source_id: int, body: SourcePatch) -> dict[str, Any]:
    """Enable/disable or edit a source; re-scan when config changes (PLAN.md §10)."""
    from core.jobs import queue as job_queue

    conn = request.app.state.index.conn
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown source {source_id}")

    fields: list[str] = []
    params: list[Any] = []
    if body.name is not None:
        fields.append("name = ?")
        params.append(body.name)
    if body.config is not None:
        fields.append("config_json = ?")
        params.append(json.dumps(body.config))
    if body.enabled is not None:
        fields.append("enabled = ?")
        params.append(int(body.enabled))
    if fields:
        params.append(source_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", params)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # A config or enable change invalidates what we know: rescan from scratch.
    if body.config is not None or body.enabled:
        settings = get_settings()
        job_queue.enqueue(
            conn,
            "scan",
            {"source_id": source_id, "child_priority": settings.queue.priority_bulk},
            priority=settings.queue.priority_live_change,
        )
    updated = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    log_event(
        "source",
        "patched",
        source_id=source_id,
        name=body.name,
        config=sanitize(body.config),
        enabled=body.enabled,
        rescan=body.config is not None or bool(body.enabled),
    )
    return {"source": _source_summary(conn, updated)}


@router.delete("/sources/{source_id}")
async def delete_source(request: Request, source_id: int) -> dict[str, Any]:
    """Forget a source and every document, chunk, and vector it owns (§9, §10)."""
    conn = request.app.state.index.conn
    row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        log_event("source", "delete-not-found", level="WARNING", source_id=source_id)
        raise HTTPException(status_code=404, detail=f"unknown source {source_id}")
    removed = forget_source(conn, source_id)
    log_event("source", "deleted", source_id=source_id, removed=removed)
    return {"forgotten": source_id, **removed}


class BatteryPolicyBody(BaseModel):
    """Body for ``POST /policy/battery``."""

    low: bool


@router.post("/policy/battery")
async def set_battery_policy(request: Request, body: BatteryPolicyBody) -> dict[str, Any]:
    """Toggle the battery-pause signal (PLAN.md §6.6, §11.3)."""
    app_policy = getattr(request.app.state, "policy", None)
    if app_policy is None:
        log_event("policy", "battery-unavailable", level="WARNING")
        raise HTTPException(status_code=503, detail="policy unavailable")
    app_policy.set_battery_low(body.low)
    log_event("policy", "battery", low=body.low, paused_reason=app_policy.paused_reason())
    return {"battery_low": body.low, "paused_reason": app_policy.paused_reason()}


class IdleTimeoutBody(BaseModel):
    """Body for ``POST /policy/idle-timeout``."""

    minutes: int


@router.post("/policy/idle-timeout")
async def set_idle_timeout(request: Request, body: IdleTimeoutBody) -> dict[str, Any]:
    """Update the LLM idle-unload window (PLAN.md §6.7, §11.3)."""
    from core.llm.server import get_supervisor

    if body.minutes < 1:
        log_event("policy", "idle-timeout-rejected", level="WARNING", minutes=body.minutes)
        raise HTTPException(status_code=422, detail="minutes must be >= 1")
    supervisor = getattr(request.app.state, "llm_supervisor", None) or get_supervisor()
    request.app.state.llm_supervisor = supervisor
    supervisor.set_idle_timeout(body.minutes)
    log_event("policy", "idle-timeout", minutes=body.minutes)
    return {"minutes": body.minutes}


@router.get("/documents/{document_id}")
async def get_document(request: Request, document_id: int) -> dict[str, Any]:
    """Document metadata plus its open URI for the results list (PLAN.md §10, §11.2)."""
    conn = request.app.state.index.conn
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        log_event("document", "not-found", level="WARNING", document_id=document_id)
        raise HTTPException(status_code=404, detail=f"unknown document {document_id}")
    chunks = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    log_event(
        "document",
        "get",
        level="DEBUG",
        document_id=document_id,
        title=row["title"],
        status=row["status"],
        chunks=chunks,
    )
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "uri": row["uri"],
        "title": row["title"],
        "author": row["author"],
        "mime": row["mime"],
        "modified_at": row["modified_at"],
        "status": row["status"],
        "error": row["error"],
        "chunks": chunks,
    }


class AnswerModelChoice(BaseModel):
    """Body for ``POST /models/answer`` (PLAN.md §11.3 model picker)."""

    key: str


@router.post("/models/answer")
async def set_answer_model(request: Request, body: AnswerModelChoice) -> dict[str, Any]:
    """Switch the answer LLM between the pinned 4B and 8B (PLAN.md §4.1, §11.3).

    Switching unloads the resident model so the next question lazy-loads the chosen one
    (§8.4); the switch is a no-op, and the warm model is kept, when the key is unchanged.
    """
    from core.llm.server import get_supervisor

    if body.key not in LLM_MODELS:
        log_event("model", "switch-rejected", level="WARNING", key=body.key)
        raise HTTPException(
            status_code=422,
            detail=f"unknown model key {body.key!r}; expected one of {sorted(LLM_MODELS)}",
        )
    supervisor = getattr(request.app.state, "llm_supervisor", None) or get_supervisor()
    request.app.state.llm_supervisor = supervisor
    changed = supervisor.model_key != body.key
    if changed:
        log_event("model", "switch-begin", previous=supervisor.model_key, next=body.key)
        supervisor.stop("model switch")
        supervisor.model_key = body.key
        supervisor.state.model_key = body.key
    log_event("model", "switch-end", key=body.key, changed=changed)
    return {
        "key": body.key,
        "changed": changed,
        "running": supervisor.state.running,
    }


class LogBatch(BaseModel):
    """Body for ``POST /debug/log`` — one batch of frontend records."""

    records: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/debug/log")
async def ingest_ui_logs(body: LogBatch) -> dict[str, Any]:
    """Persist frontend click/key/lifecycle records in the core log file.

    The UI cannot write files directly from a webview, so it batches records here. The
    route is token-protected like every other route; the batch is bounded in
    :func:`core.debug_log.write_ui_records`.
    """
    written = write_ui_records(body.records)
    return {"received": len(body.records), "written": written}


@router.get("/debug/logs")
async def debug_logs() -> dict[str, Any]:
    """Where this launch is writing logs, plus current sizes (Settings shows these)."""
    paths = log_paths()
    sizes: dict[str, int | None] = {}
    from pathlib import Path

    for key, value in paths.items():
        if key == "dir":
            continue
        try:
            sizes[key] = Path(value).stat().st_size
        except OSError:
            sizes[key] = None
    return {"paths": paths, "sizes": sizes}


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for the Tauri supervisor (PLAN.md §13.1).

    Token-protected like every other route, and that is deliberate: §13.1 has Rust polling
    this to decide whether the child process came up, and Rust already holds the token it
    generated. There is no reason to expose even a "the app is running" signal to any
    process on the machine that does not.
    """
    log_event("api.health", "ok", level="DEBUG")
    return {"status": "ok"}
