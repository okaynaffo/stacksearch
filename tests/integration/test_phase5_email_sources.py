"""Phase 5 integration: email through the queue worker path (§6.5, TODO 5.1).

``embed=False`` keeps ONNX out of tests. Local containers exercise the real
stdlib path; IMAP is driven by a fake ``imapclient`` module so the cursor and
fetch logic are covered without the optional extra or a network.
"""

from __future__ import annotations

import json
import mailbox
import sys
import types
from email.message import EmailMessage

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


def _raw(subject: str, body: str, message_id: str = "<m@example.com>") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg["Date"] = "Mon, 03 Mar 2025 10:15:23 +0000"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


def test_eml_index_and_search(tmp_path):
    (tmp_path / "note.eml").write_bytes(_raw("Storage pricing", "zephyr storage is monthly"))
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _add_source(conn, "email", {"paths": [str(tmp_path)]})
        from core.connectors.email import EmailConnector

        refs = list(EmailConnector().scan({"paths": [str(tmp_path)]}))
        assert len(refs) == 1
        report = run_index_doc(conn, src, refs[0].external_id, embed=False)
        assert report["ok"]
        assert bm25_search(conn, "zephyr")
        assert bm25_search(conn, "pricing")
        row = conn.execute(
            "SELECT author FROM documents WHERE external_id = ?", (refs[0].external_id,)
        ).fetchone()
        assert row["author"] == "alice@example.com"


def test_mbox_index_and_search(tmp_path):
    path = tmp_path / "inbox.mbox"
    box = mailbox.mbox(str(path))
    box.lock()
    try:
        box.add(mailbox.mboxMessage(_raw("Offer", "the offer id is 48213", "<1@x>")))
        box.add(mailbox.mboxMessage(_raw("Recipe", "lentil soup with saffron", "<2@x>")))
        box.flush()
    finally:
        box.unlock()
        box.close()
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        src = _add_source(conn, "email", {"paths": [str(path)]})
        from core.connectors.email import EmailConnector

        refs = list(EmailConnector().scan({"paths": [str(path)]}))
        assert len(refs) == 1
        assert run_index_doc(conn, src, refs[0].external_id, embed=False)["ok"]
        assert bm25_search(conn, "48213")
        assert bm25_search(conn, "saffron")


class _FakeIMAPClient:
    """Minimal imapclient stand-in: three messages in INBOX, UIDVALIDITY 7."""

    def __init__(self, host, port=993, ssl=True):
        self.host = host

    def login(self, account, password):
        assert password == "pw"

    def select_folder(self, folder, readonly=False):
        self.folder = folder

    def folder_status(self, folder, keys):
        return {b"UIDVALIDITY": 7}

    def search(self, criteria):
        if criteria == ["ALL"]:
            return [1, 2, 3]
        return [3]  # UID last+1:*

    def fetch(self, uids, keys):
        return {
            uid: {b"RFC822": _raw("IMAP hi", f"zephyr message {uid}", f"<{uid}@imap>")}
            for uid in uids
        }

    def logout(self):
        pass


def test_imap_scan_fetch_and_cursor(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "imapclient", types.SimpleNamespace(IMAPClient=_FakeIMAPClient)
    )
    monkeypatch.setattr("core.privacy.keychain.get_secret", lambda service, account: "pw")
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        config = {"account": "me@example.com", "host": "imap.example.com", "folders": ["INBOX"]}
        src = _add_source(conn, "email_imap", config)
        from core.connectors.email_imap import ImapEmailConnector

        refs = list(ImapEmailConnector().scan({**config, "_cursor": None}))
        assert [r.external_id for r in refs] == [
            "imap://me@example.com/INBOX::1",
            "imap://me@example.com/INBOX::2",
            "imap://me@example.com/INBOX::3",
        ]
        report = run_index_doc(conn, src, refs[0].external_id, embed=False)
        assert report["ok"]
        assert bm25_search(conn, "zephyr")
        uri = conn.execute(
            "SELECT uri FROM documents WHERE external_id = ?", (refs[0].external_id,)
        ).fetchone()["uri"]
        assert uri.startswith("message://")
        cursor_row = conn.execute(
            "SELECT cursor FROM sources WHERE id = ?", (src,)
        ).fetchone()
        cursor = json.loads(cursor_row[0])
        assert cursor["INBOX"] == {"uid": 1, "uidvalidity": 7}
