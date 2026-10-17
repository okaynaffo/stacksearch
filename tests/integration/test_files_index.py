"""Phase 1 integration: scan → extract → chunk → write → search (§6, TODO 1.1–1.3)."""

from __future__ import annotations

import pytest

from core.chunking.prose import chunk_prose
from core.connectors.files import FilesConnector
from core.extract.text import extract_by_suffix
from core.index import open_index
from core.index.writer import ChunkWrite, index_document, mark_deleted, record_error
from core.query.bm25 import bm25_search

pytestmark = pytest.mark.integration


def _index_vault(conn, source_id: int, root) -> int:
    connector = FilesConnector()
    n = 0
    for ref in connector.scan({"paths": [str(root)]}):
        from core.connectors.pdf import resolve_fs_path

        fs_path = resolve_fs_path(ref.external_id)
        text = extract_by_suffix(fs_path)
        chunks = [
            ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
            for c in chunk_prose(text, header=f"[note] {ref.title}")
        ]
        index_document(conn, source_id=source_id, external_id=ref.external_id,
                       uri=ref.uri, title=ref.title, text=text, chunks=chunks)
        n += 1
    return n


def test_index_and_search_fixture_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Alpha\n\norder ID 48213 confirmed for pickup")
    (vault / "b.txt").write_text("storage pricing article about cold tiers")
    (vault / ".git").mkdir()
    (vault / ".git" / "ignored.md").write_text("order ID 48213 should not be indexed")
    (vault / "node_modules").mkdir()
    (vault / "node_modules" / "x.md").write_text("ignored content here")

    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        assert _index_vault(conn, src, vault) == 2

        results = bm25_search(conn, "48213")
        assert len(results) == 1
        assert "a.md" in results[0]["title"]

        # Excluded dirs never reach the DB.
        titles = [r["title"] for r in bm25_search(conn, "ignored")]
        assert titles == []


def test_reindex_same_content_skips_chunk_work(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("stable content here")

    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _index_vault(conn, src, vault)
        before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        _index_vault(conn, src, vault)
        after = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert before == after == 1


def test_delete_disappears_from_search(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "gone.md").write_text("temporary file about zephyr winds")

    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _index_vault(conn, src, vault)
        assert bm25_search(conn, "zephyr")
        (vault / "gone.md").unlink()
        from core.connectors.pdf import resolve_fs_path  # noqa: F401  (kept local)

        assert mark_deleted(conn, src, f"{vault}::gone.md") is True
        assert bm25_search(conn, "zephyr") == []


def test_skip_recorded_with_reason(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        ix.conn.execute(
            "INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')"
        )
        src = ix.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        doc_id = record_error(ix.conn, source_id=src, external_id="x",
                              uri="file:///x", error="exceeds 50MB: 999")
        row = ix.conn.execute("SELECT status, error FROM documents WHERE id = ?",
                              (doc_id,)).fetchone()
        assert row["status"] == "error"
        assert "50MB" in row["error"]


def test_search_api_returns_shaped_results(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from core.api.app import create_app

    token = "phase1-token-abcdefghijklmnop"
    monkeypatch.setenv("STACKSEARCH_TOKEN", token)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))
    app = create_app(open_database=True)
    conn = app.state.index.conn
    conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
    src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    index_document(conn, source_id=src, external_id="a", uri="file:///a.md",
                   title="a.md", text="searchable lighthouse content",
                   chunks=[ChunkWrite(text="searchable lighthouse content",
                                      loc_json="{}", token_count=3)])
    with TestClient(app) as client:
        resp = client.post("/search", json={"query": "lighthouse"},
                           headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["snippet"]
        assert "latency_ms" in body
    app.state.index.close()
