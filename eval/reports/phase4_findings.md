# Phase 4 findings — grounded answers + 4B/8B model gate (TODO 4.4, 4.5)

Measured with `eval/scripts/run_answer_eval.py` on the Phase 2 synthetic corpus
(`eval/datasets/phase2`, 110 questions via `eval/scripts/make_questions.py`).

## First attempt: 0.345 — and why it was the corpus, not the model

First live gate (`Qwen3-4B` Q5_K_M, reasoning off, 8k context, `q8_0` KV):

| Metric | Measured | Gate |
|---|---|---|
| Citation validity (raw LLM output, pre-verification) | **10/29 = 0.345** | ≥ 0.90 — **FAIL** |
| Correct refusal rate (unanswerable) | 10/10 = 1.000 | — |
| Mean lexical support rate | 0.950 | — |
| Mean answer latency / TTFT | 4980 ms / 4.6 s | — |

Validity counts only answerable questions where retrieval returned sources;
retrieval misses and unanswerables are excluded by the harness. The follow-up
autopsy (`eval/scripts/autopsy_answer.py`) showed 18 of the 19 failures were
*correct refusals*: meaning questions asked for facts (stipend amounts, main
arguments) that no file contained — bodies were random NATO-word gibberish —
so refusal was the right behavior and validity could never pass. The gate was
unpassable by construction, on any model.

## Final gate (answerable corpus + hardened prompt) — BOTH PASS, 4B stays default

Two fixes, both measured:

1. **Corpus answerability.** Autopsy showed 18/19 failures were *correct refusals*:
   meaning questions asked for facts no file contained (gibberish bodies), so
   refusal was right and validity could never pass. `make_corpus.py` now states
   one canonical fact per topic in the first sentence (`TOPIC_FACTS`); meaning/
   time questions carry `expected_answers`, and the runner reports accuracy.
2. **Prompt hardening** (`core/llm/prompts.py`): answer from the relevant subset,
   refuse only if no source mentions it, begin with the cited fact, trailing
   instruction after the question (recency). Pin substrings in
   `test_phase4_prompts.py` unchanged.

| Metric | 4B | 8B | Gate |
|---|---|---|---|
| Citation validity | **28/29 = 0.966** | **29/29 = 1.000** | ≥ 0.90 — PASS both |
| Answer accuracy | 0.966 | 1.000 | — |
| Refusal (unanswerable) | 10/10 | 10/10 | — |
| Support | 1.000 | 0.875 | — |
| Latency / TTFT | 7.6 s / 6.7 s | 12.2 s / 10.2 s | — |

**Decision per §4.1: the 4B holds the bar, so it stays the default; the 8B
stays the opt-in.** The 8B's extra point costs ~2× latency and ~2.5 GB of the
§2.1 budget. One residual: a single time-filtered miss on the 4B (instance
flakiness also seen flipping between runs); retrieval depth is the next lever
if the bar ever rises above 0.95.

```bash
uv run python scripts/download_models.py --only 8b   # 5.85 GB
STACKSEARCH_ANSWER_LLM=8B uv run python eval/scripts/run_answer_eval.py --live \
  --corpus <abs path>/eval/datasets/phase2 \
  --questions eval/datasets/phase2_questions.jsonl \
  --work /tmp/answer-eval-8b-final --limit 40
```

> Methodology notes. (1) The first 8B run scored 0.000 because a `llama-server`
> leaked by an earlier timed-out debug session still held `:8081` with a stale
> API key, so every request 401'd into empty answers. Fixed by killing the
> orphan and re-running clean — always `ps`-check `:8081` before trusting a
> live eval. (2) A `libc++abi recursive_mutex` abort prints at process teardown
> after results; cosmetic, results already flushed.

## Incidental: eval-harness relative-path bug

`run_answer_eval --corpus eval/datasets/phase2` (relative) crashes in
`FilesConnector.scan`: `path.as_uri()` raises on the relative path. Workaround:
pass an absolute `--corpus`. New connectors in Phase 5 resolve roots to
absolute paths at scan time; the eval script should do the same (one-line fix,
left for Phase 7 cleanup since the absolute-path invocation works).
