"""Phase 2 parser + filter tests (PLAN.md §7.1, TODO 2.1)."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.query.filters import QueryFilters, filter_clause, filters_from_dict
from core.query.parser import parse_date_range, parse_query

pytestmark = pytest.mark.unit


def test_parse_source_hints():
    assert parse_query("offer details in my notes").filters.source_kind == "files"
    assert parse_query("flight confirmation email").filters.source_kind == "email"
    assert parse_query("where is that PDF about pricing").filters.source_kind == "pdf"
    assert parse_query("who owns this function in code").filters.source_kind == "code"


def test_parse_author():
    parsed = parse_query("offer letter from hr@company.com")
    assert parsed.filters.author == "hr@company.com"


def test_parse_author_ignores_date_phrase():
    parsed = parse_query("notes from last week about the offer")
    assert parsed.filters.author is None
    assert parsed.filters.date_from is not None


def test_parse_last_week_range():
    now = datetime(2026, 3, 15)
    assert parse_date_range("emails from last week", now) == ("2026-03-08", "2026-03-15")


def test_parse_month_year():
    assert parse_date_range("notes in March 2025", datetime(2026, 1, 1)) == (
        "2025-03-01",
        "2025-03-31",
    )


def test_parse_bare_year():
    assert parse_date_range("roundup 2025", datetime(2026, 1, 1)) == (
        "2025-01-01",
        "2025-12-31",
    )


def test_parse_no_date_returns_none():
    assert parse_date_range("order ID 48213 confirmed", datetime(2026, 1, 1)) is None


def test_bare_month_is_not_a_date_filter():
    # "November" / "November 3" are dates mentioned in content (the internship
    # note, the offer email), not a request to filter by document date. Treating
    # them as a filter excludes every undated file, so they must stay text.
    assert parse_date_range("November", datetime(2026, 10, 19)) is None
    assert parse_date_range("November 3", datetime(2026, 10, 19)) is None
    assert parse_date_range("When do I start my internship?", datetime(2026, 10, 19)) is None


def test_in_month_without_year_is_a_date_filter():
    assert parse_date_range("notes in March", datetime(2026, 1, 1)) == (
        "2026-03-01",
        "2026-03-31",
    )


def test_parse_keeps_quoted_phrases():
    parsed = parse_query('"order ID 48213" in my notes')
    assert parsed.exact_phrases == ('"order ID 48213"',)
    # Scaffolding is stripped so the embedding sees searchable text.
    assert "in my notes" not in parsed.text
    assert "order ID 48213" in parsed.text


def test_parse_empty_query():
    parsed = parse_query("   ")
    assert parsed.text == "" and parsed.filters.is_empty()


def test_filter_clause_empty():
    assert filter_clause(None) == ("", [])
    assert filter_clause(QueryFilters()) == ("", [])


def test_filter_clause_combines_all_fields():
    clause, params = filter_clause(
        QueryFilters(source_kind="email", author="rahul", date_from="2025-03-01",
                     date_to="2025-03-31")
    )
    assert "sources.kind = ?" in clause
    assert "documents.author LIKE ?" in clause
    assert params == ["email", "%rahul%", "2025-03-01", "2025-03-31"]


def test_filters_merge_caller_wins():
    parsed = QueryFilters(source_kind="files", author="a")
    merged = parsed.merge(QueryFilters(author="b"))
    assert merged == QueryFilters(source_kind="files", author="b")


def test_filters_from_dict():
    assert filters_from_dict(None) == QueryFilters()
    assert filters_from_dict({"source_kind": "pdf"}) == QueryFilters(source_kind="pdf")
