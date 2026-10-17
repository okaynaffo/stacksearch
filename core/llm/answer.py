"""Answer orchestration: retrieve → prompt → stream → verify (PLAN.md §7.4, §8).

Two-speed results (§7.4): retrieval returns instantly via ``POST /search``;
this module powers ``POST /answer``, which streams the grounded answer after.
It also owns the §6.7 state 2→3 transition: the indexer yields (via the
caller's ``query_pause``), indexer-side models are evicted, and
``llama-server`` is spawned lazily if not already resident.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from typing import Any

from core.llm.citations import Verification, refusal_verification, verify_answer
from core.llm.prompts import REFUSAL, BuiltPrompt, build_answer_prompt


def evict_index_models() -> None:
    """§6.7 Query-arrives transition: evict embedder + reranker for the LLM."""
    try:
        from core.models.embedder import unload_embedder
    except ImportError:  # pragma: no cover — embedder module always exists
        unload_embedder = None  # type: ignore[assignment]
    try:
        from core.models.reranker import unload_reranker
    except ImportError:  # pragma: no cover
        unload_reranker = None  # type: ignore[assignment]
    if unload_embedder is not None:
        try:
            unload_embedder()
        except Exception:  # noqa: BLE001 — eviction is best effort
            pass
    if unload_reranker is not None:
        try:
            unload_reranker()
        except Exception:  # noqa: BLE001
            pass


def hydrate_texts(
    conn: sqlite3.Connection, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach full chunk texts for the prompt; fused rows may carry only snippets.

    Vec-only hits have no ``text`` (``vector_search`` returns a truncated
    snippet), so one batched PK lookup fills every gap. Rows already carrying
    text are untouched.
    """
    missing = [c["chunk_id"] for c in candidates if not c.get("text")]
    if not missing:
        return candidates
    try:
        rows = conn.execute(
            f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(missing))})",
            missing,
        ).fetchall()
    except Exception:  # noqa: BLE001 — prompt falls back to snippets
        return candidates
    texts = {r["id"]: r["text"] for r in rows}
    out = []
    for c in candidates:
        if not c.get("text") and c["chunk_id"] in texts:
            c = dict(c)
            c["text"] = texts[c["chunk_id"]]
        out.append(c)
    return out


def prepare_prompt(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int | None = None,
    filters: Any = None,
    embed_fn=None,
) -> tuple[BuiltPrompt, list[str]]:
    """Run retrieval and build the §8.1 prompt. Returns (prompt, warnings)."""
    from core.config import get_settings
    from core.query.hybrid import hybrid_search

    settings = get_settings()
    n = limit or settings.retrieval.answer_max_chunks
    n = max(settings.retrieval.answer_min_chunks, min(n, settings.retrieval.answer_max_chunks))
    outcome = hybrid_search(conn, query, limit=n, filters=filters, embed_fn=embed_fn)
    candidates = hydrate_texts(conn, outcome["results"][:n])
    built = build_answer_prompt(query, candidates)
    return built, outcome["warnings"]


def answer_once(
    query: str,
    conn: sqlite3.Connection,
    *,
    client=None,
    limit: int | None = None,
    filters: Any = None,
    embed_fn=None,
    temperature: float = 0.0,
    max_tokens: int = 512,
    verify_threshold: float | None = None,
) -> dict[str, Any]:
    """Non-streaming answer for scripts, eval, and tests (mockable client)."""
    import time as _time

    from core.llm.client import get_client

    started = _time.perf_counter()
    built, warnings = prepare_prompt(conn, query, limit=limit, filters=filters, embed_fn=embed_fn)
    if not built.sources:
        v = refusal_verification(REFUSAL)
        return _result(query, built, REFUSAL, v, warnings, started, ttft_s=None)

    evict_index_models()
    llm = client or get_client()
    try:
        llm._ensure_ready()
    except Exception as exc:  # noqa: BLE001 — LLM down degrades to results-only
        return _result(
            query, built, "", refusal_verification(""), warnings + [f"LLM unavailable: {exc}"],
            started, ttft_s=None, llm_error=str(exc),
        )
    result = llm.chat(built.messages, temperature=temperature, max_tokens=max_tokens)
    kwargs: dict[str, Any] = {}
    if verify_threshold is not None:
        kwargs["threshold"] = verify_threshold
    v = verify_answer(result.text, built.sources, **kwargs)
    text, grounded_flags = _apply_verdict(v, built)
    return _result(query, built, text, v, warnings + grounded_flags, started, ttft_s=None)


def iter_answer_stream(
    query: str,
    conn: sqlite3.Connection,
    *,
    client=None,
    limit: int | None = None,
    filters: Any = None,
    embed_fn=None,
    temperature: float = 0.0,
    max_tokens: int = 512,
    verify_threshold: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield ``{"event", "data"}`` frames for the SSE route.

    Frame order: ``sources`` → zero or more ``delta`` → one ``done`` (or
    ``error``). The ``done`` payload carries the verified text, citations,
    flags, and timings. Verification runs on the full text before ``done``,
    so citations are checked before display (§8.3).
    """
    import time as _time

    from core.llm.client import get_client

    started = _time.perf_counter()
    built, warnings = prepare_prompt(conn, query, limit=limit, filters=filters, embed_fn=embed_fn)
    yield {"event": "sources", "data": {"sources": _public_sources(built), "warnings": warnings}}
    if not built.sources:
        v = refusal_verification(REFUSAL)
        yield {"event": "delta", "data": {"text": REFUSAL}}
        done_data = _done_payload(query, built, REFUSAL, v, warnings, started, None)
        yield {"event": "done", "data": done_data}
        return

    evict_index_models()
    llm = client or get_client()
    try:
        llm._ensure_ready()
    except Exception as exc:  # noqa: BLE001
        err = f"LLM unavailable: {exc}"
        yield {"event": "error", "data": {"error": err, "warnings": warnings}}
        return

    parts: list[str] = []
    ttft: float | None = None
    try:
        for chunk in llm.stream(built.messages, temperature=temperature, max_tokens=max_tokens):
            if ttft is None:
                ttft = _time.perf_counter() - started
            if chunk.text:
                parts.append(chunk.text)
                yield {"event": "delta", "data": {"text": chunk.text}}
    except Exception as exc:  # noqa: BLE001 — partial answer still verified
        warnings = [*warnings, f"stream interrupted: {exc}"]
    full = "".join(parts)
    kwargs: dict[str, Any] = {}
    if verify_threshold is not None:
        kwargs["threshold"] = verify_threshold
    v = verify_answer(full, built.sources, **kwargs)
    text, grounded_flags = _apply_verdict(v, built)
    done_data = _done_payload(query, built, text, v, warnings + grounded_flags, started, ttft)
    yield {"event": "done", "data": done_data}


def _apply_verdict(v: Verification, built: BuiltPrompt) -> tuple[str, list[str]]:
    """Apply the §8.3 no-grounded-answer path. Returns (display_text, flags)."""
    if not v.grounded:
        return REFUSAL, ["no valid citations; showing refusal instead of an ungrounded answer"]
    return v.text, list(v.flags)


def _public_sources(built: BuiltPrompt) -> list[dict[str, Any]]:
    """Source map for the UI: provenance + text for hover, no internals."""
    return [
        {
            "n": s["n"],
            "chunk_id": s.get("chunk_id"),
            "document_id": s.get("document_id"),
            "source_kind": s.get("source_kind"),
            "title": s.get("title"),
            "author": s.get("author"),
            "date": s.get("date"),
            "uri": s.get("uri"),
            "loc": s.get("loc", {}),
            "text": s.get("text", ""),
        }
        for s in built.sources
    ]


def _result(
    query: str,
    built: BuiltPrompt,
    text: str,
    v: Verification,
    warnings: list[str],
    started: float,
    *,
    ttft_s: float | None,
    llm_error: str | None = None,
) -> dict[str, Any]:
    payload = _done_payload(query, built, text, v, warnings, started, ttft_s)
    if llm_error:
        payload["llm_error"] = llm_error
    return payload


def _done_payload(
    query: str,
    built: BuiltPrompt,
    text: str,
    v: Verification,
    warnings: list[str],
    started: float,
    ttft_s: float | None,
) -> dict[str, Any]:
    weak = [
        {"n": s.valid, "supported": s.supported}
        for s in v.sentences
        if s.valid and not all(s.supported.values())
    ]
    return {
        "query": query,
        "text": text,
        "citations": v.valid_citations,
        "grounded": v.grounded,
        "flags": v.flags,
        "warnings": warnings,
        "weak_support": weak,
        "support_rate": v.support_rate,
        "sources": _public_sources(built),
        "prompt_tokens": built.prompt_tokens,
        "trimmed": built.trimmed,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "ttft_s": ttft_s,
    }
