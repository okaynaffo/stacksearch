"""Sync embedding helpers for the Phase 2 write path (PLAN.md §6.6, TODO 2.1).

Phase 2 embeds synchronously: callers compute passage embeddings *before*
opening the write transaction and hand the BLOBs to
`core.index.writer.index_document`. Phase 3 will move this call into a
background job; the helper signature stays the same.
"""

from __future__ import annotations


def embed_chunk_texts(texts: list[str]) -> list[bytes]:
    """Embed passage texts to the float32 BLOBs `vec0` stores.

    Uses the passage prefix (`passage:` for e5) and the §6.6 batch size of 32.
    The model loads lazily on first call. Must be called *outside* any SQLite
    write transaction — ONNX inference is slow and must never hold the lock.
    """
    if not texts:
        return []
    from core.models.embedder import get_embedder

    return get_embedder().embed_blobs(texts)
