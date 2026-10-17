"""Index lifecycle: open an index, migrate it, keep the vector width honest (PLAN.md §5)."""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import EmbeddingModel, embedding_model
from core.index import migrations, schema
from core.index.db import connect, transaction, vec_version
from core.index.migrations import (
    get_int_meta,
    get_meta,
    reconcile_embedding_model,
    seed_meta,
    set_meta,
)


@dataclass
class Index:
    """An open index database."""

    conn: sqlite3.Connection
    path: Path
    embedding_dim: int
    reconcile_report: dict[str, Any]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_index(
    path: Path | str,
    *,
    model: EmbeddingModel | None = None,
    read_only: bool = False,
) -> Index:
    """Open (creating if needed) the index at ``path``.

    Order matters: seed ``meta`` with the embedding model before migrating, because
    the vector table's width is generated from ``meta.embedding_dim`` (§5).
    """
    model = model or embedding_model()
    conn = connect(path, read_only=read_only)

    if not read_only:
        migrations.migrate(conn)
        seed_meta(conn, model)
        report = reconcile_embedding_model(conn, model)
        # reconcile is the authority on what actually happened to the vector table
        # (including a rebuild), so the configured dimension is what is in force now.
        dim = model.dim
    else:
        report = {}
        dim = get_int_meta(conn, "embedding_dim") or model.dim

    return Index(conn=conn, path=Path(path), embedding_dim=dim, reconcile_report=report)


# --------------------------------------------------------------------------------
# vector serialization
# --------------------------------------------------------------------------------


def pack_embedding(values) -> bytes:
    """Serialize a float sequence as the little-endian float32 BLOB ``vec0`` expects.

    Packing here rather than calling ``vec_f32()`` in SQL avoids building a 768-element
    float string for every query, which matters in the indexing hot path (§6.6).
    """
    return struct.pack(f"{len(values)}f", *values)


def unpack_embedding(blob: bytes) -> tuple[float, ...]:
    """Inverse of :func:`pack_embedding`."""
    return struct.unpack(f"{len(blob) // 4}f", blob)


def upsert_vector(conn: sqlite3.Connection, chunk_id: int, values) -> None:
    """Write (or replace) one chunk's embedding."""
    conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
        (chunk_id, pack_embedding(values)),
    )


def delete_vectors(conn: sqlite3.Connection, chunk_ids) -> None:
    """Remove vectors for the given chunk ids (§6.5.3)."""
    conn.executemany("DELETE FROM chunks_vec WHERE chunk_id = ?", [(c,) for c in chunk_ids])


# --------------------------------------------------------------------------------
# status helpers for GET /status (PLAN.md §10)
# --------------------------------------------------------------------------------


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Index statistics for the status endpoint."""
    counts = {
        "sources": "SELECT COUNT(*) FROM sources",
        "documents": "SELECT COUNT(*) FROM documents",
        "chunks": "SELECT COUNT(*) FROM chunks",
        "vectors": "SELECT COUNT(*) FROM chunks_vec",
        "jobs_pending": "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')",
    }
    out: dict[str, Any] = {}
    for key, sql in counts.items():
        try:
            out[key] = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            # chunks_vec is absent before the first migration on a brand new file.
            out[key] = None

    out["documents_by_status"] = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
        ).fetchall()
    }
    out["embedding_model"] = get_meta(conn, "embedding_model")
    out["embedding_dim"] = get_int_meta(conn, "embedding_dim")
    out["schema_version"] = get_int_meta(conn, "schema_version")
    out["vec_version"] = vec_version(conn)
    return out


__all__ = [
    "Index",
    "open_index",
    "pack_embedding",
    "unpack_embedding",
    "upsert_vector",
    "delete_vectors",
    "stats",
    "transaction",
    "set_meta",
    "get_meta",
    "schema",
    "migrations",
]