"""Phase 5 unit tests: code / chat / browser extract + chunk + scan (PLAN.md §6.2, §6.4).

Pure stdlib + repo deps only: tree-sitter and trafilatura are optional extras
and deliberately not required here — the regex fallback and the no-fetch
history path are what these tests pin.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.chunking.chat import chunk_chat
from core.chunking.code import split_code
from core.connectors.browser import BrowserConnector
from core.connectors.chat import ChatConnector
from core.connectors.code import CodeConnector
from core.extract.browser import read_history
from core.extract.chat import parse_chat_export

pytestmark = pytest.mark.unit


# -- code --------------------------------------------------------------------


def test_split_code_small_file_whole():
    text = "def hello():\n    return 1\n"
    chunks = split_code(text, header="[code] a.py", language="python")
    assert len(chunks) == 1
    assert "[code] a.py" in chunks[0].text
    assert "hello" in chunks[0].text


def test_split_code_per_function_with_lines(tmp_path=None):
    body = "\n".join(f"    line{i}" for i in range(200))
    text = f"def alpha():\n{body}\n\ndef beta():\n{body}\n"
    chunks = split_code(text, header="[code] big.py", language="python")
    symbols = [json.loads(c.loc_json)["symbol"] for c in chunks]
    assert "alpha" in symbols and "beta" in symbols
    loc = json.loads(chunks[0].loc_json)
    assert loc["line_start"] >= 1 and loc["line_end"] >= loc["line_start"]
    assert all(c.token_count <= 600 * 2 for c in chunks)  # hard-split ceiling respected


def test_split_code_unknown_suffix_fallback():
    text = "line one\n" * 400
    chunks = split_code(text, header="[code] data.xyz")
    assert len(chunks) >= 1
    assert all("[code] data.xyz" in c.text for c in chunks)


def test_code_connector_respects_gitignore(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    pass\n")
    (repo / "src" / "skip_me.py").write_text("x = 1\n")
    (repo / "target").mkdir()
    (repo / "target" / "out.rs").write_text("fn main() {}\n")
    (repo / ".gitignore").write_text("skip_me.py\ntarget/\n")
    (repo / "notes.md").write_text("# not code\n")
    refs = list(CodeConnector().scan({"paths": [str(repo)]}))
    titles = sorted(r.title for r in refs)
    assert titles == ["src/a.py"]


# -- chat --------------------------------------------------------------------


def test_parse_whatsapp_and_window(tmp_path):
    export = tmp_path / "chat.txt"
    lines = ["[12/03/2025, 10:15:23] Alice: hello there"]
    for i in range(25):
        lines.append(f"[12/03/2025, 10:{16 + i // 60:02d}:{i % 60:02d}] Bob: msg {i}")
    export.write_text("\n".join(lines) + "\n")
    conv, messages = parse_chat_export(export)
    assert conv == "chat"
    assert len(messages) == 26
    chunks = chunk_chat(messages, header="[chat] chat")
    assert len(chunks) == 2  # 20 + 6
    loc = json.loads(chunks[0].loc_json)
    assert loc["msg_start"] == 0 and loc["msg_end"] == 19
    assert "Bob" in chunks[0].text


def test_parse_telegram_and_discord(tmp_path):
    tg = tmp_path / "tg.json"
    tg.write_text(
        json.dumps(
            {
                "name": "Family",
                "messages": [
                    {"from": "Mom", "date": "2025-01-01", "text": "dinner at six"},
                    {"type": "service", "text": ""},
                    {"from": "Dad", "date": "2025-01-01", "text": ["see ", {"text": "you"}]},
                ],
            }
        )
    )
    conv, messages = parse_chat_export(tg)
    assert (conv, len(messages)) == ("Family", 2)
    assert messages[1].text == "see you"

    dc = tmp_path / "dc.json"
    dc.write_text(
        json.dumps(
            [{"author": {"username": "zoe"}, "timestamp": "2025-02-02", "content": "deploy done"}]
        )
    )
    _, dm = parse_chat_export(dc)
    assert dm[0].sender == "zoe"


def test_chat_connector_scan(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    (d / "wa.txt").write_text("[01/01/2025, 10:00:00] A: hi\n")
    (d / "tg.json").write_text(json.dumps({"messages": [{"from": "A", "text": "hi"}]}))
    (d / "notes.md").write_text("# no\n")
    refs = list(ChatConnector().scan({"paths": [str(d)]}))
    assert sorted(r.title for r in refs) == ["tg.json", "wa.txt"]


# -- browser -----------------------------------------------------------------


def _chrome_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls (url TEXT, title TEXT, last_visit_time INTEGER, visit_count INTEGER)"
    )
    # 2025-06-01 in Chrome microseconds.
    base = int((1754000000 + 11644473600) * 1_000_000)
    for i, (url, title) in enumerate(rows):
        conn.execute(
            "INSERT INTO urls VALUES (?, ?, ?, ?)", (url, title, base - i * 86_400_000_000, i)
        )
    conn.commit()
    conn.close()


def test_read_history_chrome_copy_not_in_place(tmp_path):
    db = tmp_path / "History"
    _chrome_db(db, [("https://example.com/a", "Alpha"), ("https://example.com/b", "Beta")])
    entries = read_history(db)
    assert [e.url for e in entries] == ["https://example.com/a", "https://example.com/b"]
    assert entries[0].last_visit is not None
    # since-filter keeps undated rows and drops older ones.
    filtered = read_history(db, since=entries[1].last_visit)
    assert all(e.url != entries[1].url or e.last_visit is None for e in filtered)


def test_read_history_unknown_schema(tmp_path):
    db = tmp_path / "History"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nope (x TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="unrecognized history schema"):
        read_history(db)


def test_browser_connector_finds_profiles(tmp_path):
    chrome = tmp_path / "Chrome" / "Default"
    chrome.mkdir(parents=True)
    _chrome_db(chrome / "History", [("https://example.com/", "Home")])
    (tmp_path / "random.sqlite").write_text("x")
    refs = list(BrowserConnector().scan({"paths": [str(tmp_path)]}))
    assert len(refs) == 1
    assert refs[0].title.startswith("chrome/")
