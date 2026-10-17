"""Phase 6 API tests: source CRUD + documents (PLAN.md §10, §11.3; TODO 6.4).

The routes the settings UI is built on. Auth is structural (the router carries the
dependency), so these focus on behaviour and on "forget source" actually removing
vectors, which have no cascade.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app

TOKEN = "phase6-test-token-abcdefghijk"
AUTH = {"X-Local-Token": TOKEN}

pytestmark = pytest.mark.integration


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKSEARCH_TOKEN", TOKEN)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))
    app = create_app(open_database=True)
    with TestClient(app) as c:
        yield c
    if app.state.index is not None:
        app.state.index.close()


def test_sources_routes_require_a_token(client):
    assert client.get("/sources").status_code == 401
    assert client.post("/sources", json={"kind": "files", "name": "x"}).status_code == 401
    assert client.get("/documents/1").status_code == 401


def test_list_sources_starts_empty(client):
    body = client.get("/sources", headers=AUTH).json()
    assert body == {"sources": []}


def test_create_source_enqueues_a_scan(client):
    resp = client.post(
        "/sources",
        headers=AUTH,
        json={"kind": "files", "name": "Notes", "config": {"paths": ["/tmp/nonexistent"]}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source"]["kind"] == "files"
    assert body["source"]["name"] == "Notes"
    assert body["source"]["enabled"] is True
    assert body["job_id"] is not None

    listed = client.get("/sources", headers=AUTH).json()["sources"]
    assert len(listed) == 1
    assert listed[0]["config"]["paths"] == ["/tmp/nonexistent"]


def test_create_source_rejects_an_unknown_kind(client):
    resp = client.post("/sources", headers=AUTH, json={"kind": "telepathy", "name": "x"})
    assert resp.status_code == 422


def test_create_disabled_source_skips_the_scan(client):
    body = client.post(
        "/sources", headers=AUTH, json={"kind": "chat", "name": "quiet", "enabled": False}
    ).json()
    assert body["job_id"] is None
    assert body["source"]["enabled"] is False


def test_patch_source_toggles_and_renames(client):
    sid = client.post(
        "/sources", headers=AUTH, json={"kind": "chat", "name": "old", "enabled": False}
    ).json()["source"]["id"]
    resp = client.patch(
        f"/sources/{sid}", headers=AUTH, json={"name": "new", "enabled": True}
    )
    assert resp.status_code == 200
    src = resp.json()["source"]
    assert src["name"] == "new"
    assert src["enabled"] is True


def test_patch_unknown_source_404(client):
    assert client.patch("/sources/999", headers=AUTH, json={"name": "x"}).status_code == 404


def test_delete_forgets_documents_chunks_and_vectors(client):
    from core.index import pack_embedding

    conn = client.app.state.index.conn
    sid = client.post(
        "/sources", headers=AUTH, json={"kind": "chat", "name": "doomed", "enabled": False}
    ).json()["source"]["id"]
    conn.execute(
        "INSERT INTO documents(source_id, external_id, uri, content_hash) VALUES (?,?,?,?)",
        (sid, "vault::a.md", "file:///a.md", "h"),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO chunks(document_id, ord, text, chunk_hash, loc_json, token_count)"
        " VALUES (?,0,'zephyr text','ch','{}',3)",
        (doc_id,),
    )
    chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, pack_embedding([0.0] * 768)),
    )
    conn.commit()

    resp = client.delete(f"/sources/{sid}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["documents"] == 1
    assert resp.json()["chunks"] == 1

    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 0


def test_delete_unknown_source_404(client):
    assert client.delete("/sources/123", headers=AUTH).status_code == 404


def test_get_document_returns_metadata(client, tmp_path):
    conn = client.app.state.index.conn
    conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','t','{}')")
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO documents(source_id, external_id, uri, title, content_hash)"
        " VALUES (?,?,?,?,?)",
        (sid, "vault::a.md", "file:///a.md", "a.md", "h"),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    body = client.get(f"/documents/{doc_id}", headers=AUTH).json()
    assert body["uri"] == "file:///a.md"
    assert body["title"] == "a.md"
    assert body["chunks"] == 0


def test_get_document_unknown_404(client):
    assert client.get("/documents/424242", headers=AUTH).status_code == 404


def test_answer_model_switch_and_status(client, monkeypatch):
    import core.llm.server as server

    monkeypatch.setattr(server, "_supervisor", None)

    resp = client.post("/models/answer", headers=AUTH, json={"key": "8B"})
    assert resp.status_code == 200
    assert resp.json()["key"] == "8B"
    assert resp.json()["changed"] is True
    assert client.get("/status", headers=AUTH).json()["models"]["answer_llm"]["key"] == "8B"

    again = client.post("/models/answer", headers=AUTH, json={"key": "8B"})
    assert again.json()["changed"] is False

    assert client.post("/models/answer", headers=AUTH, json={"key": "huge"}).status_code == 422
    monkeypatch.setattr(server, "_supervisor", None)

