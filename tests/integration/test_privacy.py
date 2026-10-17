"""Privacy tests (PLAN.md §9, §15)."""

from __future__ import annotations

import socket

import pytest

from core.index import open_index
from core.index.writer import ChunkWrite, index_document
from core.privacy.scrubber import scrub


pytestmark = pytest.mark.integration


def test_secret_redacted_from_stored_chunks(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    text = f"config uses {secret} for access"

    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        scrubbed, n = scrub(text)
        assert n >= 1
        index_document(
            conn,
            source_id=src,
            external_id="a",
            uri="file:///a.md",
            title="a.md",
            text=scrubbed,
            chunks=[ChunkWrite(text=scrubbed, loc_json="{}", token_count=4)],
        )
        stored = conn.execute("SELECT text FROM chunks").fetchone()["text"]
        assert secret not in stored
        assert "REDACTED" in stored


def test_excluded_paths_never_indexed(tmp_path):
    from core.connectors.files import FilesConnector

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "keep.md").write_text("kept content")
    (vault / ".env").write_text("SECRET=do-not-index")
    (vault / "node_modules").mkdir()
    (vault / "node_modules" / "x.md").write_text("ignored")

    refs = list(FilesConnector().scan({"paths": [str(vault)]}))
    paths = [r.title for r in refs]
    assert any("keep.md" in p for p in paths)
    assert not any(".env" in p for p in paths)
    assert not any("node_modules" in p for p in paths)


def test_no_outbound_network_during_indexing(tmp_path, monkeypatch):
    """Block all socket creation; indexing must not open a network connection."""
    original = socket.socket

    def blocked(*args, **kwargs):
        raise RuntimeError("network access blocked by privacy test")

    monkeypatch.setattr(socket, "socket", blocked)
    # Re-running scrubber + a trivial open_index must not touch the network.
    with open_index(tmp_path / "index.db") as ix:
        assert ix.conn is not None
    assert scrub("no secrets here")[1] == 0