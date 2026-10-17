"""Phase 5 unit tests: email extract / chunk / scan (PLAN.md §6.2, §6.4, TODO 5.1).

Stdlib only — the IMAP connector's live path needs the optional ``phase5``
extra, so these pin the offline-first parsers, chunker, and local connector.
"""

from __future__ import annotations

import mailbox
from email.message import EmailMessage

import pytest

from core.chunking.email import chunk_emails
from core.connectors.email import EmailConnector
from core.extract.email import (
    html_to_text,
    parse_email_bytes,
    parse_email_container,
    parse_eml,
    parse_emlx,
    parse_mbox,
)

pytestmark = pytest.mark.unit


def _raw_email(
    body: str = "The cabin keys are under the mat.",
    *,
    subject: str = "Trip plan",
    sender: str = "Alice <alice@example.com>",
    to: str = "Bob <bob@example.com>",
    date: str = "Mon, 03 Mar 2025 10:15:23 +0000",
    message_id: str = "<abc123@example.com>",
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


# -- extract -----------------------------------------------------------------


def test_parse_eml_headers_and_body(tmp_path):
    path = tmp_path / "note.eml"
    path.write_bytes(_raw_email())
    parsed = parse_eml(path)
    assert parsed.subject == "Trip plan"
    assert parsed.sender == "alice@example.com"
    assert parsed.recipients == "bob@example.com"
    assert parsed.date == "2025-03-03"
    assert parsed.message_id == "abc123@example.com"
    assert "cabin keys" in parsed.body


def test_parse_eml_strips_quoted_reply_and_signature(tmp_path):
    body = "Fresh answer.\nOn Monday, Bob wrote:\nold quoted text\n-- \nAlice"
    path = tmp_path / "reply.eml"
    path.write_bytes(_raw_email(body))
    parsed = parse_eml(path)
    assert "Fresh answer." in parsed.body
    assert "old quoted" not in parsed.body
    assert "Alice" not in parsed.body


def test_parse_eml_html_fallback(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "HTML only"
    msg["From"] = "a@example.com"
    msg.set_content(
        "<html><body><h1>Storage</h1><p>pricing is <b>monthly</b></p>"
        "<script>ignore()</script></body></html>",
        subtype="html",
    )
    path = tmp_path / "html.eml"
    path.write_bytes(msg.as_bytes())
    parsed = parse_eml(path)
    assert "Storage" in parsed.body
    assert "monthly" in parsed.body
    assert "ignore" not in parsed.body


def test_parse_emlx_strips_plist_trailer(tmp_path):
    raw = _raw_email(subject="From Apple Mail")
    blob = str(len(raw)).encode() + b"\n" + raw + b'<?xml version="1.0"?><plist></plist>'
    path = tmp_path / "1.emlx"
    path.write_bytes(blob)
    parsed = parse_emlx(path)
    assert parsed.subject == "From Apple Mail"
    assert "<plist" not in parsed.body


def test_parse_emlx_rejects_bad_header(tmp_path):
    path = tmp_path / "broken.emlx"
    path.write_bytes(b"not-a-number\nhello")
    with pytest.raises(ValueError, match="bad byte count"):
        parse_emlx(path)


def test_parse_mbox_multiple_messages(tmp_path):
    path = tmp_path / "inbox.mbox"
    box = mailbox.mbox(str(path))
    box.lock()
    try:
        box.add(mailbox.mboxMessage(_raw_email(subject="First", message_id="<1@x>")))
        box.add(mailbox.mboxMessage(_raw_email(subject="Second", message_id="<2@x>")))
        box.flush()
    finally:
        box.unlock()
        box.close()
    messages = parse_mbox(path)
    assert [m.subject for m in messages] == ["First", "Second"]


def test_parse_email_container_dispatch(tmp_path):
    eml = tmp_path / "one.eml"
    eml.write_bytes(_raw_email())
    assert len(parse_email_container(eml)) == 1
    assert parse_email_bytes(_raw_email()).subject == "Trip plan"


def test_html_to_text_skips_script():
    text = html_to_text("<div>visible</div><style>hidden</style><script>bad</script>")
    assert "visible" in text
    assert "hidden" not in text and "bad" not in text


# -- chunking ----------------------------------------------------------------


def test_chunk_emails_header_and_loc(tmp_path):
    path = tmp_path / "a.eml"
    path.write_bytes(_raw_email())
    messages = [parse_eml(path)]
    chunks = chunk_emails(messages)
    assert len(chunks) == 1
    assert "[email]" in chunks[0].text
    assert "alice@example.com" in chunks[0].text
    assert "Subject: Trip plan" in chunks[0].text
    assert '"msg_index": 0' in chunks[0].loc_json


def test_chunk_emails_splits_long_body(tmp_path):
    long_body = "\n\n".join(f"Paragraph {i} " + ("word " * 120) for i in range(20))
    path = tmp_path / "long.eml"
    path.write_bytes(_raw_email(long_body))
    messages = [parse_eml(path)]
    chunks = chunk_emails(messages, max_tokens=200)
    assert len(chunks) > 1
    assert all(c.token_count <= 400 for c in chunks)  # ceiling respected
    assert chunks[0].text.startswith("[email]")


# -- connector ---------------------------------------------------------------


def test_email_connector_scan_files_and_maildir(tmp_path):
    (tmp_path / "note.eml").write_bytes(_raw_email())
    (tmp_path / "apple.emlx").write_bytes(
        str(len(_raw_email())).encode() + b"\n" + _raw_email()
    )
    maildir = tmp_path / "Maildir"
    for sub in ("cur", "new", "tmp"):
        (maildir / sub).mkdir(parents=True)
    (tmp_path / "ignore.md").write_text("# not email\n")
    refs = list(EmailConnector().scan({"paths": [str(tmp_path)]}))
    titles = sorted(r.title for r in refs)
    assert titles == ["Maildir/", "apple.emlx", "note.eml"]
