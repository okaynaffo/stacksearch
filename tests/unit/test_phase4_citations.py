"""Phase 4 citation-verifier tests (PLAN.md §8.3, TODO 4.3/4.4)."""

from __future__ import annotations

import pytest

from core.llm.citations import (
    parse_citations,
    refusal_verification,
    sentence_supported,
    verify_answer,
)

pytestmark = pytest.mark.unit


def _src(n: int, text: str):
    return {"n": n, "chunk_id": n, "text": text}


def test_parse_citations_in_order():
    assert parse_citations("A [2] then [1] and [2] again.") == [2, 1, 2]
    assert parse_citations("no citations") == []
    assert parse_citations("malformed [1,2] and [] give") == []


def test_valid_citations_survive_and_ground():
    v = verify_answer(
        "You start November 3 [1].",
        [_src(1, "Your start date will be November 3 at the office.")],
    )
    assert v.grounded is True
    assert v.valid_citations == [1]
    assert v.text == "You start November 3 [1]."
    assert v.flags == []
    assert v.support_rate == pytest.approx(1.0)


def test_invalid_numbers_stripped_and_flagged():
    v = verify_answer(
        "Start November 3 [1] and also [9].",
        [_src(1, "Your start date will be November 3.")],
    )
    assert v.valid_citations == [1]
    assert "[9]" not in v.text
    assert "[1]" in v.text
    assert any("[9]" in f for f in v.flags)
    assert v.grounded is True


def test_malformed_brackets_stripped():
    v = verify_answer("Something [] here [a] done [1].", [_src(1, "Something here done.")])
    assert "[]" not in v.text and "[a]" not in v.text
    assert v.valid_citations == [1]


def test_zero_valid_citations_is_ungrounded():
    v = verify_answer("The moon is cheese [7].", [_src(1, "Start November 3.")])
    assert v.grounded is False
    assert v.valid_citations == []
    assert "[7]" not in v.text


def test_weak_support_flagged_not_dropped():
    v = verify_answer(
        "Quantum bananas orbit Mars [1].",
        [_src(1, "Your start date will be November 3 at the office.")],
    )
    assert v.grounded is True  # citation is valid even if the claim is weak
    assert v.support_rate == pytest.approx(0.0)
    assert any("weak support" in f for f in v.flags)


def test_sentence_supported_threshold():
    assert sentence_supported("start november 3", "Your start date will be November 3.")
    assert not sentence_supported("quantum bananas orbit mars", "Start November 3 office.")


def test_refusal_path():
    v = refusal_verification("I couldn't find that in your data.")
    assert v.grounded is False
    assert v.text == "I couldn't find that in your data."
