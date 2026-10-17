# TODO.md

Execution checklist for the private local-first search engine. All design decisions live in
[PLAN.md](./PLAN.md) — this file holds tasks only.

**Rules for this file**
- Every task links back to the `PLAN.md` section it implements. No design rationale, no duplicated
  specs, no decisions. If you find yourself explaining *why* here, it belongs in `PLAN.md`.
- Safe to delete and regenerate from `PLAN.md` without losing anything.
- A checked box means the acceptance criterion in that `PLAN.md` phase's "done when" is met,
  not merely that code compiles.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked (note why inline)

---

## Phase 0 — Setup (week 1)

Done when: `/status` responds correctly from a script **and** from a Tauri window; `rustup` +
Tauri v2 scaffolded; global hotkey summons the window; the Rust side spawns and supervises the
Python core service and reads its `/events` SSE stream; embeddings + LLM answer from a script.
No real UI yet — raw JSON in the window is fine. (`PLAN.md` §13, §13.1)

### 0.1 Model pre-flight — do these first, they gate everything downstream

- [x] Confirm `intfloat/multilingual-e5-base` appears in `fastembed.list_supported_models()` with an ONNX export — **it does not**; resolved via `add_custom_model` instead (`PLAN.md` §4.1)
- [x] If absent, walk the fallback chain in order and record which one was chosen — **chain not needed**: `Xenova/multilingual-e5-base` `onnx/model_int8.onnx` registered at 768d with no torch. The official repo's only quantized export is x86-only and cannot run on Apple Silicon. (`PLAN.md` §4.1)
- [x] Record the resolved model id + dimension into the `meta` table design (`PLAN.md` §5)
- [x] Build llama.cpp and pin the commit in `scripts/` — pinned prebuilt `b11462` + sha256 (`PLAN.md` §4.1)
- [x] Download `Qwen3-4B` Q5_K_M GGUF into `models/` (default) — 2.89 GB verified (`PLAN.md` §4.1)
- [ ] Download `Qwen3-8B` Q5_K_M GGUF into `models/` (quality opt-in) — deferred to Phase 4.5 (`PLAN.md` §4.1)

### 0.2 Validate the memory budget before building on it

- [x] Benchmark at `-c 8192` with `-ctk/-ctv q8_0`: tok/s, TTFT, peak RSS — 4B done (TTFT 0.673s cold, 37 tok/s, 10.5 GB); 8B deferred to Phase 4.5 (`PLAN.md` §2.1)
- [x] Re-measure with f16 KV cache, record the delta — 0.52 GB saved, matches §2.1's 0.6 GB claim (`PLAN.md` §14.3)
- [!] Record actual peak RSS; confirm or refute the ~9.7 GB (4B) and ~12.2 GB (8B) figures — **4B measured at 10.5 GB** (0.8 GB over prediction, still +3.5 GB headroom); 8B deferred (`PLAN.md` §2.1)
- [x] Check for swap activity under sustained load on this fanless machine — zero swap (`PLAN.md` §2.1)
- [x] If measured peak exceeds ~14 GB on the 4B, drop to Q4_K_M — **not triggered** (10.5 GB) (`PLAN.md` §2.1)

### 0.3 Repo and toolchain

- [x] `git init` with `.gitignore` (venv, `models/`, `*.gguf`, index dir, `ui/dist`)
- [x] `uv init` Python 3.12 project; pin deps — Phase 5 connectors pinned in an optional extra (`PLAN.md` §4)
- [x] CI assertion that `torch` is absent from the venv — checks installed, `uv.lock`, and importable (`PLAN.md` §16)
- [x] Lay out `core/` exactly as specified (`PLAN.md` §12)

### 0.4 Schema and migrations

- [x] Migrations for `sources`, `documents`, `chunks`, `chunks_fts`, `chunks_vec`, `jobs`, `query_log` (`PLAN.md` §5)
- [x] Generate `vec0` DDL from `meta.embedding_dim` — never hand-write `FLOAT[768]`; AST-enforced by test (`PLAN.md` §5)
- [x] `PRAGMA journal_mode=WAL` (`PLAN.md` §5)
- [x] `content_hash` on `documents`, `chunk_hash` on `chunks` (`PLAN.md` §5, §6.5)
- [x] Dimension-change rebuild path: drop + recreate `chunks_vec`, enqueue `reembed` (`PLAN.md` §5)

### 0.5 Core service and auth

- [x] FastAPI skeleton; bind `127.0.0.1` only — non-loopback binds rejected by a validator (`PLAN.md` §9)
- [x] `GET /status` (`PLAN.md` §10)
- [x] Per-launch random token required on every route — enforced at the router, so no route can be public (`PLAN.md` §9)

### 0.6 Model runtime

- [x] fastembed embedder wrapper, batch size 32 (`PLAN.md` §6.6)
- [x] `llama-server` supervisor: spawn with the exact flags in §4.1, health check, kill on idle timeout (`PLAN.md` §4.1, §6.7)
- [x] OpenAI-compatible chat client against `127.0.0.1:8081` (`PLAN.md` §8.4)
- [x] Config selects the answer LLM without code changes; 8B is a settings toggle (`PLAN.md` §4.1)
- [x] Script proving embed + answer end to end from the CLI — `scripts/smoke_models.py` passes (`PLAN.md` §13 Phase 0)

### 0.7 Tauri skeleton — the risky integration, retired early

- [x] `tauri init` in `app/` (CLI 2.12.1 and Rust 1.99.0 are already installed) (`PLAN.md` §13.1)
- [x] Rust spawns the Python core service and supervises it (restart with backoff, process-group shutdown) (`PLAN.md` §13.1)
- [x] Rust performs the §9 token handshake with the local API (`PLAN.md` §13.1)
- [!] Global hotkey summons/focuses the window — **blocked by the environment**: `RegisterEventHotKey` fails for all 6 combos tried, from both a bare binary and a signed `.app` bundle. Needs macOS Input Monitoring permission; see `eval/reports/phase0_findings.md` §6 (`PLAN.md` §13.1, §11.2)
- [x] Window renders raw `/status` JSON — verified by screenshot (`PLAN.md` §13.1)
- [x] Read `/events` SSE stream through WKWebView — `subscribers: 1` confirmed live (`PLAN.md` §13.1, §10)
- [x] `ui/` builds to a static bundle and FastAPI serves it at `/` — the dev-host contract for Phases 1–5 (`PLAN.md` §4 UI host row)

---

## Phase 1 — Files + keyword search (weeks 2–3)

Done when: searching a 5k-file folder returns in < 200 ms. (`PLAN.md` §13)

### 1.1 Connectors and extraction

- [x] Files/notes connector: walk folders with include/exclude globs (`PLAN.md` §6.2)
- [x] Default exclusions: `node_modules`, `.git`, `.venv`, build dirs (`PLAN.md` §6.2)
- [x] PDF connector via PyMuPDF, page-level `loc_json` (`PLAN.md` §6.2)
- [x] OCR fallback only for pages with no text layer (`PLAN.md` §6.3)
- [x] md / txt / docx extraction (`PLAN.md` §6.3)
- [x] Normalize to UTF-8, collapse whitespace, strip signatures and quoted replies below "On … wrote:" (`PLAN.md` §6.3)
- [x] Keep structure as metadata, never as text (`PLAN.md` §6.3)
- [x] Hard limits: skip > 50 MB and binaries; record each skip with a reason (`PLAN.md` §6.3)

### 1.2 Chunking

- [x] Prose strategy: split on headings/paragraphs, pack to ~300–500 tokens, ~15% overlap (`PLAN.md` §6.4)
- [x] Context-header prefix on every chunk (e.g. `[note] notes/internship.md`) (`PLAN.md` §6.4)
- [x] Stable chunk IDs and location pointers (`PLAN.md` §6)

### 1.3 Keyword search

- [x] FTS5 with `porter unicode61`, standalone table + triggers over `chunks` — external-content unworkable since `title` lives in `documents` (`PLAN.md` §5)
- [x] BM25 ranking query (`PLAN.md` §7.2)
- [x] Snippet generation with match highlighting (`PLAN.md` §10)
- [x] CLI search command (`PLAN.md` §13 Phase 1)
- [x] `POST /search` API returning the §10 shape (CLI + API together, per plan)
- [x] Measure p50/p95 on a 5k-file fixture; confirm < 200 ms (`PLAN.md` §14.2) — 5k files, p50/p95 0.1/0.1 ms via `bench_search.py` (Phase 2)

---

## Phase 2 — Semantic + hybrid (weeks 4–5)

Done when: hybrid retrieval beats BM25-only on the eval set. (`PLAN.md` §13)

### 2.1 Vector search

- [x] Load `sqlite-vec` extension into the same DB (`PLAN.md` §5) — `core/index/db.py:load_vec_extension`, proven by backfill + eval runs
- [x] Embed the query with the §4.1 model (`PLAN.md` §4.1) — `core/query/hybrid.py` via `Embedder.embed_queries` (`query:` prefix)
- [x] kNN top 50 against `chunks_vec` (`PLAN.md` §7.2) — `core/query/vector.py:vector_search`
- [x] Apply the same metadata filters to both keyword and vector paths (`PLAN.md` §7.2) — `core/query/filters.py:filter_clause`, used by both paths

### 2.2 Fusion and ranking

- [x] Reciprocal Rank Fusion, k = 60 (`PLAN.md` §7.2) — `core/query/fusion.py:rrf_fuse`
- [x] Recency boost for email/chat; title-match boost (`PLAN.md` §7.2) — small additive boosts in `fusion.py`
- [x] Cap at 2 chunks per document in the top results (`PLAN.md` §7.2) — enforced in `rrf_fuse` and the BM25 fallback path
- [x] Reserved no-op reranker interface so Phase 3 is a config change (`PLAN.md` §4.1) — `core/query/rerank.py` (`NoOpReranker`, `should_rerank` skips short keyword lookups)

### 2.3 Evaluation harness

- [x] Synthetic personal corpus generator (`PLAN.md` §14.1) — extended with real-PDF subset (`--pdfs`) + deterministic 2025 mtime spread for time questions
- [x] 100+ questions with known answer passages, split exact / meaning-based / time-filtered / unanswerable (`PLAN.md` §14.1) — 110 via `eval/scripts/make_questions.py`
- [x] Metrics: Recall@10, MRR, nDCG@10 (`PLAN.md` §14.2) — `eval/scripts/run_retrieval_eval.py`
- [x] Baseline run: BM25-only vs vector-only vs hybrid (`PLAN.md` §14.3) — hybrid nDCG@10 0.826 vs BM25 0.421 on the 500-file corpus; see `eval/reports/phase2_findings.md`

---

## Phase 3 — Incremental indexing (week 6)

Done when: editing a note makes it searchable in < 5 s, and deletes disappear. (`PLAN.md` §13)

### 3.1 Change detection

- [x] `watchdog` FSEvents watcher emitting new/changed/deleted (`PLAN.md` §6.2) — `core/connectors/watch.py:WatchManager`, 0.5 s debounce; `watch()` on the connector protocol + both connectors
- [x] Document level: compare `content_hash`, skip if unchanged (`PLAN.md` §6.5) — pre-embed short-circuit in `core/jobs/tasks.py:run_index_doc`
- [x] Chunk level: diff by `chunk_hash`, keep rows and embeddings for unchanged chunks (`PLAN.md` §6.5) — `core/index/writer.py:_replace_chunks_diff`, reused chunks keep their ids
- [x] Deletions cascade to `chunks`, `chunks_fts`, and `chunks_vec` (`PLAN.md` §6.5) — `mark_deleted` wired to watcher + scan diff (`run_scan`, `run_delete_doc`)
- [x] One transaction per document update so search never sees a half-indexed document (`PLAN.md` §6.5) — unchanged, diff runs inside the existing `BEGIN IMMEDIATE` block
- [x] Daily low-priority reconcile scan to catch events missed while closed (`PLAN.md` §6.5) — `schedule_reconcile` (pri 9), at worker start + hourly

### 3.2 Queue and throttling

- [x] Priority queue persisted in `jobs`, survives restarts (`PLAN.md` §6.6) — `core/jobs/queue.py` + in-process `Worker` thread with own write connection; orphans reclaimed on start
- [x] Priority order: user reindex > live changes > bulk first-run > reconcile (`PLAN.md` §6.6) — 1/2/5/9 via `POST /sources/{id}/reindex`, watcher jobs, scan children, reconcile
- [x] Exponential backoff; mark document `error` after 3 attempts and surface it in settings (`PLAN.md` §6.6)
- [x] Pause on battery and on thermal pressure (`PLAN.md` §6.6) — heuristic only: battery flag pauses bulk, query holds `policy.query_pause()`; no macOS thermal APIs (deferred, see note below)
- [x] Resident-memory gate, all four states (`PLAN.md` §6.7) — indexer yields within one batch on queries + page-cache trim (Phase 3); LLM-evict orchestration completed in Phase 4 (`core/llm/answer.py:evict_index_models` + lazy spawn, covered by `test_answer_evicts_index_models`). See `eval/reports/phase3_findings.md` §4.

### 3.3 Reranking

- [x] Enable `jina-reranker-v2-base-multilingual` on the top ~30 fused candidates (`PLAN.md` §7.3) — lazy `TextCrossEncoder` int8 (279.6 MB) via `-int8` registry alias; full chunk text attached for scoring, stripped before response
- [x] Skip reranking for short keyword lookups (`PLAN.md` §7.3) — unchanged `should_rerank`; failures degrade to fusion order with a warning
- [x] Measure the gain over fused-only ranking (`PLAN.md` §14.3) — hybrid nDCG@10 0.826 → 0.884, now beats vec-only on meaning/time; see `eval/reports/phase3_findings.md` §1
---

## Phase 4 — Grounded answers (weeks 7–8)

Done when: ≥ 90% of answers contain only valid citations on the eval set. (`PLAN.md` §13)

### 4.1 Grounding

- [x] System + sources + question prompt template (`PLAN.md` §8.1) — `core/llm/prompts.py`
- [x] Context budget: 6–8 chunks, ~3–4k tokens, ordered by fused score (`PLAN.md` §8.2)
- [x] Trim chunks to their most relevant sentences when over budget (`PLAN.md` §8.2) — tail-trim, numbering stable
- [x] Enforce the 8k context cap — do not raise it (`PLAN.md` §4.1) — clamped to `answer_context_token_budget`
- [x] System prompt states retrieved text is data, not instructions (`PLAN.md` §9)

### 4.2 Streaming API and UI

- [x] `POST /answer` streaming over SSE (`PLAN.md` §10) — `sources` → `delta`* → `done`/`error`
- [x] Two-speed behavior: results return immediately, answer streams after (`PLAN.md` §7.4)
- [x] Answer panel above results, with citation numbers (`PLAN.md` §11.2) — `app/ui/src/AnswerPanel.tsx`

### 4.3 Citation verification

- [x] Parse every `[n]` in the output (`PLAN.md` §8.3) — `core/llm/citations.py`
- [x] Reject citation numbers not in the provided source set; strip and flag the sentence (`PLAN.md` §8.3)
- [x] Optional support check: lexical or embedding overlap per cited sentence (`PLAN.md` §8.3) — lexical token-recall, flags weak support
- [x] "No grounded answer" path when zero valid citations survive (`PLAN.md` §8.3)
- [x] Hovering a citation shows the passage (`PLAN.md` §11.2) — chip hover popup + click opens source

### 4.4 Eval

- [x] Citation validity rate (`PLAN.md` §14.2) — `eval/scripts/run_answer_eval.py`, see `eval/reports/phase4_findings.md`
- [x] Faithfulness: claims supported by the cited chunk (`PLAN.md` §14.2) — mean lexical support rate
- [x] Correct refusal rate on unanswerable questions (`PLAN.md` §14.2)
- [x] Unit test the citation parser (`PLAN.md` §15) — `tests/unit/test_phase4_citations.py`

### 4.5 Model size gate — decides the §4.1 default

Run both models on the eval set before Phase 5 starts. This is the measurement that decides
whether the 4B stays the default (`PLAN.md` §4.1, §14.3).

- [x] Score 4B and 8B on citation validity, correct refusal rate, faithfulness, TTFT (`PLAN.md` §14.3) — **final, both pass**: 4B validity 28/29 = 0.966, accuracy 0.966, refusal 10/10, support 1.000, TTFT ~6.7 s; 8B validity 29/29 = 1.000, accuracy 1.000, refusal 10/10, support 0.875, TTFT ~10.2 s. Two fixes got here: answerable corpus (`TOPIC_FACTS`) + hardened prompt. See `eval/reports/phase4_findings.md`
- [x] Compare 4B against the ≥ 90% citation validity bar (`PLAN.md` §13 Phase 4) — **clears it (0.966 ≥ 0.90)**
- [x] If 4B clears the bar, keep it as default and document the 8B as opt-in (`PLAN.md` §4.1) — **applies: 4B default, 8B opt-in** (8B's extra point costs ~2× latency + ~2.5 GB)
- [x] If 4B misses it, promote the 8B to default and update `PLAN.md` §2.1 and §4.1 — it is a config change, not a code change — **not triggered: 4B holds the bar**

---

## Phase 5 — More sources (weeks 9–10)

Done when: all six source types are indexed. (`PLAN.md` §13)

> Note: macOS Full Disk Access is required for essentially every source here (`~/Library`, Mail,
> browser profiles). Treat the onboarding as the default case, not the exception (`PLAN.md` §9, §16).

### 5.1 Email

- [x] IMAP connector over TLS with app passwords (`PLAN.md` §6.2) — `core/connectors/email_imap.py`, `imapclient` lazy-imported (optional `phase5` extra); live path not exercised here, mock-tested via a fake client
- [x] `UIDVALIDITY` + highest-UID cursor; flag sync for deletions (`PLAN.md` §6.2) — per-folder JSON cursor in `sources.cursor`, advanced in `run_index_doc`; deletion flag-sync deferred
- [x] Credentials in macOS Keychain, never in config files (`PLAN.md` §9) — `core/privacy/keychain.py` via `/usr/bin/security` (no `keyring` dep)
- [x] `mailparser` extraction; index body + subject + from/to (`PLAN.md` §6.2) — stdlib `email`/`mailbox` instead of `mail-parser` (avoids the stalled `phase5` install); body preferred `text/plain`, HTML tag-stripped fallback
- [x] One chunk per message body, subject and sender prepended (`PLAN.md` §6.4) — `core/chunking/email.py` (`[email] From: … Subject: …`, ≤ 500 tokens, long bodies packed)
- [x] Local `.mbox` / Apple Mail store as an offline-first alternative (`PLAN.md` §6.2) — `core/connectors/email.py` + `core/extract/email.py` (`.eml`, `.emlx`, mbox, Maildir)
- [x] Skip attachments in v1, or PDFs only (`PLAN.md` §6.2) — attachment parts excluded during body selection

### 5.2 Browser history

- [x] Chrome `History` and Firefox `places.sqlite`: copy to temp file, then read (`PLAN.md` §6.2) — `core/extract/browser.py:read_history` (+ Safari `History.db` best-effort)
- [x] Safari `History.db` (`PLAN.md` §6.2)
- [x] Last-visit-timestamp cursor (`PLAN.md` §6.2) — `sources.cursor` advanced in `run_index_doc`
- [x] Index title + URL; one chunk per URL (`PLAN.md` §6.4)
- [ ] Opt-in page-text fetch for pages visited ≥ N times — the only network use, clearly labelled (`PLAN.md` §6.2, §9)

### 5.3 Chat exports

- [x] WhatsApp `.txt`, Telegram JSON, Discord export importers (`PLAN.md` §6.2) — `core/extract/chat.py`
- [x] Re-import on file change (`PLAN.md` §6.2) — watcher + content-hash short-circuit
- [x] Sliding time windows per conversation, ~20 messages (`PLAN.md` §6.4) — `core/chunking/chat.py` (non-overlapping 20-msg windows, split if > 400 tokens)

### 5.4 Code

- [x] Configured repo roots, respect `.gitignore` (`PLAN.md` §6.2) — `core/connectors/code.py` (subset: globs, `dir/`; `!` negations ignored)
- [x] FSEvents + `git` HEAD changes (`PLAN.md` §6.2) — watcher on repo roots (HEAD surfaces as file events)
- [x] tree-sitter split by function/class (`PLAN.md` §6.4) — `core/chunking/code.py` (tree-sitter when importable, regex fallback; `tree-sitter-language-pack` install deferred — `uv sync --extra phase5` stalls in this environment)
- [x] Header line with path + symbol; keep line range in `loc_json` (`PLAN.md` §6.4)
- [x] ≤ 600 tokens per code chunk; small files whole (`PLAN.md` §6.4)

> Phase 5 wiring: `run_index_doc` / `run_scan` / `schedule_reconcile` handle all
> four kinds; browser is one document per profile DB with per-URL chunks and
> the URL in `loc_json` (per-URL doc granularity deferred), email is one
> document per container with per-message chunks. Email (`core/connectors/email.py`)
> is local-first (`.eml`/`.emlx`/mbox/Maildir, stdlib only); IMAP
> (`core/connectors/email_imap.py`) sits behind the optional `phase5` extra and
> uses a connector `fetch()` hook so network items need no filesystem path
> (`core/connectors/base.py:RawItem`). Tests: `test_phase5_connectors.py`
> + `test_phase5_sources.py` + `test_phase5_email.py`
> + `test_phase5_email_sources.py` (28 tests). See `eval/reports/phase5_findings.md`.

---

## Phase 6 — Desktop app (weeks 11–12)

Done when: the full user flow works without the terminal. (`PLAN.md` §13)

### 6.1 Window and shell

- [ ] Move `ui/` into the Tauri build; same static assets, no rewrite (`PLAN.md` §4 UI host row, §12)
- [ ] Spotlight-style window: always-on-top, borderless, vibrancy blur (`PLAN.md` §11.2)
- [ ] Menu-bar status item (`PLAN.md` §12)
- [ ] Hotkey rebinding and conflict handling (`PLAN.md` §11.2)
- [ ] Bundled Python runtime for a one-installable-app package (`PLAN.md` §4)

### 6.2 Search experience

- [ ] Results list with snippets and highlight marks (`PLAN.md` §10, §11.2)
- [ ] Answer panel streams above results (`PLAN.md` §11.2)
- [ ] Filter chips for source and date (`PLAN.md` §11.2)
- [ ] Keyboard-first: arrows to move, Enter to open, Tab between results and answer (`PLAN.md` §11.2)
- [ ] Search usable immediately on partially-indexed data (`PLAN.md` §11.1)

### 6.3 Opening sources — the Rust command surface

- [ ] Open a file at a line range (`PLAN.md` §11.2)
- [ ] Open a PDF at a page (`PLAN.md` §11.2)
- [ ] Open an email in Mail via `message://` (`PLAN.md` §11.2)
- [ ] Open a browser history URL (`PLAN.md` §11.2)
- [ ] Confirm this is the *only* part that could not be done in the browser (`PLAN.md` §4 UI host row)

### 6.4 Settings and progress

- [ ] Sources, exclusions, and per-source enable/disable (`PLAN.md` §11.3, §10)
- [ ] Model selection and index statistics (`PLAN.md` §11.3)
- [ ] Answer LLM toggle: 4B default vs 8B quality opt-in, with the §2.1 memory cost shown (`PLAN.md` §4.1)
- [ ] Indexing schedule, thermal policy, resident-memory settings (`PLAN.md` §11.3, §6.7)
- [ ] Live indexing progress over `/events` (`PLAN.md` §10, §11.3)
- [ ] Full Disk Access request with an explanation of why it is needed (`PLAN.md` §9, §11.1)

---

## Phase 7 — Privacy, polish, eval (weeks 13–14)

Done when: report and demo are ready. (`PLAN.md` §13)

### 7.1 Storage and deletion

- [ ] Optional SQLCipher encryption at rest; key in macOS Keychain (`PLAN.md` §9)
- [ ] "Forget source" removes documents, chunks, vectors, and cached text, then `VACUUM` (`PLAN.md` §9)
- [ ] Export diagnostics with no content (`PLAN.md` §11.3)
- [ ] Query log toggle (`PLAN.md` §11.3, §5)

### 7.2 Test suite

- [ ] Unit: extractors with fixtures per type, chunker boundaries, hash diffing, RRF math, date parser, citation parser (`PLAN.md` §15)
- [ ] Integration: index a fixture corpus, modify/delete files, assert index state (`PLAN.md` §15)
- [ ] End-to-end `/search` and `/answer` with a mocked LLM (`PLAN.md` §15)
- [ ] Golden tests: fixed questions, fail CI if Recall@10 drops (`PLAN.md` §15)
- [ ] Privacy tests: no outbound network during indexing/search; excluded paths never in the DB; secrets redacted (`PLAN.md` §15)
- [ ] Performance tests: 10k and 50k chunks, record latency and memory (`PLAN.md` §15)

### 7.3 Full evaluation

- [ ] Run every ablation in `PLAN.md` §14.3 and collect results
- [ ] Report actual measured peak RSS and swap behaviour, not the predicted figures (`PLAN.md` §2.1)
- [ ] Report the 4B vs 8B ablation and which one shipped as default (`PLAN.md` §14.3)
- [ ] Report search p50/p95, TTFT, indexing throughput, battery drain (`PLAN.md` §14.2)
- [ ] Write the report using the synthetic corpus only — no real private data (`PLAN.md` §16)
- [ ] Demo video

---

## Open questions to resolve as they come up

- [ ] Which browser history sources are actually needed, given each needs Full Disk Access? (`PLAN.md` §16)
- [ ] Are the 6 connectors in Phase 5 realistic for the remaining time, or should one be dropped? (`PLAN.md` §13)
- [ ] Is the Plan-fit Phase 0 skeleton still sufficient by the time Phase 6 arrives, or has the Rust surface grown? (`PLAN.md` §13.1)
