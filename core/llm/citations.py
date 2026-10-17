"""Citation parsing and verification (PLAN.md §8.3, TODO 4.3).

The contract: every ``[n]`` in the answer must name a source the prompt
actually provided. Failures are *visible, not silent* — invalid numbers are
stripped and flagged, weakly supported sentences are marked for the UI, and an
answer with zero valid citations becomes "No grounded answer" with just the
results list.

Support check is lexical-overlap only (token recall of the cited sentence
against the cited chunk). No embeddings here: the answering path must stay
fast and torch-free, and Phase 2 already showed lexical overlap separates
grounded from ungrounded claims well enough for a UI flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Matches ``[1]``, ``[12]``. Malformed groups like ``[1,2]`` or ``[]`` are
#: intentionally *not* matched — they are stripped separately as invalid.
_CITE_RE = re.compile(r"\[(\d+)\]")

#: Anything bracket-shaped that is not a clean ``[n]``: stripped as invalid.
_MALFORMED_RE = re.compile(r"\[(?:[^\d\[\]]*)\]")

#: Minimum token-recall for a cited sentence to count as supported. Tuned for
#: the flagging role: below this the UI shows "weak support", it never blocks.
SUPPORT_THRESHOLD = 0.35

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class CitedSentence:
    """One answer sentence plus its citation verdict."""

    text: str
    citations: list[int]
    valid: list[int] = field(default_factory=list)
    invalid: list[int] = field(default_factory=list)
    #: Per valid citation: does the sentence overlap the cited chunk?
    supported: dict[int, bool] = field(default_factory=dict)


@dataclass
class Verification:
    """Outcome of :func:`verify_answer`."""

    #: Answer text with invalid citations stripped.
    text: str
    sentences: list[CitedSentence]
    #: Sorted valid citation numbers referenced anywhere in the answer.
    valid_citations: list[int]
    #: Stripped problems, e.g. ``"removed invalid citation [9]"``.
    flags: list[str]
    #: True when at least one valid citation survives.
    grounded: bool
    #: Fraction of cited sentences whose claims overlap their chunk.
    support_rate: float | None = None


def parse_citations(text: str) -> list[int]:
    """Return every ``[n]`` number in order of appearance."""
    return [int(m.group(1)) for m in _CITE_RE.finditer(text or "")]


def split_answer_sentences(text: str) -> list[str]:
    """Split an answer into sentences, keeping citation markers attached."""
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]
    return parts or ([text.strip()] if text and text.strip() else [])


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def sentence_supported(
    sentence: str, chunk_text: str, *, threshold: float = SUPPORT_THRESHOLD
) -> bool:
    """Lexical support: fraction of sentence tokens present in the chunk."""
    sent_tokens = _tokenize(sentence)
    if not sent_tokens:
        return True
    chunk_vocab = set(_tokenize(chunk_text))
    if not chunk_vocab:
        return False
    hits = sum(1 for t in sent_tokens if t in chunk_vocab)
    return (hits / len(sent_tokens)) >= threshold


def verify_answer(
    answer_text: str,
    sources: list[dict[str, Any]],
    *,
    threshold: float = SUPPORT_THRESHOLD,
) -> Verification:
    """Verify every ``[n]`` in ``answer_text`` against ``sources``.

    ``sources`` are the numbered maps from ``prompts.build_answer_prompt``
    (each carries ``n`` and ``text``). Invalid numbers are stripped from the
    returned text and recorded in ``flags``; malformed brackets (``[]``,
    ``[a]``, ``[1,2]``) are stripped the same way.
    """
    by_n: dict[int, str] = {}
    for s in sources or []:
        try:
            by_n[int(s.get("n"))] = str(s.get("text") or "")
        except (TypeError, ValueError):
            continue
    valid_ns = set(by_n)

    raw = answer_text or ""
    flags: list[str] = []
    sentences: list[CitedSentence] = []
    stripped_parts: list[str] = []

    for sent in split_answer_sentences(raw):
        nums = parse_citations(sent)
        valid = sorted({n for n in nums if n in valid_ns})
        invalid = sorted({n for n in nums if n not in valid_ns})
        cleaned = sent
        for n in invalid:
            cleaned = cleaned.replace(f"[{n}]", "")
        # Strip malformed brackets left over (e.g. "[,]", "[a]", "[]").
        malformed = _MALFORMED_RE.findall(cleaned)
        for m in set(malformed):
            if not _CITE_RE.fullmatch(m):
                cleaned = cleaned.replace(m, "")
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if invalid:
            flags.append(f"removed invalid citation(s) {', '.join(f'[{n}]' for n in invalid)}")
        if malformed:
            flags.append(f"removed malformed citation(s) {', '.join(sorted(set(malformed)))}")
        supported: dict[int, bool] = {}
        for n in valid:
            supported[n] = sentence_supported(cleaned, by_n[n], threshold=threshold)
        sentences.append(
            CitedSentence(
                text=cleaned, citations=nums, valid=valid, invalid=invalid, supported=supported
            )
        )
        stripped_parts.append(cleaned)

    text = " ".join(p for p in stripped_parts if p)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    valid_citations = sorted({n for s in sentences for n in s.valid})
    grounded = bool(valid_citations)
    support_checks = [ok for s in sentences for ok in s.supported.values()]
    if support_checks:
        support_rate = sum(1 for ok in support_checks if ok) / len(support_checks)
    else:
        support_rate = None
    if grounded and support_checks and not any(support_checks):
        flags.append("no cited sentence overlaps its source; showing as weak support")
    return Verification(
        text=text,
        sentences=sentences,
        valid_citations=valid_citations,
        flags=flags,
        grounded=grounded,
        support_rate=support_rate,
    )


def refusal_verification(answer_text: str) -> Verification:
    """Verification for the refusal / no-grounded-answer path (zero citations)."""
    return Verification(
        text=(answer_text or "").strip(),
        sentences=[],
        valid_citations=[],
        flags=[],
        grounded=False,
        support_rate=None,
    )
