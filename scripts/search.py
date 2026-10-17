#!/usr/bin/env python3
"""CLI search: hybrid by default, BM25-only on request (PLAN.md §13, TODO 2.2).

Usage:
    uv run python scripts/search.py "order ID 48213" --limit 10
    uv run python scripts/search.py "that article about storage pricing" --mode hybrid
    STACKSEARCH_DATA_DIR=/tmp/idx uv run python scripts/search.py "stipend" --mode bm25
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config import get_settings  # noqa: E402
from core.index import open_index  # noqa: E402
from core.query.bm25 import bm25_search  # noqa: E402
from core.query.hybrid import hybrid_search  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Search over the local index")
    parser.add_argument("query", help="search query (quoted phrases kept verbatim)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", choices=["hybrid", "bm25"], default="hybrid")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    args = parser.parse_args()

    settings = get_settings()
    started = time.perf_counter()
    with open_index(settings.index_db_path) as ix:
        if args.mode == "bm25":
            results = bm25_search(ix.conn, args.query, limit=args.limit)
            warnings: list[str] = []
        else:
            outcome = hybrid_search(ix.conn, args.query, limit=args.limit)
            results = outcome["results"]
            warnings = outcome["warnings"]
    latency_ms = int((time.perf_counter() - started) * 1000)

    if args.json:
        print(json.dumps({"results": results, "latency_ms": latency_ms}, indent=2))
        return 0
    print(f"{len(results)} result(s) in {latency_ms} ms for {args.query!r} [{args.mode}]\n")
    for w in warnings:
        print(f"  warning: {w}")
    for r in results:
        print(f"[{r['chunk_id']}] {r['title']} ({r['source_kind']}) score={r['score']:.4f}")
        print(f"  {r['snippet']}")
        print(f"  {r['uri']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
