"""SQLite connection management (PLAN.md §5).

The index lives in a single SQLite file: metadata tables, the FTS5 keyword index, and
the sqlite-vec virtual table all share it, which is why backup and "forget source"
(PLAN.md §9) are a single-file operation.

WAL mode is mandatory, not an optimization: the UI reads while the indexer writes, and
§5 requires that search never blocks on ingestion.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: sqlite-vec is a loadable extension, so loading it must happen outside any
#: transaction. Kept as a helper so tests and the migrator share one code path.
VEC_EXTENSION_LOADED = "vec_version"


def load_vec_extension(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into an existing connection."""
    conn.enable_load_extension(True)
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the index database with the pragmas §5 relies on.

    ``isolation_level=None`` puts transaction control in the application rather than
    the implicit sqlite3 machinery, which §6.5.4 needs: one document update must be
    exactly one transaction, with no surprise intermediate commits.
    """
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        check_same_thread=False,
        uri=bool(read_only),
    )
    conn.row_factory = sqlite3.Row

    # WAL so the UI can read while the indexer writes (PLAN.md §5). Not available on a
    # read-only connection or on some network filesystems; degrade rather than fail.
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # §6.5.4 requires transactional writes; NORMAL is the documented-safe pairing with
    # WAL (durable across application crashes, which is the failure mode that matters).
    conn.execute("PRAGMA synchronous=NORMAL")
    # §2.1 budgets 0.5 GB idle / 1.5 GB indexing for "SQLite page cache + indexer".
    # Negative means KiB; 64 MB keeps us inside that line.
    conn.execute("PRAGMA cache_size=-65536")

    if not read_only:
        load_vec_extension(conn)

    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as a single transaction (PLAN.md §6.5.4).

    Rolls back on exception, so a failed document update never leaves a half-indexed
    document visible to search.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def trim_page_cache(conn: sqlite3.Connection) -> None:
    """Release SQLite's page cache back to the OS.

    Called at the §6.7 state 2 transition, where the embedder is being evicted to make
    room for llama-server. Without this the page cache stays resident and the handoff
    does not actually free memory.
    """
    conn.execute("PRAGMA shrink_memory")


def vec_version(conn: sqlite3.Connection) -> str | None:
    """sqlite-vec version, or None if the extension is not loaded."""
    try:
        return conn.execute("SELECT vec_version()").fetchone()[0]
    except sqlite3.OperationalError:
        return None
