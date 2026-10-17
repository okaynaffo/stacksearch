"""Email chunker: one chunk per message body (§6.4, TODO 5.1).

Subject and sender are prepended as a ``[email] From: … Subject: …`` header,
which measurably improves retrieval (§6.4). Bodies longer than
``email_chunk_max_tokens`` (500) are packed by paragraph and hard-split as a
last resort, so no chunk exceeds the ceiling. ``loc_json`` carries the message
index, Message-ID, and part number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.config import get_settings
from core.extract.email import EmailMessage
from core.extract.normalize import estimate_tokens


@dataclass(frozen=True)
class EmailChunk:
    text: str  # includes the [email] header
    loc_json: str
    token_count: int


def _header(msg: EmailMessage) -> str:
    bits = ["[email]"]
    if msg.sender:
        bits.append(f"From: {msg.sender}")
    if msg.subject:
        bits.append(f"Subject: {msg.subject}")
    if msg.date:
        bits.append(f"Date: {msg.date}")
    return " ".join(bits)


def _pack(body: str, max_tokens: int) -> list[str]:
    """Pack paragraphs into pieces that fit the token budget."""
    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    if not paragraphs:
        return []
    pieces: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if estimate_tokens(candidate) <= max_tokens:
            current = candidate
            continue
        if current:
            pieces.append(current)
        if estimate_tokens(para) <= max_tokens:
            current = para
        else:
            # A single oversized paragraph: hard-split by characters.
            step = max_tokens * 4
            parts = [para[i : i + step] for i in range(0, len(para), step)] or [para]
            pieces.extend(parts[:-1])
            current = parts[-1]
    if current:
        pieces.append(current)
    return pieces


def chunk_emails(
    messages: list[EmailMessage], *, max_tokens: int | None = None
) -> list[EmailChunk]:
    """Chunk parsed messages; returns one or more chunks per message."""
    settings = get_settings()
    max_tokens = max_tokens or settings.indexing.email_chunk_max_tokens
    chunks: list[EmailChunk] = []
    for i, msg in enumerate(messages):
        header = _header(msg)
        body = msg.body or ""
        pieces = _pack(body, max_tokens) or [""]
        for part_no, piece in enumerate(pieces):
            full = f"{header}\n\n{piece}" if piece else header
            loc = {"msg_index": i, "part": part_no}
            if msg.message_id:
                loc["message_id"] = msg.message_id
            chunks.append(
                EmailChunk(
                    text=full,
                    loc_json=json.dumps(loc, sort_keys=True),
                    token_count=estimate_tokens(full),
                )
            )
    return chunks
