#!/usr/bin/env python3
"""Backfill embeddings for chunks missing vectors (PLAN.md §5, TODO 2.1).

Phase 1 indexes have zero `chunks_vec` rows: the table exists but nothing wrote
to it. This script embeds every chunk without a vector in batches of 32 (§6.6),
prunes orphan vectors whose chunk no longer exists, and is resumable — chunks
that already have vectors are skipped.

Usage:
    uv run python scripts/backfill_vectors.py
    STACKSEARCH_DATA_DIR=/tmp/idx uv run python scripts/backfill_vectors.py --batch 32
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config import get_settings  # noqa: E402
from core.index import open_index  # noqa: E402
from core.index.db import vec_version  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing chunk embeddings")
    parser.add_argument("--batch", type=int, default=32, help="embed batch size (§6.6)")
    args = parser.parse_args()

    settings = get_settings()
    # Honor --batch without a config change: the embedder reads this setting.
    settings.indexing = settings.indexing.__class__(
        **{**settings.indexing.__dict__, "embed_batch_size": args.batch}
    )

    from core.models.embedder import get_embedder

    embedder = get_embedder()
    # Fail fast on dimension mismatch rather than writing wrong-width vectors (§5).
    expected_dim = embedder.dim

    with open_index(settings.index_db_path) as ix:
        conn = ix.conn
        if vec_version(conn) is None:
            print("FAIL: sqlite-vec extension not loaded; cannot backfill")
            return 1

        # Prune orphan vectors left by Phase 1 deletes (vec0 has no cascade).
        orphan = conn.execute(
            "SELECT chunk_id FROM chunks_vec WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        ).fetchall()
        if orphan:
            conn.executemany(
                "DELETE FROM chunks_vec WHERE chunk_id = ?", [(r["chunk_id"],) for r in orphan]
            )
            print(f"pruned={len(orphan)} orphan vectors")

        missing = conn.execute(
            """
            SELECT c.id AS chunk_id, c.text AS text
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            LEFT JOIN chunks_vec v ON v.chunk_id = c.id
            WHERE v.chunk_id IS NULL AND d.status != 'deleted'
            ORDER BY c.id
            """
        ).fetchall()
        total = len(missing)
        if total == 0:
            print("backfill: nothing to do (vectors complete)")
            return 0

        print(f"backfill: {total} chunks missing vectors (dim={expected_dim})")
        done = 0
        for i in range(0, total, args.batch):
            batch = missing[i : i + args.batch]
            texts = [r["text"] for r in batch]
            blobs = embedder.embed_blobs(texts)
            for row, blob in zip(batch, blobs, strict=True):
                # Width check: a packed BLOB is 4 bytes per dim (§5).
                if len(blob) != 4 * expected_dim:
                    print(
                        f"FAIL: embedder returned dim {len(blob) // 4}, "
                        f"expected {expected_dim}"
                    )
                    return 1
                conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (row["chunk_id"],))
                conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                    (row["chunk_id"], blob),
                )
            done += len(batch)
            print(f"  {done}/{total}")
    print(f"backfill complete: vectors={done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
