"""Per-job execution: scan / index_doc / delete_doc / reembed (PLAN.md §6.5, §6.6).

The document pipeline (size guard → extract → chunk → embed-new-only →
atomic write) is the same one ``scripts/index_files.py`` runs synchronously;
the worker calls these functions from its background thread so search stays
responsive while ingestion proceeds.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.chunking.chat import chunk_chat
from core.chunking.code import SUFFIX_LANGUAGE, split_code
from core.chunking.email import chunk_emails
from core.chunking.prose import chunk_prose
from core.connectors.browser import BrowserConnector
from core.connectors.chat import ChatConnector
from core.connectors.code import CodeConnector
from core.connectors.email import EmailConnector
from core.connectors.email_imap import ImapEmailConnector, advance_cursor, split_external_id
from core.connectors.files import FilesConnector
from core.connectors.pdf import PdfConnector, resolve_fs_path
from core.extract.browser import read_history
from core.extract.chat import parse_chat_export
from core.extract.email import parse_email_bytes, parse_email_container
from core.extract.normalize import is_binary, normalize_text
from core.extract.pdf import extract_pdf
from core.extract.text import extract_by_suffix
from core.index import upsert_vector
from core.index.writer import (
    ChunkWrite,
    content_hash_for,
    fetch_existing_chunks,
    find_document_id,
    index_document,
    mark_deleted,
    new_chunk_indices,
    record_error,
)
from core.jobs import queue as job_queue

MAX_FILE_BYTES = 50 * 1024 * 1024


def _source_row(conn: sqlite3.Connection, source_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, kind, config_json, cursor FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "config": json.loads(row["config_json"]),
        "cursor": row["cursor"],
    }


def _connector_for(kind: str):
    if kind == "pdf":
        return PdfConnector()
    if kind == "code":
        return CodeConnector()
    if kind == "chat":
        return ChatConnector()
    if kind == "browser":
        return BrowserConnector()
    if kind == "email":
        return EmailConnector()
    if kind == "email_imap":
        return ImapEmailConnector()
    return FilesConnector()


def _email_chunks(messages) -> list[ChunkWrite]:
    return [
        ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
        for c in chunk_emails(messages)
    ]


def _build_chunks(
    kind: str, ref_external_id: str, title: str, fs_path: Path
) -> tuple[str, list[ChunkWrite], str | None, dict]:
    """Extract + chunk one item. Returns ``(text, chunks, mime, meta)``.

    ``meta`` carries per-kind index state the worker must persist — currently
    only the browser last-visit cursor (``{"cursor": <ISO date>}``).
    """
    if kind == "pdf":
        result = extract_pdf(fs_path)
        header = f"[pdf] {title}"
        chunks: list[ChunkWrite] = []
        for page in result.pages:
            for pc in chunk_prose(page.text, header=header, loc_extra={"page": page.page}):
                chunks.append(
                    ChunkWrite(text=pc.text, loc_json=pc.loc_json, token_count=pc.token_count)
                )
        if not chunks:
            raise _Skip(f"no extractable text (skipped pages: {result.skipped_pages})")
        return "\n\n".join(p.text for p in result.pages), chunks, "application/pdf", {}
    if kind == "code":
        data = fs_path.read_bytes()
        if is_binary(data[:8192]):
            raise _Skip("binary file")
        text = normalize_text(data)
        language = SUFFIX_LANGUAGE.get(fs_path.suffix.lower())
        header = f"[code] {title}"
        chunks = [
            ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
            for c in split_code(text, header=header, language=language)
        ]
        if not chunks:
            raise _Skip("no chunks produced")
        return text, chunks, None, {}
    if kind == "chat":
        try:
            conversation, messages = parse_chat_export(fs_path)
        except ValueError as exc:
            raise _Skip(str(exc)) from exc
        header = f"[chat] {conversation}"
        chunks = [
            ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
            for c in chunk_chat(messages, header=header)
        ]
        if not chunks:
            raise _Skip("no parseable messages")
        text = "\n".join(f"{m.sender}: {m.text}" for m in messages)
        return text, chunks, None, {}
    if kind == "browser":
        try:
            entries = read_history(fs_path)
        except ValueError as exc:
            raise _Skip(str(exc)) from exc
        if not entries:
            raise _Skip("no history entries")
        chunks = []
        texts: list[str] = []
        for i, e in enumerate(entries):
            label = e.title or e.url
            header = f"[browser] {label}"
            body = f"{label}\n{e.url}"
            chunks.append(
                ChunkWrite(
                    text=f"{header}\n\n{body}",
                    loc_json=json.dumps(
                        {
                            "url": e.url,
                            "last_visit": e.last_visit,
                            "visit_count": e.visit_count,
                            "chunk": i,
                        },
                        sort_keys=True,
                    ),
                    token_count=len(f"{header}\n\n{body}") // 4 or 1,
                )
            )
            texts.append(body)
        dated = [e.last_visit for e in entries if e.last_visit]
        meta = {"cursor": max(dated)} if dated else {}
        return "\n\n".join(texts), chunks, "text/x-browser-history", meta
    if kind == "email":
        try:
            messages = parse_email_container(fs_path)
        except ValueError as exc:
            raise _Skip(str(exc)) from exc
        if not messages:
            raise _Skip("no messages")
        chunks = _email_chunks(messages)
        text = "\n\n".join(m.body for m in messages)
        if not text.strip():
            raise _Skip("no extractable message bodies")
        # Single-message containers carry their own author/date; multi-message
        # ones (mbox, Maildir) leave document-level metadata empty.
        meta = {}
        if len(messages) == 1:
            meta = {"author": messages[0].sender, "modified_at": messages[0].date}
        return text, chunks, "message/rfc822", meta
    data = fs_path.read_bytes()
    if is_binary(data[:8192]):
        raise _Skip("binary file")
    text = extract_by_suffix(fs_path)
    header = f"[note] {title}"
    chunks = [
        ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
        for c in chunk_prose(text, header=header)
    ]
    if not chunks:
        raise _Skip("no chunks produced")
    return text, chunks, None, {}


class _Skip(Exception):
    """A document that should be recorded with a reason, not retried."""


def _build_chunks_from_raw(
    kind: str, external_id: str, title: str, data: bytes
) -> tuple[str, list[ChunkWrite], str | None, dict]:
    """Extract + chunk an item fetched as raw bytes (network connectors).

    Only IMAP email uses this today; it parses one RFC822 message.
    """
    if kind != "email_imap":
        raise _Skip(f"raw fetch unsupported for {kind}")
    parsed = parse_email_bytes(data)
    if not parsed.body.strip():
        raise _Skip("no extractable message body")
    return parsed.body, _email_chunks([parsed]), "message/rfc822", {}


def run_index_doc(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
    *,
    embed: bool = True,
) -> dict[str, Any]:
    """Index one document by source/external id. Returns the writer report."""
    from core.index.embed_helpers import embed_chunk_texts

    src = _source_row(conn, source_id)
    if src is None:
        return {"ok": False, "error": f"unknown source {source_id}"}
    kind = src["kind"]
    cursor = src.get("cursor")
    config = {**src["config"], "_cursor": cursor}
    connector = _connector_for(kind)

    raw = connector.fetch(config, external_id) if hasattr(connector, "fetch") else None

    if raw is not None:
        # Network item: content arrives as bytes; no filesystem path exists.
        uri = raw.uri or external_id
        title = raw.title or external_id
        author = raw.author
        modified_at = raw.modified_at

        def _skip_raw(reason: str) -> dict[str, Any]:
            record_error(conn, source_id=source_id, external_id=external_id, uri=uri, error=reason)
            return {"ok": False, "skipped": True, "error": reason}

        try:
            text, chunks, mime, meta = _build_chunks_from_raw(kind, external_id, title, raw.data)
        except _Skip as exc:
            return _skip_raw(str(exc))
    else:
        fs_path = resolve_fs_path(external_id)
        # Title: relative path after the :: separator.
        _, _, rel = external_id.partition("::")
        title = rel or external_id
        author = None
        modified_at = None
        meta = {}

        def _skip(reason: str) -> dict[str, Any]:
            # Best-effort URI: the connector knows how to open it.
            try:
                skip_uri = connector.open_uri(external_id)
            except Exception:  # noqa: BLE001
                skip_uri = external_id
            if not skip_uri.startswith(("file://", "message://", "https://")):
                skip_uri = fs_path.as_uri() if fs_path.exists() else external_id
            record_error(
                conn, source_id=source_id, external_id=external_id, uri=skip_uri, error=reason
            )
            return {"ok": False, "skipped": True, "error": reason}

        try:
            # Email containers may be a Maildir directory, not a single file.
            if kind == "email":
                if not (fs_path.is_file() or fs_path.is_dir()):
                    return _skip("missing file")
            elif not fs_path.is_file():
                return _skip("missing file")
            if fs_path.is_file() and fs_path.stat().st_size > MAX_FILE_BYTES:
                return _skip(f"exceeds 50MB: {fs_path.stat().st_size}")
            text, chunks, mime, meta = _build_chunks(kind, external_id, title, fs_path)
        except _Skip as exc:
            return _skip(str(exc))
        if kind == "email":
            author = meta.get("author")
            modified_at = meta.get("modified_at")
        uri = fs_path.as_uri()

    if kind == "browser" and meta.get("cursor"):
        # Last-visit cursor: the next scan skips entries at or below it (§6.2).
        conn.execute(
            "UPDATE sources SET cursor = ? WHERE id = ?",
            (meta["cursor"], source_id),
        )
    elif kind == "email_imap" and raw is not None and raw.meta:
        # UIDVALIDITY + highest UID per folder (§6.2).
        _, folder, uid = split_external_id(external_id)
        new_cursor = advance_cursor(
            cursor, folder, uid, int(raw.meta.get("uidvalidity", 0))
        )
        conn.execute("UPDATE sources SET cursor = ? WHERE id = ?", (new_cursor, source_id))

    # Content-hash short-circuit before paying for embeddings (§6.5.1).
    doc_id = find_document_id(conn, source_id, external_id)
    if doc_id is not None:
        row = conn.execute("SELECT content_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is not None and row["content_hash"] == content_hash_for(text):
            report = index_document(
                conn,
                source_id=source_id,
                external_id=external_id,
                uri=uri,
                title=title,
                text=text,
                chunks=chunks,
                mime=mime,
                embeddings=None,
                author=author,
                modified_at=modified_at,
            )
            return {"ok": True, **report}

    embeddings: list[bytes | None] | None = None
    if embed:
        if doc_id is not None:
            need = set(new_chunk_indices(fetch_existing_chunks(conn, doc_id), chunks))
        else:
            need = set(range(len(chunks)))
        if need:
            ordered = sorted(need)
            blobs = embed_chunk_texts([chunks[i].text for i in ordered])
            embeddings = [None] * len(chunks)
            for i, blob in zip(ordered, blobs, strict=True):
                embeddings[i] = blob
        else:
            embeddings = [None] * len(chunks)

    report = index_document(
        conn,
        source_id=source_id,
        external_id=external_id,
        uri=uri,
        title=title,
        text=text,
        chunks=chunks,
        mime=mime,
        embeddings=embeddings,
        author=author,
        modified_at=modified_at,
    )
    return {"ok": True, **report}


def run_delete_doc(conn: sqlite3.Connection, source_id: int, external_id: str) -> dict[str, Any]:
    """Remove a deleted document's chunks from FTS5 + vectors (§6.5.3)."""
    removed = mark_deleted(conn, source_id, external_id)
    return {"ok": True, "removed": removed}


def run_scan(
    conn: sqlite3.Connection, source_id: int, *, child_priority: int = 5
) -> dict[str, Any]:
    """Reconcile one source: enqueue index_doc for present files, delete_doc for gone ones."""
    from core.config import get_settings as _gs

    queue_cfg = _gs().queue
    src = _source_row(conn, source_id)
    if src is None:
        return {"ok": False, "error": f"unknown source {source_id}"}
    connector = _connector_for(src["kind"])
    seen: set[str] = set()
    enqueued = 0
    for ref in connector.scan({**src["config"], "_cursor": src.get("cursor")}):
        seen.add(ref.external_id)
        job_queue.enqueue(
            conn,
            "index_doc",
            {"source_id": source_id, "external_id": ref.external_id},
            priority=child_priority,
        )
        enqueued += 1
    # Anything in the DB that the scan no longer sees was deleted (§6.5.3).
    live = {
        r["external_id"]
        for r in conn.execute(
            "SELECT external_id FROM documents WHERE source_id = ? AND status != 'deleted'",
            (source_id,),
        ).fetchall()
    }
    deleted = 0
    for ext in live - seen:
        job_queue.enqueue(
            conn,
            "delete_doc",
            {"source_id": source_id, "external_id": ext},
            priority=queue_cfg.priority_live_change,
        )
        deleted += 1
    conn.execute("UPDATE sources SET last_sync_at = datetime('now') WHERE id = ?", (source_id,))
    return {"ok": True, "enqueued": enqueued, "deleted": deleted}


def schedule_reconcile(conn: sqlite3.Connection, *, max_age_h: float = 24.0) -> list[int]:
    """Enqueue low-priority scans for sources not synced within ``max_age_h`` (§6.5.5).

    The daily reconcile catches events missed while the app was closed. All
    file-backed kinds (files, pdf, code, chat, browser, email) are scheduled;
    browser and IMAP email scans additionally consult ``sources.cursor`` at
    index time (§6.2).
    """
    from core.config import get_settings as _gs

    queue_cfg = _gs().queue
    rows = conn.execute(
        """
        SELECT id FROM sources
        WHERE enabled != 0
          AND kind IN ('files', 'pdf', 'code', 'chat', 'browser', 'email', 'email_imap')
          AND (last_sync_at IS NULL
                OR last_sync_at < datetime('now', '-' || ? || ' hours'))
        """,
        (max_age_h,),
    ).fetchall()
    scheduled: list[int] = []
    for r in rows:
        # One pending scan per source is enough; skip if already queued/running.
        pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'scan'"
            " AND status IN ('queued', 'running')"
            " AND json_extract(payload_json, '$.source_id') = ?",
            (r["id"],),
        ).fetchone()[0]
        if pending:
            continue
        job_queue.enqueue(
            conn,
            "scan",
            {"source_id": r["id"], "child_priority": queue_cfg.priority_bulk},
            priority=queue_cfg.priority_reconcile,
        )
        scheduled.append(r["id"])
    return scheduled


def run_reembed(conn: sqlite3.Connection, chunk_id: int) -> dict[str, Any]:
    """Re-embed one chunk (dimension-change rebuild path, PLAN.md §5)."""
    from core.index.embed_helpers import embed_chunk_texts

    row = conn.execute("SELECT text FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"unknown chunk {chunk_id}"}
    blobs = embed_chunk_texts([row["text"]])
    conn.execute("BEGIN IMMEDIATE")
    try:
        upsert_vector(conn, chunk_id, _unpack(blobs[0]))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return {"ok": True, "chunk_id": chunk_id}


def _unpack(blob: bytes):
    from core.index import unpack_embedding

    return unpack_embedding(blob)
