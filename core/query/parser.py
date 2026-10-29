"""Rule-based query understanding (PLAN.md §7.1, TODO 2.1).

LLM-based filter extraction is an explicit v2; Phase 2 parses with rules and
falls back to unfiltered search when nothing matches. The parser never rejects
a query — worst case it returns the raw text with empty filters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from core.query.filters import QueryFilters

#: Source hints ("email", "in my notes", "PDF", "code") become a `sources.kind`
#: filter (§7.1). Keys are lowercase substrings; values are `sources.kind`.
SOURCE_HINTS: dict[str, str] = {
    "email": "email",
    "mail": "email",
    "inbox": "email",
    "note": "files",
    "notes": "files",
    "file": "files",
    "files": "files",
    "document": "files",
    "pdf": "pdf",
    "browser": "browser",
    "history": "browser",
    "website": "browser",
    "web page": "browser",
    "chat": "chat",
    "whatsapp": "chat",
    "telegram": "chat",
    "discord": "chat",
    "message": "chat",
    "code": "code",
    "repo": "code",
    "repository": "code",
    "function": "code",
}

#: "from Rahul" / "from:hr@company.com" match the `author` field (§7.1).
_AUTHOR_RE = re.compile(r"\bfrom\s*:?\s*([A-Za-z0-9._@][A-Za-z0-9._@\- ]{1,60})", re.IGNORECASE)

#: Quoted phrases pass through to FTS5 verbatim (§7.1).
_PHRASE_RE = re.compile(r'"[^"]+"')

_MONTHS = (
    "january february march april may june july august september october november december".split()
)


@dataclass(frozen=True)
class ParsedQuery:
    """A raw query split into searchable text plus structured filters."""

    text: str  # cleaned text for BM25 MATCH + vector embedding
    filters: QueryFilters
    exact_phrases: tuple[str, ...] = ()


def _parse_author(query: str) -> str | None:
    match = _AUTHOR_RE.search(query)
    if not match:
        return None
    author = match.group(1).strip().rstrip(".,;:")
    # "from last week" / "from March" is a date phrase, not a person (§7.1).
    # The regex is greedy, so also cut trailing scaffolding ("about the offer").
    author = re.split(r"\s+(about|in|on|during|for|regarding)\b", author, flags=re.IGNORECASE)[0]
    author = author.strip().rstrip(".,;:")
    lowered = author.lower()
    date_words = {"last", "past", "this", "week", "month", "year", *{m for m in _MONTHS}}
    if not author or any(w in date_words for w in lowered.split()) or re.search(
        r"\b(19|20)\d{2}\b", author
    ):
        return None
    return author


def _parse_source_kind(query: str) -> str | None:
    lowered = query.lower()
    for hint, kind in SOURCE_HINTS.items():
        if hint in lowered:
            return kind
    return None


def _month_range(year: int, month: int) -> tuple[str, str]:
    import calendar

    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, 1).date().isoformat(), datetime(
        year, month, last_day
    ).date().isoformat()


def parse_date_range(text: str, now: datetime | None = None) -> tuple[str, str] | None:
    """Detect a date phrase in *text* and return ``(date_from, date_to)`` ISO dates.

    Covers the §7.1 examples ("last week", "in March", "2025"). Returns None
    when no date phrase is found. The first matching pattern wins.
    """
    now = now or datetime.now()
    lowered = text.lower()

    if "last week" in lowered or "past week" in lowered:
        from datetime import timedelta

        start = (now - timedelta(days=7)).date().isoformat()
        return start, now.date().isoformat()
    if "last month" in lowered or "past month" in lowered:
        from datetime import timedelta

        start = (now - timedelta(days=30)).date().isoformat()
        return start, now.date().isoformat()

    # A month becomes a date filter only when it is explicitly scoped. PLAN.md §7.1
    # names "in March" and "2025" as the examples; a bare "November" or "November 3"
    # is far more often a date *mentioned in the content* (the internship note, the
    # offer email) than a request to filter by document date. Treating it as a filter
    # silently excludes every undated file, which is why "November 3" returned nothing
    # in the shipped corpus.
    month_alt = "|".join(_MONTHS)
    month_year = re.search(rf"\b(?:in\s+)?({month_alt})\s+(\d{{4}})\b", text, re.IGNORECASE)
    if month_year:
        month_index = _MONTHS.index(month_year.group(1).lower()) + 1
        return _month_range(int(month_year.group(2)), month_index)
    month_only = re.search(rf"\bin\s+({month_alt})\b", text, re.IGNORECASE)
    if month_only:
        return _month_range(now.year, _MONTHS.index(month_only.group(1).lower()) + 1)

    # Explicit ISO-ish dates: 2025-03-14, 2025-03, 2025.
    iso = re.search(r"\b((?:19|20)\d{2})(?:-(\d{2})(?:-(\d{2}))?)?\b", text)
    if iso:
        year = int(iso.group(1))
        month = int(iso.group(2)) if iso.group(2) else None
        day = int(iso.group(3)) if iso.group(3) else None
        if month and day:
            d = datetime(year, month, day).date().isoformat()
            return d, d
        if month:
            return _month_range(year, month)
        return f"{year}-01-01", f"{year}-12-31"

    # Fallback: dateparser's search for a single calendar date ("March 3rd").
    # Gated on the matched substring: dateparser treats bare words like "here"
    # as today, and an unguarded fallback would turn ordinary queries into
    # single-day filters that zero out results. Only accept matches containing
    # a digit or an explicit month/weekday name.
    try:
        from dateparser.search import search_dates

        found = search_dates(text, settings={"PREFER_DATES_FROM": "past"})
        if found:
            substr = found[0][0].lower()
            # Require the matched text itself to carry a year. A content date like
            # "November 3" (no year) must stay a text search, not become a
            # single-day filter that hides every undated document.
            if re.search(r"\b(19|20)\d{2}\b", substr):
                day = found[0][1].date().isoformat()
                return day, day
    except Exception:  # noqa: BLE001 — date parsing must never break search
        pass
    return None


def _strip_filter_phrases(query: str, author: str | None) -> str:
    """Remove filter-carrying phrases so the embedding sees searchable text."""
    text = query
    if author:
        text = re.sub(r"\bfrom\s*:?\s*" + re.escape(author), " ", text, flags=re.IGNORECASE)
    # Source-hint scaffolding ("in my notes", "in email") adds no meaning.
    text = re.sub(
        r"\b(in\s+(my\s+)?(notes?|emails?|pdfs?|code|chats?|browser(\s+history)?))",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" ,;:")
    return text.strip()


def parse_query(raw: str, now: datetime | None = None) -> ParsedQuery:
    """Split *raw* into cleaned text plus :class:`QueryFilters` (§7.1)."""
    query = (raw or "").strip()
    if not query:
        return ParsedQuery(text="", filters=QueryFilters())
    phrases = tuple(_PHRASE_RE.findall(query))
    author = _parse_author(query)
    source_kind = _parse_source_kind(query)
    date_range = parse_date_range(query, now)
    cleaned = _strip_filter_phrases(query, author)
    filters = QueryFilters(
        source_kind=source_kind,
        author=author,
        date_from=date_range[0] if date_range else None,
        date_to=date_range[1] if date_range else None,
    )
    return ParsedQuery(text=cleaned or query, filters=filters, exact_phrases=phrases)
