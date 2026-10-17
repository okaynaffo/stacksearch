"""Atomic per-document index writes (PLAN.md §6.5, TODO 1.1/1.3/2.1).

Each document update is exactly one SQLite transaction (§6.5.4), so search never
sees a half-indexed document. FTS5 sync is handled by the ``chunks_ai/ad/au``
triggers (migration 2); vectors are written explicitly via ``upsert_vector``
because ``vec0`` has no trigger support.

Phase 2 embeds synchronously: callers compute passage embeddings *before*
calling :func:`index_document` (ONNX inference must never hold the write lock)
and pass them as ``embeddings`` aligned 1:1 with ``chunks``. Phase 3 will move
the embedding call into a background job without changing this signature.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkWrite:
    """One chunk to store."""

    text: str
    loc_json: str
    token_count: int

    @property
    def chunk_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def content_hash_for(text: str) -> str:
    """SHA-256 of extracted document text (PLAN.md §5, §6.5)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_existing_chunks(conn: sqlite3.Connection, doc_id: int) -> list[tuple[int, str]]:
    """Return ``(chunk_id, chunk_hash)`` rows for a document, in ``ord`` order.

    Read-only helper for the Phase 3 worker (PLAN.md §6.5.2): the worker calls
    this *before* embedding to learn which hashes are already stored, embeds
    only the new ones, then hands the full chunk list to :func:`index_document`.
    """
    return [
        (r["id"], r["chunk_hash"])
        for r in conn.execute(
            "SELECT id, chunk_hash FROM chunks WHERE document_id = ? ORDER BY ord",
            (doc_id,),
        ).fetchall()
    ]


def find_document_id(conn: sqlite3.Connection, source_id: int, external_id: str) -> int | None:
    """Return the document id for a source/external pair, or None if unseen."""
    row = conn.execute(
        "SELECT id FROM documents WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    return row["id"] if row is not None else None


def new_chunk_indices(existing: list[tuple[int, str]], chunks: list[ChunkWrite]) -> list[int]:
    """Return indices of *chunks* whose hash is not already stored.

    Greedy multiset match: each existing row covers one occurrence, so a
    document with two identical paragraphs that loses one still reuses one
    row and embeds nothing new.
    """
    available: dict[str, int] = {}
    for _, h in existing:
        available[h] = available.get(h, 0) + 1
    new_idx: list[int] = []
    for i, ch in enumerate(chunks):
        h = ch.chunk_hash
        if available.get(h, 0) > 0:
            available[h] -= 1
        else:
            new_idx.append(i)
    return new_idx


def index_document(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    external_id: str,
    uri: str,
    title: str | None,
    text: str,
    chunks: list[ChunkWrite],
    author: str | None = None,
    mime: str | None = None,
    modified_at: str | None = None,
    embeddings: list[bytes] | None = None,
) -> dict:
    """Insert or update one document and its chunks atomically.

    - Same ``content_hash`` → update ``modified_at`` only, skip chunk work (§6.5.1).
    - Changed → diff by ``chunk_hash`` (§6.5.2): unchanged chunks keep their rows
      and their ``chunks_vec`` embeddings; only new hashes are inserted and
      embedded. Removed hashes are deleted from both tables.
    - ``embeddings`` (optional): pre-computed ``vec0`` BLOBs aligned 1:1 with
      ``chunks``. Entries at reused positions are ignored (the stored vector is
      kept), so the worker may pass ``None`` there after a
      :func:`new_chunk_indices` pre-pass. Entries at new positions must be
      non-None when ``embeddings`` is given. Pass None to skip vector writes
      (e.g. tests that only need BM25).
    - Returns ``{"document_id": int, "changed": bool, "chunks": int,
      "reused": int, "embedded": int}``.
    """
    if embeddings is not None and len(embeddings) != len(chunks):
        raise ValueError(
            f"embeddings ({len(embeddings)}) must align 1:1 with chunks ({len(chunks)})"
        )
    content_hash = content_hash_for(text)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id, content_hash FROM documents WHERE source_id = ? AND external_id = ?",
            (source_id, external_id),
        ).fetchone()
        if row is not None and row["content_hash"] == content_hash:
            if modified_at is not None:
                conn.execute(
                    "UPDATE documents SET modified_at = ? WHERE id = ?",
                    (modified_at, row["id"]),
                )
            conn.execute("COMMIT")
            return {
                "document_id": row["id"],
                "changed": False,
                "chunks": len(chunks),
                "reused": len(chunks),
                "embedded": 0,
            }

        if row is None:
            conn.execute(
                """INSERT INTO documents
                   (source_id, external_id, uri, title, author, mime, modified_at,
                    content_hash, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'indexed')""",
                (source_id, external_id, uri, title, author, mime, modified_at, content_hash),
            )
            doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            doc_id = row["id"]
            conn.execute(
                """UPDATE documents SET uri = ?, title = ?, author = ?, mime = ?,
                       modified_at = ?, content_hash = ?, status = 'indexed', error = NULL
                   WHERE id = ?""",
                (uri, title, author, mime, modified_at, content_hash, doc_id),
            )
            reused, embedded = _replace_chunks_diff(conn, doc_id, chunks, embeddings)
            conn.execute("COMMIT")
            return {
                "document_id": doc_id,
                "changed": True,
                "chunks": len(chunks),
                "reused": reused,
                "embedded": embedded,
            }

        for ord_, ch in enumerate(chunks):
            conn.execute(
                """INSERT INTO chunks
                   (document_id, ord, text, chunk_hash, loc_json, token_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, ord_, ch.text, ch.chunk_hash, ch.loc_json, ch.token_count),
            )
            chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            if embeddings is not None:
                blob = embeddings[ord_]
                if blob is None:
                    raise ValueError(
                        f"embeddings[{ord_}] is None for a new chunk; "
                        "reused positions may be None, new ones must carry a BLOB"
                    )
                conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,))
                conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, blob),
                )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    n_embedded = sum(1 for b in embeddings if b is not None) if embeddings is not None else 0
    return {
        "document_id": doc_id,
        "changed": True,
        "chunks": len(chunks),
        "reused": 0,
        "embedded": n_embedded,
    }


def _replace_chunks_diff(
    conn: sqlite3.Connection,
    doc_id: int,
    chunks: list[ChunkWrite],
    embeddings: list[bytes | None] | None,
) -> tuple[int, int]:
    """Diff new chunks against stored rows; returns ``(reused, embedded)``.

    Must be called inside the caller's write transaction. Reused rows are
    updated in place (fresh ``ord``/text, same ``chunk_id`` so the stored
    vector stays valid); removed rows are deleted from ``chunks`` (FTS5 via
    trigger) and ``chunks_vec`` explicitly; new rows are inserted with their
    embedding BLOB.
    """
    existing = fetch_existing_chunks(conn, doc_id)
    # hash -> queue of chunk ids holding it.
    by_hash: dict[str, list[int]] = {}
    for cid, h in existing:
        by_hash.setdefault(h, []).append(cid)

    assignment: list[int | None] = [None] * len(chunks)
    for i, ch in enumerate(chunks):
        queue = by_hash.get(ch.chunk_hash)
        if queue:
            assignment[i] = queue.pop(0)

    reused_ids = {cid for cid in assignment if cid is not None}
    all_ids = {cid for cid, _ in existing}
    removed_ids = all_ids - reused_ids
    if removed_ids:
        conn.executemany(
            "DELETE FROM chunks_vec WHERE chunk_id = ?",
            [(cid,) for cid in removed_ids],
        )
        conn.executemany("DELETE FROM chunks WHERE id = ?", [(cid,) for cid in removed_ids])

    reused = 0
    embedded = 0
    for ord_, (ch, cid) in enumerate(zip(chunks, assignment, strict=True)):
        if cid is not None:
            conn.execute(
                """UPDATE chunks SET ord = ?, text = ?, chunk_hash = ?,
                          loc_json = ?, token_count = ? WHERE id = ?""",
                (ord_, ch.text, ch.chunk_hash, ch.loc_json, ch.token_count, cid),
            )
            reused += 1
        else:
            conn.execute(
                """INSERT INTO chunks
                   (document_id, ord, text, chunk_hash, loc_json, token_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, ord_, ch.text, ch.chunk_hash, ch.loc_json, ch.token_count),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            if embeddings is not None:
                blob = embeddings[ord_]
                if blob is None:
                    raise ValueError(
                        f"embeddings[{ord_}] is None for a new chunk; "
                        "reused positions may be None, new ones must carry a BLOB"
                    )
                conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                    (new_id, blob),
                )
                embedded += 1
    return reused, embedded


def _delete_chunk_vectors(conn: sqlite3.Connection, doc_id: int) -> None:
    """Remove `chunks_vec` rows for a document's chunks (§6.5.3).

    Must run *before* the `DELETE FROM chunks`, while the chunk ids are still
    known. `vec0` has no cascade support, so orphan rows would otherwise linger
    and surface as ghost hits in vector search.
    """
    ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,)).fetchall()
    ]
    if ids:
        conn.executemany("DELETE FROM chunks_vec WHERE chunk_id = ?", [(i,) for i in ids])


def mark_deleted(conn: sqlite3.Connection, source_id: int, external_id: str) -> bool:
    """Soft-delete a document and cascade-remove its chunks (§6.5.3).

    Chunk rows are deleted outright (triggers remove them from FTS5); vectors are
    removed explicitly (vec0 has no cascade); the document row is kept with
    ``status='deleted'`` so a later re-scan can distinguish "deleted" from
    "never seen". Returns True if a document was affected.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM documents WHERE source_id = ? AND external_id = ?",
            (source_id, external_id),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return False
        _delete_chunk_vectors(conn, row["id"])
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (row["id"],))
        conn.execute("UPDATE documents SET status = 'deleted' WHERE id = ?", (row["id"],))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return True


def record_error(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    external_id: str,
    uri: str,
    error: str,
) -> int:
    """Record a skipped/failed document with a reason (PLAN.md §6.3)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM documents WHERE source_id = ? AND external_id = ?",
            (source_id, external_id),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO documents
                   (source_id, external_id, uri, content_hash, status, error)
                   VALUES (?, ?, ?, '', 'error', ?)""",
                (source_id, external_id, uri, error),
            )
            doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            doc_id = row["id"]
            conn.execute(
                "UPDATE documents SET status = 'error', error = ? WHERE id = ?",
                (error, doc_id),
            )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return doc_id
