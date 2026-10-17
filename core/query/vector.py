"""Vector kNN search over `chunks_vec` (PLAN.md §7.2, TODO 2.1).

The query embedding must use the model's query prefix (`query:` for e5);
passage prefixes are for indexed chunks. Callers that embed with
`Embedder.embed_queries` get this right — see `core/query/hybrid.py`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.index import pack_embedding
from core.query.filters import QueryFilters, filter_clause


def vector_search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    limit: int = 50,
    filters: QueryFilters | None = None,
    k: int | None = None,
) -> list[dict[str, Any]]:
    """kNN top-*k* against `chunks_vec`, filtered like the BM25 path.

    *k* is the pre-filter candidate count handed to sqlite-vec; *limit* caps
    the post-filter rows returned. They differ because the metadata filters
    apply *after* the ANN lookup — a filtered-out candidate does not pull the
    next-nearest in. Callers pass the §7.2 top-50 for both so the two paths
    stay comparable for RRF.
    """
    fetch_k = k or max(limit, 50)
    extra_where, extra_params = filter_clause(filters)
    rows = conn.execute(
        f"""
        SELECT vec.chunk_id AS chunk_id,
               vec.distance AS distance,
               chunks.document_id AS document_id,
               chunks.text AS text,
               chunks.loc_json AS loc_json,
               documents.title AS title,
               documents.uri AS uri,
               documents.author AS author,
               documents.modified_at AS date,
               sources.kind AS source_kind
        FROM (
          SELECT chunk_id, distance FROM chunks_vec
          WHERE embedding MATCH ? AND k = ?
        ) AS vec
        JOIN chunks ON chunks.id = vec.chunk_id
        JOIN documents ON documents.id = chunks.document_id
        JOIN sources ON sources.id = documents.source_id
        WHERE documents.status = 'indexed'{extra_where}
        ORDER BY vec.distance
        LIMIT ?
        """,
        (pack_embedding(query_vector), fetch_k, *extra_params, limit),
    ).fetchall()
    results = []
    for i, row in enumerate(rows):
        try:
            loc = json.loads(row["loc_json"]) if row["loc_json"] else {}
        except (json.JSONDecodeError, TypeError):
            loc = {}
        text = row["text"] or ""
        snippet = (text[:197] + "…") if len(text) > 200 else text
        results.append(
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "source_kind": row["source_kind"],
                "title": row["title"],
                "author": row["author"],
                "date": row["date"],
                "snippet": snippet,
                "uri": row["uri"],
                "loc": loc,
                "score": row["distance"],
                "signals": {"vec_rank": i + 1},
            }
        )
    return results
