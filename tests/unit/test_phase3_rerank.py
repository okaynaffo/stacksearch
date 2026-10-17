"""Phase 3: reranker ordering, fallback, and config gate (PLAN.md §7.3, TODO 3.3)."""

from __future__ import annotations

import pytest

from core.index import open_index, pack_embedding
from core.index.writer import ChunkWrite, index_document
from core.models.reranker import CrossEncoderReranker
from core.query.hybrid import hybrid_search
from core.query.rerank import NoOpReranker, get_reranker, reranker_enabled, should_rerank

pytestmark = pytest.mark.unit


class _FakeCrossEncoder:
    """Stand-in for the ONNX session: scores by keyword overlap."""

    def rerank(self, query, documents, batch_size=64, **kwargs):
        terms = set(query.lower().split())
        return [float(sum(1 for t in terms if t in doc.lower())) for doc in documents]


def _reranker_with_fake():
    r = CrossEncoderReranker.__new__(CrossEncoderReranker)
    r.model = None
    from core.config import RERANKER_MODELS

    r.model = RERANKER_MODELS["jina-reranker-v2-base-multilingual"]
    import threading

    r._lock = threading.Lock()
    r._impl = _FakeCrossEncoder()
    return r


def test_rerank_candidates_sorts_and_scores():
    r = _reranker_with_fake()
    cands = [
        {"chunk_id": 1, "title": "a", "snippet": "unrelated weather report", "signals": {}},
        {"chunk_id": 2, "title": "b", "snippet": "storage pricing tiers", "signals": {}},
    ]
    ranked = r.rerank_candidates("storage pricing article", cands, top_k=2)
    assert [c["chunk_id"] for c in ranked] == [2, 1]
    assert ranked[0]["signals"]["rerank_score"] > ranked[1]["signals"]["rerank_score"]


def test_rerank_prefers_full_text_over_snippet():
    r = _reranker_with_fake()
    cands = [
        {
            "chunk_id": 1,
            "title": "a",
            "snippet": "unrelated",
            "text": "full text about storage pricing tiers",
            "signals": {},
        },
        {"chunk_id": 2, "title": "b", "snippet": "storage", "signals": {}},
    ]
    ranked = r.rerank_candidates("storage pricing", cands, top_k=2)
    assert ranked[0]["chunk_id"] == 1


def test_hybrid_falls_back_to_fusion_on_model_failure(tmp_path, monkeypatch):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        vec = pack_embedding([0.1] * 768)
        index_document(
            conn,
            source_id=src,
            external_id="d",
            uri="file:///d",
            title="notes",
            text="storage pricing tiers for cold archives",
            chunks=[
                ChunkWrite(
                    text="storage pricing tiers for cold archives", loc_json="{}", token_count=6
                )
            ],
            embeddings=[vec],
        )

        import core.models.reranker as rm

        def _boom():
            raise rm.RerankerError("no model in test")

        monkeypatch.setattr(rm, "get_reranker_model", _boom)
        out = hybrid_search(
            conn, "what are the storage pricing tiers", embed_fn=lambda q: [0.1] * 768
        )
        assert out["results"], "fusion order must survive rerank failure"
        assert any("rerank unavailable" in w for w in out["warnings"])
        assert "text" not in out["results"][0], "full text must not leak into §10 shape"


def test_rerank_gate_and_config(monkeypatch):
    assert should_rerank("what are the storage pricing tiers") is True
    assert should_rerank("48213") is False
    assert isinstance(get_reranker(), object)
    monkeypatch.setenv("STACKSEARCH_RERANKER", "none")
    assert reranker_enabled() is False
    assert isinstance(get_reranker(), NoOpReranker)
