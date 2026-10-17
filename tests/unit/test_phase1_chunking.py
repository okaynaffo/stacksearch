"""Phase 1 chunker tests (PLAN.md §6.4, TODO 1.2)."""

from __future__ import annotations

import json

import pytest

from core.chunking.prose import chunk_prose
from core.extract.normalize import estimate_tokens

pytestmark = pytest.mark.unit


def test_short_text_is_one_chunk_with_header():
    chunks = chunk_prose("Hello world.", header="[note] a.md")
    assert len(chunks) == 1
    assert chunks[0].text.startswith("[note] a.md")
    assert "Hello world." in chunks[0].text


def test_chunks_stay_within_max_tokens():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(10))
    chunks = chunk_prose(text, header="[note] big.md", max_tokens=500)
    for ch in chunks:
        assert estimate_tokens(ch.text) <= 500 + 100  # header + overlap slack


def test_overlap_carries_previous_tail():
    text = "\n\n".join(f"Section {i} " + "alpha " * 80 for i in range(4))
    plain = chunk_prose(text, header="", max_tokens=300)
    assert len(plain) >= 2
    # Overlap means chunk 2 shares text with chunk 1's tail.
    assert any(w in plain[1].text for w in plain[0].text.split()[-5:])


def test_loc_json_has_chunk_ord_and_page():
    chunks = chunk_prose("Some text here.", header="[pdf] a.pdf", loc_extra={"page": 3})
    loc = json.loads(chunks[0].loc_json)
    assert loc["page"] == 3
    assert loc["chunk"] == 0


def test_empty_text_returns_no_chunks():
    assert chunk_prose("   \n\n  ", header="[note] e.md") == []


def test_heading_split_produces_multiple_chunks():
    text = "# A\n\n" + "body " * 200 + "\n\n# B\n\n" + "other " * 200
    chunks = chunk_prose(text, header="[note] h.md", max_tokens=300)
    assert len(chunks) >= 2
