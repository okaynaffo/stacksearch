"""Schema definitions (PLAN.md §5).

Two rules are enforced here rather than left to reviewer discipline:

1. **The vector dimension is never hardcoded.** §5 is explicit that ``FLOAT[768]`` is
   generated from ``meta.embedding_dim``. :func:`chunks_vec_ddl` is the only place a
   ``vec0`` table is declared, and it takes the dimension as an argument.
   ``tests/unit/test_schema.py`` greps the source tree to keep it that way.
2. **Metadata and vectors are deleted together.** §6.5.3 relies on ``ON DELETE CASCADE``
   from ``documents`` to ``chunks``; ``foreign_keys`` is switched on in
   :mod:`core.index.db` because SQLite ignores cascades otherwise.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------
# Plain tables (PLAN.md §5)
# --------------------------------------------------------------------------------

SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS sources (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,          -- files | email | browser | chat | code | pdf
  name          TEXT NOT NULL,
  config_json   TEXT NOT NULL,          -- paths, account, include/exclude globs
  enabled       INTEGER NOT NULL DEFAULT 1,
  last_sync_at  TEXT,
  cursor        TEXT                    -- connector-specific sync cursor (e.g. IMAP UIDVALIDITY:UID)
);
"""

DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS documents (
  id            INTEGER PRIMARY KEY,
  source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  external_id   TEXT NOT NULL,          -- path, Message-ID, URL+visit, thread id
  uri           TEXT NOT NULL,          -- how to open it: file://, message://, https://
  title         TEXT,
  author        TEXT,
  mime          TEXT,
  created_at    TEXT,
  modified_at   TEXT,
  content_hash  TEXT NOT NULL,          -- SHA-256 of extracted text
  status        TEXT NOT NULL DEFAULT 'indexed', -- pending | indexed | error | deleted
  error         TEXT,
  UNIQUE(source_id, external_id)
);
"""

CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS chunks (
  id            INTEGER PRIMARY KEY,
  document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord           INTEGER NOT NULL,       -- order within the document
  text          TEXT NOT NULL,
  chunk_hash    TEXT NOT NULL,          -- lets unchanged chunks keep their embeddings
  loc_json      TEXT NOT NULL,          -- {"page":3} | {"line_start":40,"line_end":72,"symbol":"parseFoo"} | {"msg_part":"body"}
  token_count   INTEGER NOT NULL
);
"""

JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,          -- scan | index_doc | delete_doc | reembed
  payload_json  TEXT NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 5,
  attempts      INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'queued',
  run_after     TEXT
);
"""

QUERY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS query_log (
  id INTEGER PRIMARY KEY,
  query TEXT,
  created_at TEXT,
  result_chunk_ids TEXT,
  clicked_chunk_id INTEGER,
  latency_ms INTEGER
);
"""

#: Embedding model id and dimension are stored together (PLAN.md §5) because a change to
#: either invalidates the vector index. Read on every open and compared against config.
META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# --------------------------------------------------------------------------------
# Keyword index (PLAN.md §5)
# --------------------------------------------------------------------------------

#: Standalone FTS5 table over chunk text + owning-document title (PLAN.md §5).
#:
#: An external-content table (``content='chunks'``) cannot work here because ``title``
#: lives in ``documents``, not ``chunks`` — ``rebuild`` would fail with
#: ``no such column: T.title``. A standalone table synced by the ``chunks_ai/ad/au``
#: triggers below avoids that: the triggers look up the title on every write.
#: `porter` gives BM25 stemming for the exact-lookup half of hybrid retrieval, and
#: the second column lets §7.2's title-match boost be computed from the index.
CHUNKS_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, title,
  tokenize='porter unicode61'
);
"""

# --------------------------------------------------------------------------------
# Vector index (PLAN.md §5)
# --------------------------------------------------------------------------------


def chunks_vec_ddl(dim: int) -> str:
    """DDL for the ``vec0`` vector table at an explicit dimension.

    SQLite cannot ``ALTER`` a ``vec0`` column width, so the dimension is baked into the
    table definition and a change means a full rebuild — see
    :func:`core.index.migrations.rebuild_vector_index`.
    """
    if not isinstance(dim, int) or dim <= 0:
        raise ValueError(f"embedding dimension must be a positive int, got {dim!r}")
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
  chunk_id INTEGER PRIMARY KEY,
  embedding FLOAT[{dim}]
);
"""


#: Sync triggers for the external-content FTS5 table (PLAN.md §5).
#:
#: Without these, writes to ``chunks`` never reach ``chunks_fts`` and BM25 silently
#: returns nothing. Each trigger mirrors one row change, looking up the owning
#: document's title so §7.2's title-match boost can be computed from the index.
#: Stored as complete statements (not split on ``;``) because trigger bodies contain
#: semicolons — see :func:`statements` note below.
FTS_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text, title)
  VALUES (new.id, new.text,
    (SELECT title FROM documents WHERE id = new.document_id));
END""",
    """
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  DELETE FROM chunks_fts WHERE rowid = old.id;
END""",
    """
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  DELETE FROM chunks_fts WHERE rowid = old.id;
  INSERT INTO chunks_fts(rowid, text, title)
  VALUES (new.id, new.text,
    (SELECT title FROM documents WHERE id = new.document_id));
END""",
)

#: Ordering matters: plain tables first so foreign keys resolve, then the FTS5 mirror,
#: then the vec0 table. Indices after everything.
INDEXES_DDL = (
    # §7.2 joins BM25 hits back to documents and filters on status, so both are indexed.
    "CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);",
    # §6.6: the queue is drained by (status, priority, id).
    "CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, priority, id);",
    "CREATE INDEX IF NOT EXISTS idx_query_log_created ON query_log(created_at);",
)

#: §14.1/§11.3: the query log is optional and user-disableable, so it ships off.
DEFAULT_META: dict[str, str] = {
    "query_log_enabled": "0",
    "secret_scrubbing_enabled": "1",
}


def statements(ddl: str) -> list[str]:
    """Split a DDL block into individual statements.

    Migrations must not use ``executescript``: sqlite3 commits any open transaction
    before running a script, which silently destroys the transaction boundary that
    §6.5.4 depends on. Every DDL block here is a sequence of simple statements with no
    semicolons inside string literals, so splitting on ``;`` is sufficient.
    """
    return [s.strip() for s in ddl.split(";") if s.strip()]