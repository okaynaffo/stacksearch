"""Reciprocal Rank Fusion over BM25 + vector results (PLAN.md §7.2, TODO 2.2).

`score(d) = Σ 1 / (k + rank_i(d))` with `k = 60` (§7.2). No score
normalization is needed, which is the point: BM25 scores and cosine distances
live on incomparable scales, and RRF only needs ranks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def rrf_fuse(
    bm25_results: list[dict[str, Any]],
    vec_results: list[dict[str, Any]],
    *,
    k: int = 60,
    limit: int = 20,
    max_chunks_per_doc: int = 2,
    query_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fuse two ranked lists into one, ordered by fused score descending.

    Each input must carry `chunk_id`, `document_id`, and `signals` with its
    1-based rank (`bm25_rank` / `vec_rank`). Output rows keep the §10 shape and
    record both ranks plus the fused score in `signals`.
    """
    terms = [t.lower() for t in (query_terms or []) if t.strip()]
    bm25_rank = {r["chunk_id"]: i + 1 for i, r in enumerate(bm25_results)}
    vec_rank = {r["chunk_id"]: i + 1 for i, r in enumerate(vec_results)}
    by_id: dict[Any, dict[str, Any]] = {}
    for r in bm25_results:
        by_id.setdefault(r["chunk_id"], r)
    for r in vec_results:
        by_id.setdefault(r["chunk_id"], r)

    fused: list[dict[str, Any]] = []
    for chunk_id, base in by_id.items():
        score = 0.0
        if chunk_id in bm25_rank:
            score += 1.0 / (k + bm25_rank[chunk_id])
        if chunk_id in vec_rank:
            score += 1.0 / (k + vec_rank[chunk_id])
        score += _boosts(base, terms)
        merged = dict(base)
        merged["score"] = score
        signals = dict(base.get("signals", {}))
        if chunk_id in bm25_rank:
            signals["bm25_rank"] = bm25_rank[chunk_id]
        if chunk_id in vec_rank:
            signals["vec_rank"] = vec_rank[chunk_id]
        signals["fused_score"] = score
        merged["signals"] = signals
        fused.append(merged)

    fused.sort(key=lambda r: r["score"], reverse=True)

    # §7.2: at most 2 chunks per document so one long PDF cannot drown the list.
    per_doc: dict[Any, int] = {}
    deduped: list[dict[str, Any]] = []
    for r in fused:
        seen = per_doc.get(r["document_id"], 0)
        if seen >= max_chunks_per_doc:
            continue
        per_doc[r["document_id"]] = seen + 1
        deduped.append(r)
    return deduped[:limit]


def _boosts(row: dict[str, Any], terms: list[str]) -> float:
    """Small additive boosts (§7.2): recency for email/chat, title matches."""
    boost = 0.0
    if terms and row.get("title"):
        title = str(row["title"]).lower()
        hits = sum(1 for t in terms if t and t in title)
        boost += 0.005 * hits
    if row.get("source_kind") in ("email", "chat") and row.get("date"):
        try:
            dt = datetime.fromisoformat(str(row["date"])[:10])
            if datetime.now() - dt < timedelta(days=30):
                boost += 0.01
        except ValueError:
            pass
    return boost
