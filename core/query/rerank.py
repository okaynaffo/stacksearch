"""Reranker interface, enabled in Phase 3 (PLAN.md §4.1, §7.3, TODO 3.3).

:class:`NoOpReranker` is the fallback: short keyword lookups skip reranking
via :func:`should_rerank`, and any load/score failure degrades to fusion
order with a warning rather than failing the search.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

_ORDER_ID_RE = re.compile(r"order-id-\d+", re.IGNORECASE)

#: Values of ``settings.reranker`` that keep the no-op (eval ablation, Phase 4).
DISABLED = frozenset({"", "none", "off", "noop"})


class Reranker(Protocol):
    """Rescores fused candidates against the raw query."""

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, top_k: int
    ) -> list[dict[str, Any]]: ...


class NoOpReranker:
    """Pass-through reranker: returns the first *top_k* candidates unchanged."""

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, top_k: int
    ) -> list[dict[str, Any]]:
        return list(candidates[:top_k])


class CrossEncoderReranker:
    """jina cross-encoder behind the shared lazy singleton (§4.1, §7.3)."""

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, top_k: int
    ) -> list[dict[str, Any]]:
        from core.models.reranker import get_reranker_model

        return get_reranker_model().rerank_candidates(query, candidates, top_k=top_k)


def should_rerank(query: str) -> bool:
    """Skip reranking for short keyword lookups (§7.3).

    Exact ID lookups and 1–2 term queries are already decided by BM25; a
    cross-encoder buys nothing and costs a model pass.
    """
    text = (query or "").strip()
    if not text:
        return False
    if _ORDER_ID_RE.search(text):
        return False
    terms = [t for t in re.split(r"\s+", text.strip('"')) if t]
    return len(terms) > 2


def reranker_enabled() -> bool:
    """Whether config selects the real reranker (default ON, Phase 3)."""
    from core.config import get_settings

    return get_settings().reranker not in DISABLED


def get_reranker() -> Reranker:
    """Return the configured reranker, or the no-op when disabled."""
    if not reranker_enabled():
        return NoOpReranker()
    return CrossEncoderReranker()
