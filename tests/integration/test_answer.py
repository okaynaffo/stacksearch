"""Phase 4 integration: POST /answer streams verified grounded answers (PLAN.md §10, TODO 4.2/4.3).

The LLM is faked at the client level (``app.state.llm_client``), so these run
with no ``llama-server`` and no downloaded models. The vector path is forced
to degrade to BM25-only the same way — retrieval itself is covered by the
Phase 2/3 evals, not here.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.index.writer import ChunkWrite, index_document

TOKEN = "test-token-abcdefghijklmnop"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

pytestmark = pytest.mark.integration


class _Chunk:
    def __init__(self, text: str):
        self.text = text


class FakeClient:
    """Mocked LLM: streams ``answer`` verbatim, then reports ready."""

    def __init__(self, answer: str):
        self.answer = answer
        self.seen_messages: list | None = None

    def _ensure_ready(self) -> None:
        return None

    def stream(self, messages, **kwargs):
        self.seen_messages = messages
        words = self.answer.split(" ")
        for i, w in enumerate(words):
            yield _Chunk(w if i == 0 else " " + w)

    def chat(self, messages, **kwargs):
        self.seen_messages = messages

        class _R:
            text = self.answer

        return _R()


class DownClient:
    def _ensure_ready(self) -> None:
        raise RuntimeError("no LLM endpoint")


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    """App with one indexed note plus a BM25-only retrieval path.

    The note is a real file inside a vault the source points at, so the
    worker's reconcile scan (started by the lifespan) finds it and keeps it
    ``indexed``. A config-less source would be reconciled as deleted, which
    is correct behaviour but empties every answer fixture.
    """
    monkeypatch.setenv("STACKSEARCH_TOKEN", TOKEN)
    monkeypatch.setenv("STACKSEARCH_DATA_DIR", str(tmp_path / "data"))

    # No model downloads in tests: the vector path degrades to BM25-only.
    import core.models.embedder as _emb

    def _no_model(*args, **kwargs):
        raise RuntimeError("no model")

    monkeypatch.setattr(_emb, "get_embedder", _no_model)

    import json as _json

    from core.chunking.prose import chunk_prose
    from core.connectors.files import FilesConnector
    from core.connectors.pdf import resolve_fs_path
    from core.extract.text import extract_by_suffix

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "start.md").write_text("Your start date will be November 3 at the office. Bring ID.")

    app = create_app(open_database=True)
    conn = app.state.index.conn
    config = {"paths": [str(vault)]}
    conn.execute(
        "INSERT INTO sources(kind, name, config_json) VALUES ('files','v',?)",
        (_json.dumps(config),),
    )
    src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for ref in FilesConnector().scan(config):
        text = extract_by_suffix(resolve_fs_path(ref.external_id))
        chunks = [
            ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
            for c in chunk_prose(text, header=f"[note] {ref.title}")
        ]
        index_document(
            conn,
            source_id=src,
            external_id=ref.external_id,
            uri=ref.uri,
            title=ref.title,
            text=text,
            chunks=chunks,
        )
    with TestClient(app) as c:
        yield app, c
    app.state.index.close()


def _frames(resp) -> list[tuple[str, dict]]:
    """Parse an SSE stream into (event, data) pairs."""
    out: list[tuple[str, dict]] = []
    event: str | None = None
    for line in resp.iter_lines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event is not None:
            out.append((event, json.loads(line.split(":", 1)[1].strip())))
            event = None
    return out


def test_answer_requires_a_token(seeded):
    _, c = seeded
    assert c.post("/answer", json={"query": "x"}).status_code == 401


def test_answer_streams_sources_then_done(seeded):
    app, c = seeded
    app.state.llm_client = FakeClient("You start November 3 [1].")
    with c.stream("POST", "/answer", json={"query": "start date office"}, headers=AUTH) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = _frames(resp)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "sources"
    assert "delta" in kinds
    assert kinds[-1] == "done"
    done = frames[-1][1]
    assert done["grounded"] is True
    assert done["citations"] == [1]
    assert done["text"] == "You start November 3 [1]."
    assert done["sources"] and done["sources"][0]["text"]
    assert "November 3" in done["sources"][0]["text"]


def test_answer_strips_invalid_citations(seeded):
    app, c = seeded
    app.state.llm_client = FakeClient("Start November 3 [1] forever [9].")
    with c.stream("POST", "/answer", json={"query": "start date office"}, headers=AUTH) as resp:
        frames = _frames(resp)
    done = frames[-1][1]
    assert done["citations"] == [1]
    assert "[9]" not in done["text"]
    assert any("[9]" in f for f in done["flags"])


def test_answer_with_no_citations_is_refusal(seeded):
    app, c = seeded
    app.state.llm_client = FakeClient("The moon is made of cheese, definitely.")
    with c.stream("POST", "/answer", json={"query": "Moon cheese?"}, headers=AUTH) as resp:
        frames = _frames(resp)
    done = frames[-1][1]
    assert done["grounded"] is False
    assert done["text"] == "I couldn't find that in your data."


def test_answer_empty_query_is_refusal(seeded):
    app, c = seeded
    app.state.llm_client = FakeClient("should never be called")
    with c.stream("POST", "/answer", json={"query": "  "}, headers=AUTH) as resp:
        frames = _frames(resp)
    done = frames[-1][1]
    assert done["grounded"] is False
    assert "couldn't find that" in done["text"]


def test_answer_llm_down_yields_error_frame(seeded):
    app, c = seeded
    app.state.llm_client = DownClient()
    with c.stream("POST", "/answer", json={"query": "start date office"}, headers=AUTH) as resp:
        frames = _frames(resp)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "sources"
    assert kinds[-1] == "error"
    assert "LLM unavailable" in frames[-1][1]["error"]


def test_answer_evicts_index_models(seeded, monkeypatch):
    """§6.7: the answering transition evicts the indexer-side models."""
    import core.llm.answer as _ans

    app, c = seeded
    app.state.llm_client = FakeClient("You start November 3 [1].")
    calls: list[str] = []
    monkeypatch.setattr(_ans, "evict_index_models", lambda: calls.append("evicted"))
    with c.stream("POST", "/answer", json={"query": "start date office"}, headers=AUTH):
        pass
    assert calls == ["evicted"]
