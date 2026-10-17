"""Prose chunker: headings/paragraphs → 300–500 tokens, ~15% overlap (§6.4, TODO 1.2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from core.config import get_settings
from core.extract.normalize import estimate_tokens

_HEADING_RE = re.compile(r"^#{1,6}\s+.*$")


@dataclass(frozen=True)
class ProseChunk:
    text: str  # includes the context header prefix
    loc_json: str
    token_count: int


def _split_paragraphs(text: str) -> list[str]:
    """Split on headings and blank lines, keeping headings attached to content."""
    blocks: list[str] = []
    current: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        lines = para.split("\n")
        for line in lines:
            if _HEADING_RE.match(line.strip()) and current:
                blocks.append("\n".join(current).strip())
                current = [line.strip()]
            else:
                current.append(line.strip())
        # Paragraph boundary ends the current block unless it is a lone heading.
        if len(lines) == 1 and _HEADING_RE.match(lines[0].strip()):
            continue
        if current and not (len(current) == 1 and _HEADING_RE.match(current[0])):
            blocks.append("\n".join(current).strip())
            current = []
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def chunk_prose(
    text: str,
    *,
    header: str,
    loc_extra: dict | None = None,
    min_tokens: int | None = None,
    max_tokens: int | None = None,
) -> list[ProseChunk]:
    """Pack paragraphs into header-prefixed chunks with overlap.

    ``header`` is the §6.4 context prefix, e.g. ``[note] notes/a.md``.
    ``loc_extra`` (e.g. ``{"page": 3}``) is merged into every chunk's loc_json.
    """
    settings = get_settings()
    min_tokens = min_tokens or settings.indexing.prose_chunk_min_tokens
    max_tokens = max_tokens or settings.indexing.prose_chunk_max_tokens
    overlap_ratio = settings.indexing.prose_overlap_ratio

    blocks = _split_paragraphs(text)
    if not blocks:
        return []

    # Greedy pack to max_tokens (heuristic count on body only; header is extra).
    packed: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for block in blocks:
        bt = estimate_tokens(block)
        if bt >= max_tokens:
            if current:
                packed.append("\n\n".join(current))
                current, current_tokens = [], 0
            # Hard-split an oversized block by characters.
            chars_per_token = 4
            step = max_tokens * chars_per_token
            for i in range(0, len(block), step):
                packed.append(block[i : i + step])
            continue
        if current and current_tokens + bt > max_tokens:
            packed.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(block)
        current_tokens += bt
    if current:
        packed.append("\n\n".join(current))

    chunks: list[ProseChunk] = []
    for i, body in enumerate(packed):
        # ~15% overlap: carry the tail of the previous body into this chunk.
        if i > 0 and overlap_ratio > 0:
            overlap_chars = int(len(body) * overlap_ratio)
            prev_tail = packed[i - 1][-overlap_chars:] if overlap_chars else ""
            if prev_tail:
                body = prev_tail + "\n\n" + body
        full = f"{header}\n\n{body}" if header else body
        loc = dict(loc_extra or {})
        loc["chunk"] = i
        chunks.append(
            ProseChunk(
                text=full,
                loc_json=json.dumps(loc, sort_keys=True),
                token_count=estimate_tokens(full),
            )
        )
    return chunks
