"""Text normalization (PLAN.md §6.3, TODO 1.1).

Structure stays as metadata, never as text: this module only cleans the string.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"[ \t\x0b\x0c\r]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# Common email reply delimiter: "On <date>, <someone> wrote:"
_QUOTED_REPLY_RE = re.compile(r"\nOn .+ wrote:\n.*$", re.DOTALL)
_SIGNATURE_RE = re.compile(r"\n-- \n.*$", re.DOTALL)


def normalize_text(raw: str | bytes) -> str:
    """Normalize to UTF-8, collapse whitespace, strip quotes/signatures (§6.3)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _QUOTED_REPLY_RE.sub("\n", text)
    text = _SIGNATURE_RE.sub("\n", text)
    text = _WHITESPACE_RE.sub(" ", text)
    # Collapse horizontal whitespace but keep paragraph breaks.
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def is_binary(data: bytes, *, sample_size: int = 8192) -> bool:
    """Heuristic binary detect: null byte in the head sample (PLAN.md §6.3)."""
    return b"\x00" in data[:sample_size]


def estimate_tokens(text: str) -> int:
    """Char-heuristic token count (~4 chars/token).

    Phase 1 standardizes on this rather than tiktoken: no new dependency, fast,
    and within ~10% for English prose — fine for the 300–500 chunk target.
    """
    return max(1, len(text) // 4)
