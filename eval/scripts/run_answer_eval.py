#!/usr/bin/env python3
"""Answer eval: citation validity, refusal rate, support (PLAN.md §14.2, TODO 4.4).

Builds a fresh index from a synthetic corpus, runs every question through the
Phase 4 answer pipeline (``core.llm.answer``), and reports:

* citation validity rate — raw LLM outputs whose every ``[n]`` names a
  provided source (measured pre-verification; the verifier strips the rest);
* correct refusal rate on the ``unanswerable`` split;
* mean lexical support rate (faithfulness proxy, §8.3);
* TTFT in live mode.

Two modes:

* ``--mocked`` (default): a fake LLM answers from the top retrieved chunk
  (answerable) or refuses (unanswerable). No ``llama-server`` needed; gates
  the *pipeline* — validity must be 1.0 or the pipeline itself is broken.
* ``--live``: the real local LLM via ``llama-server``. Gates the Phase 4
  done-when: validity >= 0.90 (``--gate``).

Usage:
    uv run python eval/scripts/run_answer_eval.py --mocked --corpus eval/datasets/phase2 \\
        --questions eval/datasets/phase2_questions.jsonl --work /tmp/answer-eval
    uv run python eval/scripts/run_answer_eval.py --live --corpus ... --questions ... --work ...
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.llm.answer import iter_answer_stream  # noqa: E402
from core.llm.citations import verify_answer  # noqa: E402
from core.llm.prompts import REFUSAL  # noqa: E402


def _load_retrieval_eval():
    spec = importlib.util.spec_from_file_location(
        "run_retrieval_eval", REPO_ROOT / "eval" / "scripts" / "run_retrieval_eval.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MockLLM:
    """Fake answerer: extractive by construction.

    Answerable: first sentence of the top source + ``[1]``. Unanswerable
    (no sources): the refusal. Validity must therefore be perfect — anything
    less means the pipeline misnumbers sources or mangles citations.
    """

    def __init__(self, sources):
        self._sources = sources

    def _ensure_ready(self) -> None:
        return None

    def stream(self, messages, **kwargs):
        from core.llm.client import StreamChunk

        if not self._sources:
            yield StreamChunk(text=REFUSAL)
            return
        first = (self._sources[0].get("text") or "").split(".")[0].strip()
        for piece in (first, " [1]."):
            if piece:
                yield StreamChunk(text=piece)

    def chat(self, messages, **kwargs):
        from core.llm.client import ChatResult

        if not self._sources:
            return ChatResult(text=REFUSAL)
        first = (self._sources[0].get("text") or "").split(".")[0].strip()
        return ChatResult(text=f"{first} [1].")


def _collect(query, conn, client):
    """Run the stream to completion; return (raw_text, done_payload)."""
    raw_parts: list[str] = []
    done: dict = {}
    for frame in iter_answer_stream(query, conn, client=client):
        if frame["event"] == "delta":
            raw_parts.append(frame["data"].get("text", ""))
        elif frame["event"] == "done":
            done = frame["data"]
        elif frame["event"] == "error":
            done = {"error": frame["data"].get("error"), "text": "", "citations": []}
    return "".join(raw_parts), done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--work", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--mocked", action="store_true", default=True)
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--gate", type=float, default=0.90)
    ap.add_argument("--limit", type=int, default=40, help="max questions (0 = all)")
    args = ap.parse_args()

    live = bool(args.live)
    retr = _load_retrieval_eval()
    work = Path(args.work)
    db_path, _ = retr.build_index(Path(args.corpus), work)

    from core.index import open_index

    questions = [json.loads(line) for line in open(args.questions) if line.strip()]
    if args.limit:
        # Stratified: keep type balance when truncating.
        by_type: dict[str, list] = {}
        for q in questions:
            by_type.setdefault(q["type"], []).append(q)
        per = max(1, args.limit // len(by_type))
        questions = [q for qs in by_type.values() for q in qs[:per]]

    if live:
        from core.llm.client import get_client

        client = get_client()
        try:
            client._ensure_ready()
        except Exception as exc:  # noqa: BLE001
            print(f"live LLM unavailable: {exc}")
            return 2
    else:
        client = None  # per-question MockLLM, built after retrieval
    validity_hits = 0
    validity_n = 0
    accuracy_hits = 0
    accuracy_n = 0
    refusals = 0
    refusal_n = 0
    support_rates: list[float] = []
    ttfts: list[float] = []
    latencies: list[float] = []
    t0 = time.perf_counter()

    with open_index(db_path) as ix:
        conn = ix.conn
        for q in questions:
            query = q["query"]
            if live:
                raw, done = _collect(query, conn, client)
            else:
                # Retrieve first so the mock answers from real sources.
                from core.llm.answer import prepare_prompt

                built, _warnings = prepare_prompt(conn, query)
                mock = MockLLM(built.sources)
                raw, done = _collect(query, conn, mock)

            if q["type"] == "unanswerable":
                refusal_n += 1
                if not done.get("citations") and "couldn't find that" in done.get("text", ""):
                    refusals += 1
                continue

            sources = done.get("sources", [])
            if not sources:
                continue  # retrieval miss: counted in retrieval eval, not here
            validity_n += 1
            v = verify_answer(raw, sources)
            if raw and v.valid_citations and not any(s.invalid for s in v.sentences):
                validity_hits += 1
            want = q.get("expected_answers") or []
            if want:
                accuracy_n += 1
                if any(w.lower() in raw.lower() for w in want):
                    accuracy_hits += 1
            if v.support_rate is not None:
                support_rates.append(v.support_rate)
            if done.get("ttft_s") is not None:
                ttfts.append(done["ttft_s"])
            latencies.append(done.get("latency_ms", 0))

    validity = validity_hits / validity_n if validity_n else 0.0
    refusal_rate = refusals / refusal_n if refusal_n else 0.0
    mean_support = sum(support_rates) / len(support_rates) if support_rates else 0.0
    mean_ttft = sum(ttfts) / len(ttfts) if ttfts else None
    mean_lat = sum(latencies) / len(latencies) if latencies else 0.0

    print(f"mode: {'live' if live else 'mocked'}  questions: {len(questions)}")
    print(f"citation validity: {validity_hits}/{validity_n} = {validity:.3f} (gate {args.gate})")
    if accuracy_n:
        print(
            f"answer accuracy: {accuracy_hits}/{accuracy_n} = "
            f"{accuracy_hits / accuracy_n:.3f} (expected fact in answer)"
        )
    print(f"refusal rate (unanswerable): {refusals}/{refusal_n} = {refusal_rate:.3f}")
    print(f"mean support rate: {mean_support:.3f}")
    print(f"mean answer latency: {mean_lat:.0f} ms  TTFT: {mean_ttft}")
    print(f"eval wall time: {time.perf_counter() - t0:.1f}s")

    gate = 1.0 if not live else args.gate
    if live:
        # The eval owns the server it spawned: unload it so a later run does
        # not find the port busy (a second supervisor cannot reuse it and
        # fails loudly instead of answering). Stop the client's own
        # supervisor: get_supervisor() would mint a fresh per-launch token and
        # return a different, never-started instance.
        try:
            client.supervisor.stop("answer eval finished")
        except Exception:  # noqa: BLE001, S110 — best effort cleanup
            pass
    if validity < gate:
        print(f"FAIL: validity {validity:.3f} < gate {gate}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
