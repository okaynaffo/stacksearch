#!/usr/bin/env python3
"""Search latency benchmark (PLAN.md §14.2, TODO 1.3).

Samples exact-lookup keys from the manifest, runs BM25, reports p50/p95.
Fails (exit 1) if p95 >= 200 ms — the Phase 1 done-when.

Usage:
    uv run python eval/scripts/bench_search.py --corpus eval/datasets/phase1 --queries 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config import get_settings  # noqa: E402
from core.index import open_index  # noqa: E402
from core.query.bm25 import bm25_search  # noqa: E402


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    ordered = sorted(data)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark BM25 search latency")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--queries", type=int, default=200)
    args = parser.parse_args()

    manifest = json.loads((Path(args.corpus) / "manifest.json").read_text())
    keys = [m["key"] for m in manifest[: args.queries]]
    topics = [m["topic"] for m in manifest[: args.queries]]
    queries = keys + [f'"{t}"' for t in topics[: max(0, args.queries - len(keys))]]
    queries = queries[: args.queries]

    settings = get_settings()
    latencies: list[float] = []
    with open_index(settings.index_db_path) as ix:
        for q in queries:
            start = time.perf_counter()
            bm25_search(ix.conn, q, limit=20)
            latencies.append((time.perf_counter() - start) * 1000)

    p50 = statistics.median(latencies)
    p95 = _percentile(latencies, 95)
    print(
        f"queries={len(latencies)} p50={p50:.1f}ms p95={p95:.1f}ms "
        f"min={min(latencies):.1f}ms max={max(latencies):.1f}ms"
    )
    if p95 >= 200:
        print("FAIL: p95 >= 200 ms (Phase 1 done-when not met)")
        return 1
    print("PASS: p95 < 200 ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
