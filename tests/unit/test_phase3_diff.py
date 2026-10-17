"""Phase 3: chunk-level diffing keeps rows and embeddings for unchanged chunks (PLAN.md §6.5.2)."""

from __future__ import annotations

import pytest

from core.index import open_index, pack_embedding
from core.index.writer import (
    ChunkWrite,
    fetch_existing_chunks,
    index_document,
    new_chunk_indices,
)

pytestmark = pytest.mark.unit

VEC = pack_embedding([0.1] * 768)


def _cw(t: str) -> ChunkWrite:
    return ChunkWrite(text=t, loc_json="{}", token_count=3)


def _source(conn) -> int:
    conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_changed_doc_reuses_unchanged_chunks(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _source(conn)
        r1 = index_document(
            conn,
            source_id=src,
            external_id="d",
            uri="file:///d",
            title="d",
            text="alpha BRAVO",
            chunks=[_cw("alpha"), _cw("BRAVO")],
            embeddings=[VEC, VEC],
        )
        assert (r1["reused"], r1["embedded"]) == (0, 2)
        ids_before = [cid for cid, _ in fetch_existing_chunks(conn, r1["document_id"])]

        new_chunks = [_cw("alpha"), _cw("CHARLIE")]
        assert new_chunk_indices(fetch_existing_chunks(conn, r1["document_id"]), new_chunks) == [1]

        r2 = index_document(
            conn,
            source_id=src,
            external_id="d",
            uri="file:///d",
            title="d",
            text="alpha CHARLIE",
            chunks=new_chunks,
            embeddings=[None, VEC],
        )
        assert r2["changed"] is True
        assert (r2["reused"], r2["embedded"]) == (1, 1)
        # Reused chunk keeps its row id, so the stored vector stays valid.
        assert ids_before[0] in dict(fetch_existing_chunks(conn, r2["document_id"]))
        assert conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 2


def test_new_chunk_with_none_blob_raises(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _source(conn)
        index_document(
            conn,
            source_id=src,
            external_id="d",
            uri="file:///d",
            title="d",
            text="alpha",
            chunks=[_cw("alpha")],
            embeddings=[VEC],
        )
        with pytest.raises(ValueError, match="None for a new chunk"):
            index_document(
                conn,
                source_id=src,
                external_id="d",
                uri="file:///d",
                title="d",
                text="alpha CHARLIE",
                chunks=[_cw("alpha"), _cw("CHARLIE")],
                embeddings=[None, None],
            )


def test_duplicate_paragraph_reuses_one_copy(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _source(conn)
        index_document(
            conn,
            source_id=src,
            external_id="d",
            uri="file:///d",
            title="d",
            text="x x",
            chunks=[_cw("x"), _cw("x")],
            embeddings=[VEC, VEC],
        )
        doc_id = 1
        assert new_chunk_indices(fetch_existing_chunks(conn, doc_id), [_cw("x")]) == []
        r = index_document(
            conn,
            source_id=src,
            external_id="d",
            uri="file:///d",
            title="d",
            text="x",
            chunks=[_cw("x")],
            embeddings=[None],
        )
        assert (r["reused"], r["embedded"]) == (1, 0)
        assert conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 1
