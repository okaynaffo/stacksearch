#!/usr/bin/env python3
"""Failure autopsy for the Phase 4.5 citation-validity gate (PLAN.md §14.3).

For each question in the *same stratified slice* `run_answer_eval.py --limit 40`
uses (first 10 per type, file order), records one JSON line:

  {id, type, n_sources, warnings, hit, raw_len, outcome, raw_head}

where `hit` = an expected file is among the provided sources, and `outcome` is
one of HIT_VALID | HIT_NO_CITE | MISS_REFUSAL | MISS_CITED | NO_SOURCES |
UNANSWERABLE_REFUSED | UNANSWERABLE_ANSWERED.

HIT_NO_CITE is model-side (evidence present, citations absent/wrong).
MISS_* is retrieval-side (evidence absent). The summary table at the end is
what decides where the fix goes.

Usage:
    uv run python eval/scripts/autopsy_answer.py --live --corpus <abs path> \\
        --questions eval/datasets/phase2_questions.jsonl --work /tmp/autopsy-4b \\
        --out /tmp/autopsy-4b.jsonl
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.llm.citations import verify_answer  # noqa: E402


def _load_retrieval_eval():
    spec = importlib.util.spec_from_file_location(
        "run_retrieval_eval", REPO_ROOT / "eval" / "scripts" / "run_retrieval_eval.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _slice(questions: list[dict], limit: int) -> list[dict]:
    by_type: dict[str, list] = {}
    for q in questions:
        by_type.setdefault(q["type"], []).append(q)
    per = max(1, limit // len(by_type))
    return [q for qs in by_type.values() for q in qs[:per]]


def main() -> int:
    ap = argparse.ArgumentParser(description="Autopsy the answer-eval failures")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--live", action="store_true", default=True)
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    retr = _load_retrieval_eval()
    work = Path(args.work)
    db_path, _ = retr.build_index(Path(args.corpus).resolve(), work)
    questions = [json.loads(line) for line in open(args.questions) if line.strip()]
    sliced = _slice(questions, args.limit)

    from core.index import open_index
    from core.llm.answer import prepare_prompt
    from core.llm.client import get_client

    client = get_client()
    try:
        client._ensure_ready()
    except Exception as exc:  # noqa: BLE001
        print(f"live LLM unavailable: {exc}")
        return 2

    rows: list[dict] = []
    with open_index(db_path) as ix:
        conn = ix.conn
        for q in sliced:
            built, warnings = prepare_prompt(conn, q["query"])
            src_titles = [Path(s.get("title") or "").name for s in built.sources]
            expected = set(q.get("expected_files", []))
            hit = bool(expected & set(src_titles))
            row: dict = {
                "id": q["id"],
                "type": q["type"],
                "n_sources": len(built.sources),
                "warnings": warnings,
                "hit": hit,
            }
            if q["type"] == "unanswerable":
                raw = client.chat(built.messages, max_tokens=256).text
                refused = "couldn't find that" in raw.lower()
                row["outcome"] = "UNANSWERABLE_REFUSED" if refused else "UNANSWERABLE_ANSWERED"
                row["raw_head"] = raw[:300]
                rows.append(row)
                continue
            if not built.sources:
                row["outcome"] = "NO_SOURCES"
                row["raw_head"] = ""
                rows.append(row)
                continue
            raw = client.chat(built.messages, max_tokens=512).text
            v = verify_answer(raw, built.sources)
            row["raw_len"] = len(raw)
            row["raw_head"] = raw[:300]
            row["valid_citations"] = v.valid_citations
            row["flags"] = v.flags
            if raw and v.valid_citations and not any(s.invalid for s in v.sentences):
                row["outcome"] = "HIT_VALID" if hit else "MISS_CITED_VALID"
            elif hit:
                row["outcome"] = "HIT_NO_CITE"
            elif not raw.strip() or "couldn't find that" in raw.lower():
                row["outcome"] = "MISS_REFUSAL"
            else:
                row["outcome"] = "MISS_CITED"
            rows.append(row)

    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    from collections import Counter

    print(f"{'outcome':<22}{'n':>4}")
    for outcome, n in sorted(Counter(r["outcome"] for r in rows).items()):
        print(f"{outcome:<22}{n:>4}")
    print("\nby type:")
    for t in ("exact", "meaning", "time-filtered", "unanswerable"):
        sub = [r for r in rows if r["type"] == t]
        c = Counter(r["outcome"] for r in sub)
        print(f"  {t}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    warned = [r["id"] for r in rows if r["warnings"]]
    print(f"\nretrieval warnings on: {warned or 'none'}")
    print(f"wrote {len(rows)} rows to {args.out}")

    try:
        client.supervisor.stop("autopsy finished")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
