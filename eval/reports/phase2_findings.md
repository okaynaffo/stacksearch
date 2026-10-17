# Phase 2 findings: hybrid retrieval beats BM25-only (TODO 2.1–2.3)

Corpus: 500 synthetic files (10 real PDFs) + 110 questions
(`eval/datasets/phase2`, `eval/datasets/phase2_questions.jsonl`).
Index: 939 chunks, 939 vectors. Rerun with
`eval/scripts/run_retrieval_eval.py --corpus eval/datasets/phase2
--questions eval/datasets/phase2_questions.jsonl --work <scratch>`.

## nDCG@10 (headline metric)

| split | BM25-only | vector-only | hybrid |
|---|---|---|---|
| exact (40) | 1.000 | 0.124 | 1.000 |
| meaning (40) | 0.000 | 0.844 | 0.770 |
| time-filtered (15) | 0.000 | 0.565 | 0.514 |
| **overall** | **0.421** | **0.497** | **0.826** |

Recall@10 exact: 1.000 in both BM25 and hybrid (no regress). MRR meaning:
vector 0.900, hybrid 0.830. Latency p50/p95 on this corpus:
BM25 11.0/73.4 ms, vector 11.3/71.5 ms, hybrid 15.4/77.3 ms
(embedding dominates; retrieval itself is sub-ms at this scale).

5k-file BM25 gate (TODO 1.3 leftover): p50/p95 0.1/0.1 ms — PASS.

## Three observations for Phase 3

1. **Vectors alone cannot do ID lookup** (exact MRR 0.080) and **BM25 alone
   cannot do meaning** (0.000). Hybrid keeps the best of both — the reason
   the done-when gate is on hybrid-vs-BM25 overall, which passes 0.826 > 0.421.
2. **Vector-only beats hybrid on the meaning/time splits** (0.844 vs 0.770,
   0.565 vs 0.514). Suspected cause: BM25 matches filler words shared across
   all synthetic docs, and RRF adds ~1/61 to that junk, letting it outrank
   mid-tier vector hits. The Phase 3 cross-encoder over the top-30 fused
   candidates (§7.3) should recover this; if it does not, consider weighting
   RRF by path (e.g. down-weight BM25 when the query has no exact terms).
3. **Top-1 fused-score separation** (answerable 0.0223 vs unanswerable 0.0156)
   is narrow — Phase 4 refusal thresholds cannot rely on it alone and will
   need the citation-verification path (§8.3) rather than a score cutoff.

## Incidents during the build

- `dateparser.search_dates` treats the bare word "here" as today; the parser
  fallback now only accepts matches containing a digit or a month/weekday
  name (`core/query/parser.py`). Without the gate, ordinary queries became
  single-day filters returning nothing.
- nDCG initially exceeded 1.0 because several chunks map to one file; the
  runner dedupes files (rank-preserving) before scoring.
