"""Chat chunker: sliding time windows per conversation (§6.4, TODO 5.3).

~20 messages per window (``settings.indexing.chat_window_messages``), capped at
~400 tokens (``chat_chunk_max_tokens``); over-budget windows split in half so no
chunk exceeds the ceiling. Every chunk carries the ``[chat] conversation``
header and ``loc_json`` with the message range.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.config import get_settings
from core.extract.chat import Message
from core.extract.normalize import estimate_tokens


@dataclass(frozen=True)
class ChatChunk:
    text: str  # includes the [chat] header
    loc_json: str
    token_count: int


def _render(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        tag = f" [{m.ts}]" if m.ts else ""
        lines.append(f"{m.sender}{tag}: {m.text}")
    return "\n".join(lines)


def chunk_chat(
    messages: list[Message],
    *,
    header: str,
    window: int | None = None,
    max_tokens: int | None = None,
) -> list[ChatChunk]:
    """Window an export's messages into header-prefixed chunks."""
    settings = get_settings()
    window = window or settings.indexing.chat_window_messages
    max_tokens = max_tokens or settings.indexing.chat_chunk_max_tokens
    if not messages:
        return []

    windows: list[list[Message]] = [
        messages[i : i + window] for i in range(0, len(messages), window)
    ]
    # Split over-budget windows in half until all fit (or single messages).
    settled: list[list[Message]] = []
    queue = list(windows)
    while queue:
        w = queue.pop(0)
        body = f"{header}\n\n{_render(w)}" if header else _render(w)
        if estimate_tokens(body) <= max_tokens or len(w) == 1:
            settled.append(w)
        else:
            mid = len(w) // 2
            queue.insert(0, w[mid:])
            queue.insert(0, w[:mid])

    chunks: list[ChatChunk] = []
    offset = 0
    for i, w in enumerate(settled):
        body = _render(w)
        full = f"{header}\n\n{body}" if header else body
        participants = sorted({m.sender for m in w})
        loc = {
            "window": i,
            "msg_start": offset,
            "msg_end": offset + len(w) - 1,
            "participants": participants,
        }
        chunks.append(
            ChatChunk(
                text=full,
                loc_json=json.dumps(loc, sort_keys=True),
                token_count=estimate_tokens(full),
            )
        )
        offset += len(w)
    return chunks
