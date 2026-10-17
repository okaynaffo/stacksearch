"""Phase 5 integration: code / chat / browser through the queue worker path (§6.5).

Exercises ``run_index_doc`` with ``embed=False`` (no ONNX in tests): scan →
extract → chunk → write → BM25 search, plus the browser cursor advance.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.index import open_index
from core.jobs.tasks import run_index_doc
from core.query.bm25 import bm25_search

pytestmark = pytest.mark.integration


def _add_source(conn, kind: str, config: dict) -> int:
    conn.execute(
        "INSERT INTO sources(kind, name, config_json) VALUES (?, ?, ?)",
        (kind, f"test-{kind}", json.dumps(config)),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_code_index_and_search(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def frobnicate(items):\n    return [x * 2 for x in items]\n"
    )
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _add_source(conn, "code", {"paths": [str(repo)]})
        from core.connectors.code import CodeConnector

        refs = list(CodeConnector().scan({"paths": [str(repo)]}))
        assert len(refs) == 1
        report = run_index_doc(conn, src, refs[0].external_id, embed=False)
        assert report["ok"]
        results = bm25_search(conn, "frobnicate")
        assert len(results) == 1
        assert "calc.py" in results[0]["title"]


def test_chat_index_and_search(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    (d / "family.txt").write_text(
        "[12/03/2025, 10:15:23] Alice: the cabin keys are under the mat\n"
        "[12/03/2025, 10:16:00] Bob: got it, zephyr trip confirmed\n"
    )
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _add_source(conn, "chat", {"paths": [str(d)]})
        from core.connectors.chat import ChatConnector

        refs = list(ChatConnector().scan({"paths": [str(d)]}))
        assert len(refs) == 1
        report = run_index_doc(conn, src, refs[0].external_id, embed=False)
        assert report["ok"]
        assert bm25_search(conn, "zephyr")
        assert bm25_search(conn, "cabin keys")


def test_browser_index_search_and_cursor(tmp_path):
    profile = tmp_path / "Chrome" / "Default"
    profile.mkdir(parents=True)
    db = profile / "History"
    conn0 = sqlite3.connect(db)
    conn0.execute(
        "CREATE TABLE urls (url TEXT, title TEXT, last_visit_time INTEGER, visit_count INTEGER)"
    )
    base = int((1754000000 + 11644473600) * 1_000_000)
    conn0.execute(
        "INSERT INTO urls VALUES (?, ?, ?, ?)",
        ("https://lighthouse-docs.example/", "Lighthouse Manual", base, 3),
    )
    conn0.commit()
    conn0.close()
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _add_source(conn, "browser", {"paths": [str(tmp_path)]})
        from core.connectors.browser import BrowserConnector

        refs = list(BrowserConnector().scan({"paths": [str(tmp_path)]}))
        assert len(refs) == 1
        report = run_index_doc(conn, src, refs[0].external_id, embed=False)
        assert report["ok"]
        results = bm25_search(conn, "lighthouse")
        assert results
        snippet = results[0]["snippet"].replace("<mark>", "").replace("</mark>", "")
        assert "ighthouse" in snippet  # title and/or URL survive chunking
        cursor = conn.execute("SELECT cursor FROM sources WHERE id = ?", (src,)).fetchone()[0]
        assert cursor  # last-visit cursor advanced (§6.2)


def test_scan_delete_roundtrip_code(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "gone.py").write_text("def zephyr_wind():\n    pass\n")
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _add_source(conn, "code", {"paths": [str(repo)]})
        from core.connectors.code import CodeConnector
        from core.jobs.tasks import run_delete_doc

        refs = list(CodeConnector().scan({"paths": [str(repo)]}))
        run_index_doc(conn, src, refs[0].external_id, embed=False)
        assert bm25_search(conn, "zephyr_wind")
        (repo / "gone.py").unlink()
        assert run_delete_doc(conn, src, refs[0].external_id)["ok"]
        assert bm25_search(conn, "zephyr_wind") == []
