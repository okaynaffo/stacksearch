"""FTS5 BM25 keyword search with snippets (PLAN.md §7.2, TODO 1.3)."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from core.query.filters import QueryFilters, filter_clause

# Characters with special meaning in FTS5 MATCH syntax that must be quoted when
# they appear as literal text.
_FTS5_SPECIAL_RE = re.compile(r'["*^:()\-+]|\bOR\b|\bAND\b|\bNOT\b|\bNEAR\b')

#: Leading/trailing punctuation that is never a search term (e.g. the "?" in
#: "When do I start?"). FTS5 rejects it as a syntax error, so strip it before
#: matching. Without this, every natural-language question — the entire Phase 4
#: workload — fails the keyword path.
_STRIP_PUNCT_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def escape_fts5_term(term: str) -> str:
    """Quote a raw term if it contains FTS5 syntax characters."""
    if _FTS5_SPECIAL_RE.search(term):
        return '"' + term.replace('"', '""') + '"'
    return term


def build_match_query(query: str) -> str:
    """Build a MATCH expression: quoted phrases pass through, other terms ANDed.

    Quoted phrases (e.g. ``"order ID 48213"``) are kept verbatim per §7.1; the
    remaining terms are individually escaped and implicitly ANDed by joining
    with spaces (FTS5 default is AND within a match group... actually OR — so
    join with explicit AND semantics via space-separated quoted column filter).
    For Phase 1, space-joined terms use FTS5's default OR→rank-by-BM25, which is
    the standard keyword behavior; exact AND is not forced.
    """
    query = query.strip()
    if not query:
        return '""'
    phrases = re.findall(r'"[^"]+"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    terms = []
    for raw in rest.split():
        cleaned = _STRIP_PUNCT_RE.sub("", raw)
        if cleaned:
            terms.append(escape_fts5_term(cleaned))
    parts = [*phrases, *terms]
    return " ".join(parts) if parts else '""'


def bm25_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    filters: QueryFilters | None = None,
) -> list[dict[str, Any]]:
    """Run BM25 keyword search, returning §10-shaped result dicts.

    *filters* applies the shared metadata WHERE clause (§7.2: the same filters
    constrain both the keyword and vector paths).
    """
    match = build_match_query(query)
    extra_where, extra_params = filter_clause(filters)
    rows = conn.execute(
        f"""
        SELECT chunks.id AS chunk_id,
               chunks.document_id AS document_id,
               chunks.text AS text,
               chunks.loc_json AS loc_json,
               documents.title AS title,
               documents.uri AS uri,
               documents.author AS author,
               documents.modified_at AS date,
               sources.kind AS source_kind,
               bm25(chunks_fts) AS rank,
               snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 32) AS snippet
        FROM chunks_fts
        JOIN chunks ON chunks.id = chunks_fts.rowid
        JOIN documents ON documents.id = chunks.document_id
        JOIN sources ON sources.id = documents.source_id
        WHERE chunks_fts MATCH ?
          AND documents.status = 'indexed'{extra_where}
        ORDER BY rank
        LIMIT ?
        """,
        (match, *extra_params, limit),
    ).fetchall()
    results = []
    for i, row in enumerate(rows):
        try:
            loc = json.loads(row["loc_json"]) if row["loc_json"] else {}
        except (json.JSONDecodeError, TypeError):
            loc = {}
        results.append(
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "source_kind": row["source_kind"],
                "title": row["title"],
                "author": row["author"],
                "date": row["date"],
                "snippet": row["snippet"],
                "uri": row["uri"],
                "loc": loc,
                "score": row["rank"],
                "signals": {"bm25_rank": i + 1},
            }
        )
    return results
