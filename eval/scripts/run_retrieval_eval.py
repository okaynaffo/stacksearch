#!/usr/bin/env python3
"""Retrieval eval: BM25-only vs vector-only vs hybrid (PLAN.md §14.2/14.3, TODO 2.3).

Builds a fresh index from a synthetic corpus (both connectors, sync embed),
runs every question in three modes, and reports Recall@10, MRR, nDCG@10 per
question type plus latency p50/p95 per mode.

Done-when gate (TODO Phase 2): hybrid beats BM25-only overall. Exits 1 when it
does not. `exact` must not regress either — hybrid Recall@10 on `exact` must be
>= BM25's. `unanswerable` questions are excluded from ranking metrics; the
report shows top-1 fused-score separation (answerable vs unanswerable) as the
signal Phase 4 refusal thresholds will use.

Usage:
    uv run python eval/scripts/run_retrieval_eval.py --corpus eval/datasets/phase2 \\
        --questions eval/datasets/phase2_questions.jsonl --work /tmp/eval-work
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.chunking.prose import chunk_prose  # noqa: E402
from core.connectors.files import FilesConnector  # noqa: E402
from core.connectors.pdf import PdfConnector, resolve_fs_path  # noqa: E402
from core.extract.pdf import extract_pdf  # noqa: E402
from core.extract.text import extract_by_suffix  # noqa: E402
from core.index import open_index  # noqa: E402
from core.index.embed_helpers import embed_chunk_texts  # noqa: E402
from core.index.writer import ChunkWrite, index_document  # noqa: E402
from core.query.bm25 import bm25_search  # noqa: E402
from core.query.hybrid import hybrid_search  # noqa: E402
from core.query.parser import parse_query  # noqa: E402
from core.query.vector import vector_search  # noqa: E402

AT = 10


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    ordered = sorted(data)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def recall_at(relevant: set[str], ranked: list[str], k: int = AT) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def mrr(relevant: set[str], ranked: list[str]) -> float:
    for i, doc in enumerate(ranked, start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(relevant: set[str], ranked: list[str], k: int = AT) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, doc in enumerate(ranked[:k]) if doc in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def build_index(corpus: Path, work: Path) -> tuple[Path, dict[str, str]]:
    """Index *corpus* into a fresh DB under *work*. Returns (db_path, chunk→file)."""
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "eval-index.db"
    if db_path.exists():
        db_path.unlink()
    chunk_to_file: dict[str, str] = {}
    with open_index(db_path) as ix:
        conn = ix.conn
        for kind, connector in (("files", FilesConnector()), ("pdf", PdfConnector())):
            conn.execute(
                "INSERT INTO sources(kind, name, config_json) VALUES (?, ?, ?)",
                (kind, f"eval-{kind}", json.dumps({"paths": [str(corpus)]})),
            )
            source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for ref in connector.scan({"paths": [str(corpus)]}):
                fs_path = resolve_fs_path(ref.external_id)
                try:
                    if kind == "pdf":
                        result = extract_pdf(fs_path)
                        header = f"[pdf] {ref.title}"
                        chunks: list[ChunkWrite] = []
                        for page in result.pages:
                            for pc in chunk_prose(
                                page.text, header=header, loc_extra={"page": page.page}
                            ):
                                chunks.append(
                                    ChunkWrite(
                                        text=pc.text, loc_json=pc.loc_json,
                                        token_count=pc.token_count,
                                    )
                                )
                        text = "\n\n".join(p.text for p in result.pages)
                    else:
                        text = extract_by_suffix(fs_path)
                        header = f"[note] {ref.title}"
                        chunks = [
                            ChunkWrite(text=c.text, loc_json=c.loc_json,
                                       token_count=c.token_count)
                            for c in chunk_prose(text, header=header)
                        ]
                    if not chunks:
                        continue
                    import datetime as _dt

                    mtime = _dt.datetime.fromtimestamp(
                        fs_path.stat().st_mtime
                    ).date().isoformat()
                    res = index_document(
                        conn, source_id=source_id, external_id=ref.external_id,
                        uri=ref.uri, title=ref.title, text=text, chunks=chunks,
                        modified_at=mtime,
                        embeddings=embed_chunk_texts([c.text for c in chunks]),
                    )
                    rows = conn.execute(
                        "SELECT id FROM chunks WHERE document_id = ? ORDER BY ord",
                        (res["document_id"],),
                    ).fetchall()
                    for r in rows:
                        chunk_to_file[str(r["id"])] = ref.title
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip {ref.title}: {type(exc).__name__}: {exc}")
    return db_path, chunk_to_file


def retrieve(conn, mode: str, query: str, limit: int, query_vec=None) -> list[dict]:
    if mode == "bm25":
        parsed = parse_query(query)
        return bm25_search(conn, parsed.text, limit=limit, filters=parsed.filters)
    if mode == "vector":
        assert query_vec is not None
        parsed = parse_query(query)
        return vector_search(conn, query_vec, limit=limit, filters=parsed.filters)
    outcome = hybrid_search(conn, query, limit=limit)
    return outcome["results"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 2 retrieval eval")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--work", required=True, help="scratch dir for the eval index")
    parser.add_argument("--limit", type=int, default=AT)
    args = parser.parse_args()

    corpus = Path(args.corpus).resolve()
    questions = [
        json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()
    ]
    print(f"indexing {corpus} ...")
    started = time.perf_counter()
    db_path, chunk_to_file = build_index(corpus, Path(args.work))
    print(f"  indexed in {time.perf_counter() - started:.1f}s")

    from core.models.embedder import get_embedder

    embedder = get_embedder()
    modes = ("bm25", "vector", "hybrid")
    # metric -> mode -> type -> values
    scores: dict[str, dict[str, dict[str, list[float]]]] = {
        m: {mode: {} for mode in modes} for m in ("recall", "mrr", "ndcg")
    }
    lat: dict[str, list[float]] = {mode: [] for mode in modes}
    top1_scores: dict[str, list[float]] = {"answerable": [], "unanswerable": []}

    with open_index(db_path) as ix:
        conn = ix.conn
        n_vec = conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        print(f"  chunks={n_chunks} vectors={n_vec}")
        vec_cache: dict[str, list[float]] = {}
        for q in questions:
            qtype, query = q["type"], q["query"]
            expected = set(q["expected_files"])
            if query not in vec_cache:
                vec_cache[query] = embedder.embed_queries([parse_query(query).text])[0]
            for mode in modes:
                t0 = time.perf_counter()
                results = retrieve(
                    conn, mode, query, args.limit,
                    query_vec=vec_cache.get(query),
                )
                lat[mode].append((time.perf_counter() - t0) * 1000)
                # Dedupe files preserving rank: several chunks can belong to one
                # file, and counting each would push nDCG above 1.
                ranked: list[str] = []
                for r in results:
                    doc = chunk_to_file.get(str(r["chunk_id"]), "?")
                    if doc not in ranked:
                        ranked.append(doc)
                if qtype == "unanswerable":
                    if mode == "hybrid":
                        top1_scores["unanswerable"].append(
                            results[0]["score"] if results else 0.0
                        )
                    continue
                scores["recall"][mode].setdefault(qtype, []).append(
                    recall_at(expected, ranked))
                scores["mrr"][mode].setdefault(qtype, []).append(mrr(expected, ranked))
                scores["ndcg"][mode].setdefault(qtype, []).append(ndcg_at(expected, ranked))
                if mode == "hybrid" and results:
                    top1_scores["answerable"].append(results[0]["score"])

    def mean(vals: list[float]) -> float:
        return statistics.fmean(vals) if vals else 0.0

    print(f"\n{'metric':<8}{'split':<14}{'bm25':>8}{'vector':>8}{'hybrid':>8}")
    summary: dict = {"modes": {}, "gate": {}}
    for metric in ("recall", "mrr", "ndcg"):
        for qtype in ("exact", "meaning", "time-filtered"):
            row = [mean(scores[metric][m].get(qtype, [])) for m in modes]
            print(f"{metric:<8}{qtype:<14}{row[0]:>8.3f}{row[1]:>8.3f}{row[2]:>8.3f}")
            summary["modes"].setdefault(qtype, {})[metric] = dict(zip(modes, row, strict=True))
    overall = {
        m: mean(
            [v for qtype in ("exact", "meaning", "time-filtered")
             for v in scores["ndcg"][m].get(qtype, [])]
        )
        for m in modes
    }
    print(f"\nndcg@10 overall: bm25={overall['bm25']:.3f} "
          f"vector={overall['vector']:.3f} hybrid={overall['hybrid']:.3f}")
    print("latency p50/p95 ms: " + ", ".join(
        f"{m}={statistics.median(lat[m]):.1f}/{_percentile(lat[m], 95):.1f}" for m in modes
    ))
    if top1_scores["answerable"] and top1_scores["unanswerable"]:
        print(f"top-1 fused score: answerable={mean(top1_scores['answerable']):.4f} "
              f"unanswerable={mean(top1_scores['unanswerable']):.4f}")

    gate_hybrid_beats = overall["hybrid"] > overall["bm25"]
    exact_recall = {m: mean(scores["recall"][m].get("exact", [])) for m in modes}
    gate_no_regress = exact_recall["hybrid"] >= exact_recall["bm25"]
    print("\ngate hybrid>bm25 overall: " + ("PASS" if gate_hybrid_beats else "FAIL"))
    print(f"gate hybrid exact-recall no-regress "
          f"({exact_recall['hybrid']:.3f} vs {exact_recall['bm25']:.3f}): "
          f"{'PASS' if gate_no_regress else 'FAIL'}")
    summary["gate"] = {"hybrid_beats_bm25": gate_hybrid_beats, "exact_no_regress": gate_no_regress}
    (Path(args.work) / "retrieval_eval.json").write_text(json.dumps(summary, indent=2))
    return 0 if (gate_hybrid_beats and gate_no_regress) else 1


if __name__ == "__main__":
    raise SystemExit(main())
