"""Hybrid retrieval orchestrator: BM25 + vector, fused (PLAN.md §7.2, TODO 2.2).

Two-speed results (§7.4) start here: this function returns ranked results
without touching the answer LLM, so the UI can render instantly and stream the
answer afterwards (Phase 4).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from core.config import get_settings
from core.query.bm25 import bm25_search
from core.query.filters import QueryFilters
from core.query.fusion import rrf_fuse
from core.query.parser import parse_query
from core.query.rerank import should_rerank
from core.query.vector import vector_search


def hybrid_search(
    conn: sqlite3.Connection,
    raw_query: str,
    *,
    limit: int = 20,
    filters: QueryFilters | None = None,
    embed_fn: Callable[[str], list[float] | None] | None = None,
) -> dict[str, Any]:
    """Run hybrid retrieval and return ``{"results", "parsed", "warnings"}``.

    *filters* (caller-supplied, e.g. UI chips) overrides the parsed filters
    field by field. *embed_fn* injects the query embedding in tests; when None
    the shared ONNX embedder is used, and a vector-path failure degrades to
    BM25-only with a warning rather than failing the search.
    """
    settings = get_settings()
    retrieval = settings.retrieval
    parsed = parse_query(raw_query)
    active = parsed.filters.merge(filters) if filters else parsed.filters
    text = parsed.text or raw_query.strip()
    if not text:
        return {"results": [], "parsed": parsed, "warnings": []}

    warnings: list[str] = []
    bm25_results = bm25_search(conn, text, limit=retrieval.bm25_top_k, filters=active)

    query_vector: list[float] | None = None
    if embed_fn is not None:
        query_vector = embed_fn(text)
    else:
        try:
            from core.models.embedder import get_embedder

            query_vector = get_embedder().embed_queries([text])[0]
        except Exception as exc:  # noqa: BLE001 — search must survive a model failure
            warnings.append(f"vector search unavailable ({type(exc).__name__}); BM25-only")

    vec_results: list[dict[str, Any]] = []
    if query_vector is not None:
        try:
            vec_results = vector_search(
                conn, query_vector, limit=retrieval.vector_top_k, filters=active
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"vector search failed ({type(exc).__name__}); BM25-only")

    if not vec_results and not warnings:
        warnings.append("no vectors in index; BM25-only (run scripts/backfill_vectors.py)")

    if not vec_results:
        # Degraded but honest: keep BM25 ranks, cap per-doc like fusion does.
        results = _cap_per_doc(bm25_results, limit=limit, max_per_doc=retrieval.max_chunks_per_doc)
        return {"results": results, "parsed": parsed, "warnings": warnings}

    fused = rrf_fuse(
        bm25_results,
        vec_results,
        k=retrieval.rrf_k,
        limit=max(limit, retrieval.rerank_top_k),
        max_chunks_per_doc=retrieval.max_chunks_per_doc,
        query_terms=text.split(),
    )
    if should_rerank(text):
        fused = _maybe_rerank(conn, text, fused, top_k=retrieval.rerank_top_k, warnings=warnings)
    return {"results": fused[:limit], "parsed": parsed, "warnings": warnings}


def _maybe_rerank(
    conn: sqlite3.Connection,
    query: str,
    candidates: list[dict[str, Any]],
    *,
    top_k: int,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Rerank the top candidates, degrading to fusion order on any failure.

    Full chunk texts are attached for scoring (one batched PK lookup) and
    stripped afterwards so the §10 response shape does not change.
    """
    from core.query.rerank import get_reranker

    reranker = get_reranker()
    if type(reranker).__name__ == "NoOpReranker":
        return reranker.rerank(query, candidates, top_k=top_k)
    ids = [c["chunk_id"] for c in candidates[:top_k]]
    texts: dict[int, str] = {}
    if ids:
        try:
            rows = conn.execute(
                f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            texts = {r["id"]: r["text"] for r in rows}
        except Exception:  # noqa: BLE001 — fall back to snippet scoring
            texts = {}
    for c in candidates[:top_k]:
        if c["chunk_id"] in texts:
            c["text"] = texts[c["chunk_id"]]
    try:
        ranked = reranker.rerank(query, candidates[:top_k], top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — search must survive a model failure
        warnings.append(f"rerank unavailable ({type(exc).__name__}); fusion order")
        return candidates
    finally:
        for c in candidates:
            c.pop("text", None)
    # `finally` runs before `return ranked`, so strip-then-return is safe.
    for c in ranked:
        c.pop("text", None)
    # Candidates past top_k keep fusion order below the reranked head.
    return ranked + [c for c in candidates[top_k:] if c not in ranked]


def _cap_per_doc(
    results: list[dict[str, Any]], *, limit: int, max_per_doc: int
) -> list[dict[str, Any]]:
    per_doc: dict[Any, int] = {}
    out: list[dict[str, Any]] = []
    for r in results:
        seen = per_doc.get(r["document_id"], 0)
        if seen >= max_per_doc:
            continue
        per_doc[r["document_id"]] = seen + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out
