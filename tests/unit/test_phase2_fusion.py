"""Phase 2 fusion + reranker tests (PLAN.md §7.2/7.3, TODO 2.2)."""

from __future__ import annotations

import pytest

from core.query.fusion import rrf_fuse
from core.query.rerank import NoOpReranker, get_reranker, should_rerank

pytestmark = pytest.mark.unit


def _row(chunk_id: int, doc_id: int, **extra):
    base = {
        "chunk_id": chunk_id,
        "document_id": doc_id,
        "source_kind": "files",
        "title": f"doc {doc_id}",
        "author": None,
        "date": None,
        "snippet": f"snippet {chunk_id}",
        "uri": f"file:///{doc_id}",
        "loc": {},
        "score": 0.0,
        "signals": {},
    }
    base.update(extra)
    return base


def test_rrf_ranks_shared_hits_first():
    # Chunk 1 is in both lists; chunk 3 is vec rank 1; chunk 2 is BM25 rank 2.
    # Shared: 1/61 + 1/62 ≈ 0.0325 > vec-only 1/61 ≈ 0.0164 > bm25-only 1/62.
    bm25 = [_row(1, 1), _row(2, 2)]
    vec = [_row(3, 3), _row(1, 1)]
    fused = rrf_fuse(bm25, vec, k=60, limit=10)
    assert [r["chunk_id"] for r in fused] == [1, 3, 2]
    top = fused[0]
    assert top["signals"]["bm25_rank"] == 1
    assert top["signals"]["vec_rank"] == 2
    assert top["signals"]["fused_score"] == pytest.approx(1 / 61 + 1 / 62)


def test_rrf_k60_default_matches_plan():
    import inspect

    from core.config import Retrieval

    assert Retrieval().rrf_k == 60
    assert inspect.signature(rrf_fuse).parameters["k"].default == 60


def test_rrf_caps_chunks_per_document():
    bm25 = [_row(1, 1), _row(2, 1), _row(3, 1), _row(4, 2)]
    fused = rrf_fuse(bm25, [], k=60, limit=10, max_chunks_per_doc=2)
    assert [r["chunk_id"] for r in fused] == [1, 2, 4]


def test_rrf_title_boost_prefers_matching_title():
    bm25 = [
        _row(1, 1, title="unrelated log"),
        _row(2, 2, title="storage pricing review"),
    ]
    fused = rrf_fuse(bm25, [], k=60, limit=10, query_terms=["storage"])
    assert fused[0]["chunk_id"] == 2


def test_rrf_keeps_bm25_snippet():
    bm25 = [_row(1, 1, snippet="a <mark>hit</mark>")]
    vec = [_row(1, 1, snippet="a hit")]
    assert rrf_fuse(bm25, vec, k=60, limit=10)[0]["snippet"] == "a <mark>hit</mark>"


def test_should_rerank_skips_keyword_lookups():
    assert should_rerank("that article about storage pricing") is True
    assert should_rerank("stipend") is False
    assert should_rerank("ORDER-ID-48213") is False
    assert should_rerank("   ") is False


def test_noop_reranker_is_a_truncating_passthrough(monkeypatch):
    from core.query.rerank import CrossEncoderReranker

    rows = [_row(1, 1), _row(2, 2)]
    assert NoOpReranker().rerank("q", rows, top_k=1) == rows[:1]
    # Phase 3 enables the cross-encoder by default (TODO 3.3); the no-op is
    # what a disabled config resolves to.
    assert isinstance(get_reranker(), CrossEncoderReranker)
    monkeypatch.setenv("STACKSEARCH_RERANKER", "none")
    assert isinstance(get_reranker(), NoOpReranker)
