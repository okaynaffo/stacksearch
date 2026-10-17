"""Phase 1 BM25 tests (PLAN.md §7.2, TODO 1.3)."""

from __future__ import annotations

import pytest

from core.index import open_index
from core.index.writer import ChunkWrite, index_document
from core.query.bm25 import bm25_search, build_match_query, escape_fts5_term

pytestmark = pytest.mark.unit


def _seed(conn) -> int:
    conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files', 't', '{}')")
    src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    index_document(
        conn, source_id=src, external_id="a", uri="file:///a.md",
        title="alpha doc", text="the quick brown fox jumps",
        chunks=[ChunkWrite(text="the quick brown fox jumps", loc_json="{}",
                           token_count=6)],
    )
    index_document(
        conn, source_id=src, external_id="b", uri="file:///b.md",
        title="beta doc", text="order ID 48213 confirmed yesterday",
        chunks=[ChunkWrite(text="order ID 48213 confirmed yesterday", loc_json="{}",
                           token_count=5)],
    )
    return src


def test_escape_quotes_special_terms():
    assert escape_fts5_term("C++") == '"C++"'
    assert escape_fts5_term("hello") == "hello"


def test_build_match_keeps_phrases():
    assert '"order ID"' in build_match_query('"order ID" extra')


def test_bm25_finds_exact_term(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        results = bm25_search(ix.conn, "48213")
    assert len(results) == 1
    assert results[0]["title"] == "beta doc"
    assert results[0]["signals"]["bm25_rank"] == 1


def test_bm25_phrase_query(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        results = bm25_search(ix.conn, '"order ID 48213"')
    assert results and results[0]["title"] == "beta doc"


def test_bm25_snippet_has_highlight(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        results = bm25_search(ix.conn, "fox")
    assert results and "<mark>" in results[0]["snippet"]


def test_bm25_empty_query_returns_nothing(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        _seed(ix.conn)
        assert bm25_search(ix.conn, "   ") == []


def test_fts_triggers_sync_on_insert_and_delete(tmp_path):
    """Migration 2: chunk writes must reach chunks_fts without manual sync."""
    with open_index(tmp_path / "index.db") as ix:
        src = _seed(ix.conn)
        assert bm25_search(ix.conn, "fox")
        from core.index.writer import mark_deleted

        assert mark_deleted(ix.conn, src, "a") is True
        assert bm25_search(ix.conn, "fox") == []
