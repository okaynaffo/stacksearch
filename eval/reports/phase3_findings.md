# Phase 3 findings: incremental indexing + reranking

Corpus: Phase 2 synthetic set (500 files incl. 10 real PDFs, 939 chunks/vectors,
110 questions). Index rebuilt per run with the §4.1 embedding model; hybrid column
is fused RRF in the baseline run and fused + jina cross-encoder in the reranked run.

## 1. Rerank gain over fused-only (TODO 3.3)

| metric | split | fused-only | +rerank | vec-only (ref) |
|---|---|---|---|---|
| nDCG@10 | exact (40) | 1.000 | 1.000 | 0.124 |
| nDCG@10 | meaning (40) | 0.770 | **0.882** | 0.844 |
| nDCG@10 | time-filtered (15) | 0.514 | **0.576** | 0.565 |
| nDCG@10 | **overall** | **0.826** | **0.884** | 0.497 |
| MRR | meaning | 0.830 | **0.900** | 0.900 |
| recall@10 | meaning | 0.154 | 0.175 | 0.166 |

The Phase 2 open question is closed: hybrid+rerank now beats vector-only on
meaning (0.882 vs 0.844) and time-filtered (0.576 vs 0.565) instead of trailing
it. RRF filler noise was the cause; the cross-encoder over the top-30 fused
candidates recovers the ordering. No RRF re-weighting needed.

Cost: hybrid latency p50 16 ms → 694 ms, p95 78 ms → 3.9 s (first query pays
the ~0.3 GB ONNX load; steady state is CPU scoring of ≤30 full-text pairs).
Short keyword/ID lookups skip reranking (`should_rerank`) and stay on the fast
path, so the §7.4 <300 ms target still holds for exact lookups but not for
question-style queries. Documented tuning knob: `retrieval.rerank_top_k`
(30 → 15 roughly halves the cost). No change made; revisit in Phase 7 if the
results list feels slow.

Model note: the catalog fp32 export (1.11 GB) is overridden at registration by
the int8 `onnx/model_int8.onnx` (279.6 MB) under a `-int8` alias, because the
fastembed registry refuses to override a catalog name in place. Same pattern
as the Phase 0 embedder registration.

## 2. Chunk diffing (§6.5.2)

Changed documents reuse stored rows + vectors by `chunk_hash`; only new hashes
are embedded. Reused chunks keep their `chunk_id`, so stored vectors stay
valid without rewrite. Verified: 2-chunk doc with 1 changed chunk reports
`reused=1, embedded=1`; duplicate-paragraph docs reuse one copy on shrink;
FTS5 and `chunks_vec` counts stay consistent after partial updates.

## 3. Queue + watcher + freshness (§6.6, §6.2, §6.5)

- Persistent priority queue (`jobs` table): priority order, backoff retry with
  `run_after`, drop after 3 attempts, orphan reclaim on restart. Battery pause
  skips bulk (pri ≥ 5) while live changes (pri ≤ 2) still flow.
- Watcher: one FSEvents Observer, 0.5 s debounce; delete wins over modify,
  change-after-delete re-arms (editor atomic-save). Live test: create → 
...[truncated 703 chars]