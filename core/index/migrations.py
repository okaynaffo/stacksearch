"""Migrations and the embedding-model dimension rebuild (PLAN.md §5, TODO 0.4).

The migration list is intentionally short and explicit. Every entry is idempotent and
recorded in ``meta.schema_version``, so opening an existing index is a no-op.

The delicate part of §5 is the vector index: SQLite cannot ``ALTER`` a ``vec0`` column
width, so changing the embedding model means dropping ``chunks_vec``, recreating it at
the new width, and enqueueing a ``reembed`` job for every chunk. Skipping that step
writes vectors of the wrong width and silently corrupts ranking, which is why the model
id and dimension are stored together and checked on every open
(:func:`reconcile_embedding_model`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.config import EmbeddingModel, embedding_model
from core.index import schema

MigrationFn = Callable[[sqlite3.Connection], None]

SCHEMA_VERSION_KEY = "schema_version"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationFn


# --------------------------------------------------------------------------------
# meta helpers
# --------------------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read a meta value, or ``default`` when unset."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Upsert a meta value."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_int_meta(conn: sqlite3.Connection, key: str) -> int | None:
    raw = get_meta(conn, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def current_schema_version(conn: sqlite3.Connection) -> int:
    return get_int_meta(conn, SCHEMA_VERSION_KEY) or 0


def ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.executescript(schema.META_DDL)


def seed_meta(conn: sqlite3.Connection, model: EmbeddingModel | None = None) -> bool:
    """Write embedding model id + dimension into ``meta`` if not already present.

    PLAN.md §5 stores the model id and dimension together, and both are seeded in one
    transaction so a half-written pair is not possible.

    This never overwrites an existing value. On an established index the configured
    model is only *proposed* here; :func:`reconcile_embedding_model` is what compares
    it against what is actually on disk and decides whether a rebuild is required.
    Overwriting first would erase the evidence the rebuild decision is based on.

    Returns True if the pair was freshly written.
    """
    model = model or embedding_model()
    ensure_meta_table(conn)
    if get_meta(conn, "embedding_model") is not None:
        for key, value in schema.DEFAULT_META.items():
            if get_meta(conn, key) is None:
                set_meta(conn, key, value)
        return False

    conn.execute("BEGIN IMMEDIATE")
    try:
        set_meta(conn, "embedding_model", model.model_id)
        set_meta(conn, "embedding_dim", model.dim)
        for key, value in schema.DEFAULT_META.items():
            if get_meta(conn, key) is None:
                set_meta(conn, key, value)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return True


# --------------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------------


def _exec_ddl(conn: sqlite3.Connection, *blocks: str) -> None:
    """Execute DDL blocks one statement at a time, preserving the transaction.

    Not ``executescript``: that commits the open transaction first, which would let a
    partially-migrated index become visible (§6.5.4).
    """
    for block in blocks:
        for stmt in schema.statements(block):
            conn.execute(stmt)


def _m001_initial_schema(conn: sqlite3.Connection) -> None:
    """All tables except ``chunks_vec``.

    The vector table is created separately by :func:`ensure_vector_index` because its
    width comes from ``meta.embedding_dim``, which must be seeded before the first
    migration runs on a fresh database.
    """
    _exec_ddl(
        conn,
        schema.SOURCES_DDL,
        schema.DOCUMENTS_DDL,
        schema.CHUNKS_DDL,
        schema.CHUNKS_FTS_DDL,
        schema.JOBS_DDL,
        schema.QUERY_LOG_DDL,
        *schema.INDEXES_DDL,
    )


def _m002_fts_triggers(conn: sqlite3.Connection) -> None:
    """Standalone FTS5 table + sync triggers (PLAN.md §5, TODO 1.3).

    Phase 0 created ``chunks_fts`` as an external-content table without triggers,
    which is unworkable: ``title`` lives in ``documents``, so ``rebuild`` fails
    with ``no such column: T.title``. Recreate as a standalone table synced by
    triggers, backfilling from the existing ``chunks`` rows.
    """
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.execute(schema.CHUNKS_FTS_DDL)
    for trigger in schema.FTS_TRIGGERS:
        conn.execute(trigger)
    conn.execute(
        """
        INSERT INTO chunks_fts(rowid, text, title)
        SELECT c.id, c.text, d.title FROM chunks c
        JOIN documents d ON d.id = c.document_id
        """
    )


MIGRATIONS: list[Migration] = [
    Migration(1, "initial_schema", _m001_initial_schema),
    Migration(2, "fts_triggers", _m002_fts_triggers),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    ensure_meta_table(conn)
    current = current_schema_version(conn)

    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(conn)
            set_meta(conn, SCHEMA_VERSION_KEY, migration.version)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        current = migration.version

    return current


# --------------------------------------------------------------------------------
# vector index lifecycle (PLAN.md §5)
# --------------------------------------------------------------------------------


def vector_index_dim(conn: sqlite3.Connection) -> int | None:
    """Dimension the existing ``chunks_vec`` table was created with.

    Returns None when the table does not exist yet. Reads the actual table rather than
    trusting ``meta``, so a hand-edited database is detected rather than assumed sound.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chunks_vec'"
    ).fetchone()
    if row is None:
        return None
    # e.g. "... embedding FLOAT[768]\n);"
    ddl = row["sql"]
    marker = "FLOAT["
    if marker not in ddl:
        return None
    tail = ddl.split(marker, 1)[1]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def ensure_vector_index(conn: sqlite3.Connection) -> int:
    """Create ``chunks_vec`` at the dimension recorded in ``meta``.

    Returns the dimension in force.
    """
    dim = get_int_meta(conn, "embedding_dim")
    if dim is None:
        raise RuntimeError("meta.embedding_dim is not set; call seed_meta() first")
    conn.execute(schema.chunks_vec_ddl(dim))
    return dim


def rebuild_vector_index(conn: sqlite3.Connection, new_dim: int) -> int:
    """Drop and recreate ``chunks_vec`` at ``new_dim``, then queue a re-embed.

    §5 calls this out as a full table rebuild because SQLite cannot alter a ``vec0``
    column width. ``chunks`` and ``chunks_fts`` are untouched — their rows are still
    valid and will simply be re-embedded. The rebuild and the job enqueue share one
    transaction so the index is never left width-less.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.execute(schema.chunks_vec_ddl(new_dim))
        set_meta(conn, "embedding_dim", new_dim)

        # Every surviving chunk needs a fresh embedding at the new width (§5). Chunks
        # belonging to soft-deleted documents are skipped: they will be re-indexed from
        # source if the document comes back, and embedding them now is wasted work.
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.status != 'deleted'
            ORDER BY c.id
            """
        ).fetchall()
        conn.executemany(
            """
            INSERT INTO jobs(kind, payload_json, priority, status)
            VALUES('reembed', ?, ?, 'queued')
            """,
            [(json.dumps({"chunk_id": r["chunk_id"]}), 2) for r in rows],
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    return len(rows)


def reconcile_embedding_model(
    conn: sqlite3.Connection, model: EmbeddingModel | None = None
) -> dict[str, Any]:
    """Check the stored embedding model against config on open (PLAN.md §5).

    Returns a report describing what happened, which ``GET /status`` surfaces so a
    pending re-embed is visible in the UI rather than silently degrading ranking.
    """
    model = model or embedding_model()
    ensure_meta_table(conn)

    stored_model = get_meta(conn, "embedding_model")
    stored_dim = get_int_meta(conn, "embedding_dim")
    actual_dim = vector_index_dim(conn)

    report: dict[str, Any] = {
        "model_changed": False,
        "dim_changed": False,
        "requeued_chunks": 0,
        "stored_model": stored_model,
        "configured_model": model.model_id,
        "stored_dim": stored_dim,
        "configured_dim": model.dim,
    }

    if stored_model is None:
        # Should not normally reach here (open_index seeds first), but keep the
        # invariant intact if a caller forgets.
        seed_meta(conn, model)
        ensure_vector_index(conn)
        return report

    if stored_model != model.model_id:
        set_meta(conn, "embedding_model", model.model_id)
        report["model_changed"] = True

    # A dimension change needs the full rebuild; anything else is a no-op. Comparing
    # against the table itself rather than `meta` means a hand-edited database is
    # detected instead of assumed sound.
    if actual_dim is not None and actual_dim != model.dim:
        report["dim_changed"] = True
        report["requeued_chunks"] = rebuild_vector_index(conn, model.dim)
    elif actual_dim is None:
        ensure_vector_index(conn)

    if stored_dim != model.dim:
        set_meta(conn, "embedding_dim", model.dim)

    return report