# StackSearch

A private, local-first personal search engine. Design decisions live in
[PLAN.md](./PLAN.md); execution tasks live in [TODO.md](./TODO.md).

Nothing leaves the device: no telemetry, no cloud calls. The core service binds to
`127.0.0.1` and requires a per-launch token on every route.

## Layout

```
core/     Python core service (FastAPI) — PLAN.md §12
app/ui/   React + Tailwind, one static build shared by both hosts — PLAN.md §4
app/src-tauri/  Rust: hotkey, supervises the core service, deep links
eval/     evaluation datasets, scripts, reports — PLAN.md §14
scripts/  dev setup, model download, llama-server pin, benchmarks
tests/
models/   GGUF weights (gitignored)
vendor/   pinned llama-server binary (gitignored)
```

## Setup

```bash
uv sync                  # runtime + dev deps
uv sync --extra phase5   # adds the Phase 5 connector deps
```

Python 3.12 is pinned in `.python-version`. `cargo` lives in `~/.cargo/bin`, which is not
on the default `PATH` for GUI shells; `scripts/dev.sh` and `scripts/ci.sh` add it.

## Phase 0 quickstart

```bash
scripts/fetch_llama_server.sh          # pinned llama.cpp build (b11462, sha256-verified)
uv run python scripts/download_models.py   # Qwen3-4B Q5_K_M into models/
uv run python scripts/probe_embedding.py   # §4.1 embedding pre-flight
uv run python scripts/bench_llm.py         # §2.1 memory budget validation
uv run python scripts/smoke_models.py      # embed + grounded answer, end to end
```

Then run the app, either host:

```bash
scripts/dev.sh browser    # core service + Vite at 127.0.0.1:8000  (Phases 1-5 dev loop)
scripts/dev.sh app        # core service, spawned and supervised by the Tauri shell
scripts/dev.sh test       # every CI gate locally
```

The core service prints its per-launch token on startup:

```
core service listening on http://127.0.0.1:8000
  index : ~/Library/Application Support/stacksearch/index.db
  token : <43 chars>
```

Open <http://127.0.0.1:8000/?token=…> in a browser, or use the Tauri window, which is
handed the token automatically.

### Environment variables

| Variable | Purpose |
|---|---|
| `STACKSEARCH_TOKEN` | Per-launch API token (§9). Generated if unset; the Tauri host sets it for the child |
| `STACKSEARCH_DATA_DIR` | Index location, default `~/Library/Application Support/stacksearch` |
| `STACKSEARCH_API_PORT` | Core service port, default `8000` |
| `STACKSEARCH_HOTKEY` | Global hotkey spec, default `Cmd+Shift+Space` (TODO 6.3) |
| `STACKSEARCH_ACCESS_LOG` | `1` to log every HTTP request — the fastest way to diagnose the webview |
| `FASTEMBED_CACHE_PATH` | ONNX model cache, default `.cache/fastembed` |
| `STACKSEARCH_REPO_ROOT` | Repository root, so the Tauri shell can find `uv` |

> Migrating from `local-search`? Rename `~/Library/Application Support/local-search`
> to `~/Library/Application Support/stacksearch`, and `LOCAL_SEARCH_*` env vars to
> `STACKSEARCH_*`. No re-index needed — the SQLite index moves as-is.

## Reports

- [`eval/reports/phase0_findings.md`](eval/reports/phase0_findings.md) — what Phase 0
  measured, including the three places reality disagreed with `PLAN.md`.
- [`eval/reports/phase0_memory.md`](eval/reports/phase0_memory.md) — §2.1 validation by
  measurement.

## Non-negotiables

- **No PyTorch, anywhere.** ONNX only. `scripts/check_no_torch.py` fails CI if it appears.
- **Context stays at 8k.** Do not raise it to chase advertised long context (PLAN.md §4.1).
- **Qwen3 reasoning stays off.** Reasoning consumes the §8.2 token budget before producing
  an answer, which leaves the §8.3 citation verifier nothing to verify.
- **`127.0.0.1` only.** The API token is per-launch and required on every route.