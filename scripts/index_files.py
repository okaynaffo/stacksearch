#!/usr/bin/env python3
"""Index files/notes/PDFs into the local index (PLAN.md §6, TODO 1.1).

Usage:
    uv run python scripts/index_files.py --path ~/notes --name notes
    uv run python scripts/index_files.py --path ~/docs --name docs --kind pdf
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.chunking.prose import chunk_prose  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.connectors.files import FilesConnector  # noqa: E402
from core.connectors.pdf import PdfConnector, resolve_fs_path  # noqa: E402
from core.extract.normalize import is_binary  # noqa: E402
from core.extract.pdf import extract_pdf  # noqa: E402
from core.extract.text import extract_by_suffix  # noqa: E402
from core.index import open_index  # noqa: E402
from core.index.embed_helpers import embed_chunk_texts  # noqa: E402
from core.index.writer import ChunkWrite, index_document, record_error  # noqa: E402

MAX_FILE_BYTES = 50 * 1024 * 1024


def _ensure_source(conn, kind: str, name: str, config: dict) -> int:
    row = conn.execute(
        "SELECT id FROM sources WHERE kind = ? AND name = ?", (kind, name)
    ).fetchone()
    if row is not None:
        return row["id"]
    conn.execute(
        "INSERT INTO sources(kind, name, config_json) VALUES (?, ?, ?)",
        (kind, name, json.dumps(config)),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Index a folder of files/PDFs")
    parser.add_argument("--path", required=True, help="folder to index")
    parser.add_argument("--name", required=True, help="source name")
    parser.add_argument("--kind", default="files", choices=["files", "pdf"])
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="skip embeddings (run scripts/backfill_vectors.py afterwards)",
    )
    args = parser.parse_args()

    settings = get_settings()
    connector = PdfConnector() if args.kind == "pdf" else FilesConnector()
    config = {"paths": [args.path]}

    indexed = skipped = 0
    with open_index(settings.index_db_path) as ix:
        conn = ix.conn
        source_id = _ensure_source(conn, args.kind, args.name, config)
        for ref in connector.scan(config):
            fs_path = resolve_fs_path(ref.external_id)
            try:
                if not fs_path.is_file():
                    record_error(
                        conn,
                        source_id=source_id,
                        external_id=ref.external_id,
                        uri=ref.uri,
                        error="missing file",
                    )
                    skipped += 1
                    continue
                if fs_path.stat().st_size > MAX_FILE_BYTES:
                    record_error(
                        conn,
                        source_id=source_id,
                        external_id=ref.external_id,
                        uri=ref.uri,
                        error=f"exceeds 50MB: {fs_path.stat().st_size}",
                    )
                    skipped += 1
                    continue
                if args.kind == "pdf":
                    result = extract_pdf(fs_path)
                    header = f"[pdf] {ref.title}"
                    chunks: list[ChunkWrite] = []
                    for page in result.pages:
                        for pc in chunk_prose(
                            page.text, header=header, loc_extra={"page": page.page}
                        ):
                            chunks.append(
                                ChunkWrite(
                                    text=pc.text, loc_json=pc.loc_json, token_count=pc.token_count
                                )
                            )
                    if not chunks:
                        record_error(
                            conn,
                            source_id=source_id,
                            external_id=ref.external_id,
                            uri=ref.uri,
                            error=f"no extractable text (skipped pages: {result.skipped_pages})",
                        )
                        skipped += 1
                        continue
                    text = "\n\n".join(p.text for p in result.pages)
                    embeddings = (
                        None
                        if args.no_embed
                        else embed_chunk_texts([c.text for c in chunks])
                    )
                    res = index_document(
                        conn,
                        source_id=source_id,
                        external_id=ref.external_id,
                        uri=ref.uri,
                        title=ref.title,
                        text=text,
                        chunks=chunks,
                        mime="application/pdf",
                        embeddings=embeddings,
                    )
                else:
                    if is_binary(fs_path.read_bytes()[:8192]):
                        record_error(
                            conn,
                            source_id=source_id,
                            external_id=ref.external_id,
                            uri=ref.uri,
                            error="binary file",
                        )
                        skipped += 1
                        continue
                    text = extract_by_suffix(fs_path)
                    header = f"[note] {ref.title}"
                    chunks = [
                        ChunkWrite(text=c.text, loc_json=c.loc_json, token_count=c.token_count)
                        for c in chunk_prose(text, header=header)
                    ]
                    if not chunks:
                        skipped += 1
                        continue
                    embeddings = (
                        None
                        if args.no_embed
                        else embed_chunk_texts([c.text for c in chunks])
                    )
                    res = index_document(
                        conn,
                        source_id=source_id,
                        external_id=ref.external_id,
                        uri=ref.uri,
                        title=ref.title,
                        text=text,
                        chunks=chunks,
                        embeddings=embeddings,
                    )
                indexed += 1 if res["changed"] else 0
            except Exception as exc:  # noqa: BLE001
                record_error(
                    conn,
                    source_id=source_id,
                    external_id=ref.external_id,
                    uri=ref.uri,
                    error=f"{type(exc).__name__}: {exc}",
                )
                skipped += 1
    print(f"indexed={indexed} skipped={skipped} source={args.name} kind={args.kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
