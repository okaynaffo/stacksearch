# Phase 0 findings (TODO 0.1 – 0.7)

Measured on the target machine: MacBook Air M5, 16 GB, 10-core, fanless, macOS 26.5.1.
Everything below was observed while building Phase 0, not inferred. Where a finding
contradicts `PLAN.md`, the plan has been updated; where it could not be resolved, that is
stated plainly rather than papered over.

---

## 1. The embedding pre-flight failed, and did not need the fallback chain (TODO 0.1)

`PLAN.md` §4.1 asks whether `intfloat/multilingual-e5-base` appears in
`fastembed.list_supported_models()`. It does not — fastembed 0.8.1 ships 37 ONNX text
models and no e5 variant at all.

The fallback chain in §4.1 (`sentence-transformers` → MLX → `bge-base-en-v1.5`) turned out
to be unnecessary. `TextEmbedding.add_custom_model(...)` registers an arbitrary HF repo
against an explicit ONNX export, which keeps the §4.1 default unchanged and keeps the
no-PyTorch constraint. Measured: 768d, int8, L2-normalized, paired cosine similarity
0.83–0.87 on a relevance probe, on Apple Silicon.

Source is `Xenova/multilingual-e5-base`, not the official repo. The official repo's only
quantized export is `model_qint8_avx512_vnni.onnx`, which is **x86-only** and cannot run
on this hardware. Anyone taking the §4.1 fallback chain for granted would have hit this.

> `PLAN.md` §4.1 updated with the measured outcome.

## 2. The reranker size was right, but not reachable by default (TODO 0.1)

§4.1 budgets `jina-reranker-v2-base-multilingual` at ~280 MB, and that figure is correct:
`onnx/model_int8.onnx` is 279.6 MB. However fastembed's catalog entry hardcodes
`onnx/model.onnx`, the fp32 export at **1114 MB**.

So the same registration pattern used for the embedder is needed for the reranker, or the
§2.1 indexing state is ~830 MB heavier than budgeted. Config records
`model_file="onnx/model_int8.onnx"` for this reason.

> `PLAN.md` §4.1 updated. Note the reranker is **CC-BY-NC-4.0** (non-commercial), which is
> fine for coursework but would need substituting for a commercial deployment.

## 3. Qwen3's reasoning mode silently destroys the answer (TODO 0.6)

Not mentioned anywhere in `PLAN.md`, and it breaks §8.1 outright.

Qwen3's chat template enables reasoning by default. On the §8.2-shaped prompt with a
200-token ceiling, the **entire budget** went to `reasoning_content`. The answer came back
as `finish_reason: "length"`, truncated to `"The internship stipend is 25,"`, with **no
citation at all** — which means §8.3's citation verifier had nothing to verify and the
failure looked like a prompt bug rather than a sampling-mode problem.

With `chat_template_kwargs: {"enable_thinking": false}`:

| | reasoning on | reasoning off |
|---|---|---|
| finish reason | `length` | `stop` |
| answer tokens | 200 (all reasoning) | 25 |
| citations | none | `[1]` |
| TTFT | never emitted an answer token | 0.27 s |

Wired as `llm_enable_thinking` (default `False`), config-driven so §14.3 can ablate it
rather than assume it.

> `PLAN.md` §4.1 and §8.1 updated.

## 4. §2.1's memory budget holds, with one correction (TODO 0.2)

Full results in [phase0_memory.md](./phase0_memory.md).

| Claim | Predicted | Measured | |
|---|---|---|---|
| 4B peak system memory | 9.7 GB | **10.5 GB** | 0.8 GB over prediction, +3.5 GB under the 14 GB ceiling |
| `q8_0` KV saving at `-c 8192` | 0.6 GB | 0.52 GB | confirmed |
| Swap under sustained load | none | none | confirmed |

The 9.7 GB figure is a slight underestimate. The budget is not threatened — 3.5 GB of
headroom remains, which is the margin §2.1 exists to preserve — but the report separates
"matched the prediction" from "fits the machine" rather than reporting an overshoot as a
confirmation.

Also worth recording: **TTFT must be measured cold.** Repeating one prompt lets llama.cpp
serve it from its KV cache, and the median across repeat requests (0.037 s) is an order of
magnitude below the cold figure (0.673 s). An earlier run of the benchmark reported
0.036 s for a warm-cache request and would have made §14.2's metric meaningless.

## 5. The desktop integration had two silent failure modes (TODO 0.7)

This is the section §13.1 exists to produce, and it earned its place. Both bugs produced a
window that looked plausible and did the wrong thing.

### 5a. A second `.setup()` silently discarded the first

The window never appeared at all. Tauri v2 honours **only the last** `.setup()` on the
builder, so adding one for supervision threw away window creation and the hotkey
registration with no warning and no compile error. Both are now in one closure.

### 5b. Tauri's own page load overwrote the tokenised URL

The window rendered, loaded assets from the core service, and then showed nothing but
`Unauthorized`. The cause: a config-declared window begins loading its URL immediately, and
that load can complete *after* an explicit `navigate()`, replacing it. The page arrived at
the right server but without `?token=`, so every API call 401'd.

Fixed by declaring no window in `tauri.conf.json` and building it with
`WebviewWindowBuilder` at the service URL, so there is no competing load. Access logging
(`STACKSEARCH_ACCESS_LOG=1`) is what made this visible in one run rather than an hour.

### 5c. `run.py` printed a token the app did not accept

`run.py` generated a token, then uvicorn imported the app module, which built its own
`Settings` and generated a *different* one. The printed token looked correct and was
rejected by every request. `run.py` now publishes the token to the environment first.
Regression test: `test_printed_token_is_the_one_validated`.

## 6. Open: the global hotkey cannot register in this environment (TODO 0.7)

`RegisterEventHotKey` fails for **every** combination tried — `Cmd+Space`,
`Cmd+Shift+Space`, `Alt+Space`, `Cmd+Shift+J`, `Ctrl+Alt+K`, `Cmd+Shift+L`. Six different
keys failing rules out a conflict.

Ruled out:
- Not the GUI session: `launchctl managername` reports `Aqua` and the process is registered
  with `lsappinfo` as frontmost.
- Not the missing `.app` bundle: a real bundle was built (37.7 MiB, plus a DMG) and failed
  identically.
- Not signing: an ad-hoc signature (`Identifier=app.stacksearch.desktop`) failed identically.

Most likely macOS 26.5 requiring **Input Monitoring** permission, which can only be granted
interactively and which could not be inspected here (`TCC.db` needs Full Disk Access).

**This needs a decision from the user.** Either grant Input Monitoring to the app in
System Settings → Privacy & Security → Input Monitoring, or accept the hotkey as
environment-blocked for Phase 0 and revisit it on the packaged app.

The code is correct and fails loudly rather than silently, which is the behaviour §13.1
wants: a dead hotkey reports itself instead of looking like a broken app. The hotkey is
also configurable via `STACKSEARCH_HOTKEY` (e.g. `Cmd+Shift+l`) so no rebuild is needed to
change it.

## 7. Notes for later phases

- **httpx's `ASGITransport` buffers the whole response body**, so it cannot verify an
  infinite SSE stream at all — it never returns. The `/events` end-to-end test runs a real
  uvicorn server instead. Worth remembering for the Phase 4 `/answer` streaming test.
- **FastAPI 0.142 does not flatten included routers.** `app.routes` contains an
  `_IncludedRouter` whose real routes hang off `original_router.routes`, so enumerating
  paths needs to recurse. A flat scan found nothing and would have made the "every route
  requires a token" test vacuously true.
- **`huggingface-hub` is capped below 2.0** by fastembed 0.8.1, so the venv is on 1.33.0.
- **Phase 5 connectors are pinned but not installed.** `uv sync --extra phase5` pulls IMAP,
  mbox, HTML, and tree-sitter deps when that phase needs them.
