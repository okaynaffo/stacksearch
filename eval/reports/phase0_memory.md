# Phase 0 — memory budget validation (PLAN.md §2.1, TODO 0.2)

Measured, not predicted. Every row is a real `llama-server` process serving a
§8.2-shaped prompt (8 numbered sources + question) at the §4.1 flags.

- Host: arm64 / 26.5.1 / 10 cores / 16 GB
- llama.cpp pin: `b11462` (llama-server)
- Config: reasoning off (§4.1 default), context 8192, KV cache compared across precisions

## Results

TTFT is reported twice because repeating one prompt lets llama.cpp serve it from
its KV cache. **Cold** is the first request and is the honest figure for a fresh
question; **warm** is the median of the later, prompt-cached ones.

| Model | KV cache | Reasoning | TTFT cold (s) | TTFT warm (s) | Gen tok/s | Peak server RSS (MB) | Peak system used (MB) | Swap Δ (MB) |
|---|---|---|---|---|---|---|---|---|
| Qwen3-4B Q5_K_M | q8_0 | off | 0.673 | 0.037 | 37.3 | 3547.7 | 10729.9 | +0.0 |
| Qwen3-4B Q5_K_M | f16 | off | 0.663 | 0.036 | 38.7 | 4081.0 | 11277.9 | +0.0 |
| Qwen3-4B Q5_K_M | q8_0 | on | None | None | 32.3 | 3543.0 | 11473.3 | +0.0 |
| Qwen3-4B Q5_K_M | f16 | on | None | None | 35.5 | 4084.1 | 11738.5 | +0.0 |

## Against the §2.1 predictions

Two different questions: did the specific predicted number hold, and is the machine
within budget? They can disagree, so both are reported.

| Prediction | Plan | Measured | Verdict |
|---|---|---|---|
| Qwen3-4B Q5_K_M peak system memory | 9.7 GB | 10.5 GB | WITHIN BUDGET — +3.5 GB headroom vs the 14.0 GB ceiling; 0.8 GB over prediction |
| Qwen3-4B Q5_K_M KV cache, q8_0 vs f16 at -c 8192 | 0.6 GB saved | 0.52 GB saved (3548 → 4081 MB) | CONFIRMED |
| swap activity under sustained load | none | none | CONFIRMED |

## Finding: reasoning mode starves the answer (supports the §4.1 decision)

At the §8.2 prompt size (128 token ceiling per run) 2 of 2 reasoning-on runs produced **no answer token at all** — the entire budget went to `reasoning_content`, so time-to-first-token is undefined. With reasoning off the same runs return a cited answer in ~0.67 s.

This is not a quality trade-off to be weighed; at the §8.2 budget, reasoning mode does not return an answer. §4.1's default is therefore load-bearing, and §8.3's citation verifier would have had nothing to verify.

## Reading this

- **Peak system used** counts wired + active + compressor pages for the whole
  machine, so it is comparable to the §2.1 table's totals (macOS + apps + models).
- **Swap Δ** is the number that matters for the fanless constraint. A positive
  delta means the SSD serviced page faults, which §2.1 is designed to avoid.
- **Reasoning off** is the shipped default; the `on` row is the §14.3 ablation.
