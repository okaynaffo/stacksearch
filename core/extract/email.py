"""Email extraction: ``.eml``, Apple Mail ``.emlx``, ``mbox`` / Maildir (§6.2, TODO 5.1).

Stdlib only (``email``, ``mailbox``, ``html.parser``) so the offline-first
local path needs no optional extra; the IMAP connector is the part that pulls
in ``imapclient``. Body selection prefers ``text/plain`` and falls back to a
tag-stripped ``text/html``; the resulting text is passed through
:func:`core.extract.normalize.normalize_text`, which already removes quoted
replies below ``On … wrote:`` and ``-- `` signatures (§6.3).
"""

from __future__ import annotations

import email
import email.policy
import mailbox
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from core.extract.normalize import normalize_text

#: Suffixes the local connector recognizes directly (Maildir/mbox are dirs/files).
EMAIL_SUFFIXES = frozenset({".eml", ".emlx"})

#: Hard cap on a single message body handed to the chunker, to bound memory.
MAX_BODY_CHARS = 2_000_000


@dataclass(frozen=True)
class EmailMessage:
    """One email, normalized for chunking and metadata."""

    subject: str | None
    sender: str | None
    recipients: str | None
    date: str | None  # ISO date, YYYY-MM-DD
    body: str
    message_id: str | None = None


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, skipping script/style/head noise."""

    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in ("p", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw: str) -> str:
    """Strip HTML to visible text (stdlib fallback for the optional trafilatura)."""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML must never be fatal
        pass
    return parser.text()


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _message_body(msg: Message) -> str:
    """Best body text: ``text/plain`` preferred, else stripped ``text/html``."""
    plain: list[str] = []
    html: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue  # v1 skips attachments (§6.2)
            if ctype == "text/plain":
                plain.append(_decode_part(part))
            elif ctype == "text/html":
                html.append(_decode_part(part))
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            html.append(_decode_part(msg))
        else:
            plain.append(_decode_part(msg))
    body = "\n".join(plain).strip() if plain else html_to_text("\n".join(html))
    return body[:MAX_BODY_CHARS]


def _iso_date(msg: Message) -> str | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        parsed: datetime = parsedate_to_datetime(raw)
        return parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _addresses(msg: Message, header: str) -> str | None:
    values = msg.get_all(header)
    if not values:
        return None
    addrs = [addr for _, addr in getaddresses(values) if addr]
    return ", ".join(addrs) if addrs else None


def message_from_email(msg: Message) -> EmailMessage:
    """Adapt a parsed ``email.message.Message`` into an :class:`EmailMessage`."""
    subject = msg.get("Subject")
    sender = _addresses(msg, "From") or msg.get("From")
    message_id = (msg.get("Message-ID") or "").strip()
    return EmailMessage(
        subject=subject.strip() if subject else None,
        sender=sender,
        recipients=_addresses(msg, "To"),
        date=_iso_date(msg),
        body=normalize_text(_message_body(msg)),
        message_id=message_id.strip("<>") or None,
    )


def parse_email_bytes(raw: bytes) -> EmailMessage:
    """Parse a single RFC822 message from bytes (used by the IMAP connector)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return message_from_email(msg)


def parse_eml(path: Path) -> EmailMessage:
    """Parse a single ``.eml`` file."""
    return parse_email_bytes(path.read_bytes())


def parse_emlx(path: Path) -> EmailMessage:
    """Parse an Apple Mail ``.emlx`` file.

    Format: a leading byte-count line, the RFC822 message, then an XML plist
    trailer. The count on the first line delimits the message, so the trailer
    is dropped without guessing where it starts.
    """
    raw = path.read_bytes()
    newline = raw.find(b"\n")
    if newline == -1:
        raise ValueError(f"not an emlx file (no header line): {path}")
    try:
        count = int(raw[:newline].strip())
    except ValueError as exc:
        raise ValueError(f"not an emlx file (bad byte count): {path}") from exc
    body = raw[newline + 1 : newline + 1 + count]
    msg = email.message_from_bytes(body, policy=email.policy.default)
    return message_from_email(msg)


def parse_mbox(path: Path) -> list[EmailMessage]:
    """Parse an ``mbox`` file into messages (stdlib ``mailbox``)."""
    box = mailbox.mbox(str(path), create=False)
    try:
        return [message_from_email(m) for m in box]
    finally:
        box.close()


def parse_maildir(path: Path) -> list[EmailMessage]:
    """Parse a Maildir directory into messages (stdlib ``mailbox``)."""
    box = mailbox.Maildir(str(path), create=False)
    try:
        return [message_from_email(m) for m in box]
    finally:
        box.close()


def parse_email_container(path: Path) -> list[EmailMessage]:
    """Parse any supported email container into a list of messages.

    Directories are treated as Maildir; ``.eml`` / ``.emlx`` files yield one
    message; everything else is read as mbox.
    """
    if path.is_dir():
        return parse_maildir(path)
    suffix = path.suffix.lower()
    if suffix == ".emlx":
        return [parse_emlx(path)]
    if suffix == ".eml":
        return [parse_eml(path)]
    return parse_mbox(path)
