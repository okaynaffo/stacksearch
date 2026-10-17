"""Phase 2 vector search + writer vector-lifecycle tests (PLAN.md §7.2, TODO 2.1).

Uses hand-packed orthogonal vectors rather than the ONNX model, so these stay
in the fast model-free suite. End-to-end embedding quality is measured by
`eval/scripts/run_retrieval_eval.py`, not asserted here.
"""

from __future__ import annotations

import pytest

from core.index import open_index, pack_embedding
from core.index.writer import ChunkWrite, index_document, mark_deleted
from core.query.filters import QueryFilters
from core.query.hybrid import hybrid_search
from core.query.vector import vector_search

pytestmark = pytest.mark.unit

DIM = 768


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _seed(conn) -> None:
    conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files', 't', '{}')")
    src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    blobs = [pack_embedding(_unit(0)), pack_embedding(_unit(1))]
    index_document(
        conn, source_id=src, external_id="a", uri="file:///a.md",
        title="alpha doc", text="alpha content here",
        chunks=[ChunkWrite(text="alpha content here", loc_json="{}", token_count=3)],
        embeddings=blobs[:1],
    )
    index_document(
        conn, source_id=src, external_id="b", uri="file:///b.md",
        title="beta doc", text="beta content here",
        chunks=[ChunkWrite(text="beta content here", loc_json="{}", token_count=3)],
        embeddings=blobs[1:],
    )


def test_vector_search_returns_nearest_first(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        results = vector_search(ix.conn, _unit(1), limit=10)
    assert [r["title"] for r in results] == ["beta doc", "alpha doc"]
    assert results[0]["signals"]["vec_rank"] == 1


def test_vector_search_applies_shared_filters(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        results = vector_search(
            ix.conn, _unit(1), limit=10, filters=QueryFilters(author="nobody")
        )
    assert results == []


def test_writer_vectors_written_and_counted(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        from core.index import stats

        assert stats(ix.conn)["vectors"] == 2


def test_writer_replace_clears_stale_vectors(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        src = ix.conn.execute("SELECT id FROM sources").fetchone()[0]
        index_document(
            ix.conn, source_id=src, external_id="a", uri="file:///a.md",
            title="alpha doc", text="alpha content revised",
            chunks=[ChunkWrite(text="alpha content revised", loc_json="{}", token_count=3)],
            embeddings=[pack_embedding(_unit(2))],
        )
        assert ix.conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 2
        results = vector_search(ix.conn, _unit(0), limit=10)
        assert [r["title"] for r in results] == ["beta doc", "alpha doc"]


def test_writer_delete_removes_vectors(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        src = ix.conn.execute("SELECT id FROM sources").fetchone()[0]
        assert mark_deleted(ix.conn, src, "a") is True
        assert ix.conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 1
        assert vector_search(ix.conn, _unit(0), limit=10)[0]["title"] == "beta doc"


def test_writer_rejects_misaligned_embeddings(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        ix.conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('t','t','{}')")
        src = ix.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        with pytest.raises(ValueError):
            index_document(
                ix.conn, source_id=src, external_id="x", uri="file:///x",
                title="x", text="x",
                chunks=[ChunkWrite(text="x", loc_json="{}", token_count=1)],
                embeddings=[],
            )


def test_hybrid_fuses_both_paths_with_injected_embedding(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        outcome = hybrid_search(
            ix.conn, "alpha content here", limit=10, embed_fn=lambda _t: _unit(1)
        )
    assert outcome["warnings"] == []
    top = outcome["results"][0]
    # BM25 rank 1 (alpha) + vec rank 2 vs vec rank 1 (beta) + absent from BM25:
    # 1/61+1/62 > 1/61 → the shared hit wins, which is the RRF contract.
    assert top["title"] == "alpha doc"
    assert top["signals"]["bm25_rank"] == 1
    assert top["signals"]["vec_rank"] == 2


def test_hybrid_degrades_to_bm25_without_vectors(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        ix.conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('t','t','{}')")
        src = ix.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        index_document(
            ix.conn, source_id=src, external_id="x", uri="file:///x",
            title="x doc", text="lighthouse beacon shines",
            chunks=[ChunkWrite(text="lighthouse beacon shines", loc_json="{}",
                               token_count=3)],
        )
        outcome = hybrid_search(ix.conn, "lighthouse", limit=10,
                                embed_fn=lambda _t: None)
    assert len(outcome["results"]) == 1
    assert outcome["warnings"], "degraded path must say so"
