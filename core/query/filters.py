"""Shared metadata filters for keyword and vector search (PLAN.md §7.1, §7.2, TODO 2.1).

Both retrieval paths must apply *the same* filters, so the WHERE-clause builder
lives here rather than in each path. The clause references the ``documents`` and
``sources`` aliases that both ``bm25_search`` and ``vector_search`` use in their
joins — callers must keep those alias names stable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryFilters:
    """Structured filters extracted from a raw query or supplied by the caller."""

    source_kind: str | None = None
    author: str | None = None
    date_from: str | None = None  # ISO date/datetime string, inclusive
    date_to: str | None = None  # ISO date/datetime string, inclusive

    def is_empty(self) -> bool:
        return not (self.source_kind or self.author or self.date_from or self.date_to)

    def merge(self, override: QueryFilters) -> QueryFilters:
        """Caller-supplied filters win over parsed ones, field by field."""
        return QueryFilters(
            source_kind=override.source_kind or self.source_kind,
            author=override.author or self.author,
            date_from=override.date_from or self.date_from,
            date_to=override.date_to or self.date_to,
        )


def filter_clause(filters: QueryFilters | None) -> tuple[str, list]:
    """Build ``(sql_fragment, params)`` for the shared WHERE clause.

    Returns ``("", [])`` when there is nothing to filter, so call sites can
    unconditionally append the fragment.
    """
    if filters is None or filters.is_empty():
        return "", []
    parts: list[str] = []
    params: list = []
    if filters.source_kind:
        parts.append("sources.kind = ?")
        params.append(filters.source_kind)
    if filters.author:
        parts.append("documents.author LIKE ?")
        params.append(f"%{filters.author}%")
    if filters.date_from:
        parts.append("documents.modified_at >= ?")
        params.append(filters.date_from)
    if filters.date_to:
        parts.append("documents.modified_at <= ?")
        params.append(filters.date_to)
    return " AND " + " AND ".join(parts), params


def filters_from_dict(raw: dict | None) -> QueryFilters:
    """Parse an API-supplied ``filters`` object into :class:`QueryFilters`."""
    if not raw:
        return QueryFilters()
    return QueryFilters(
        source_kind=raw.get("source_kind"),
        author=raw.get("author"),
        date_from=raw.get("date_from") or raw.get("dateFrom"),
        date_to=raw.get("date_to") or raw.get("dateTo"),
    )
