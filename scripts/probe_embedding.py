#!/usr/bin/env python3
"""Embedding model pre-flight (PLAN.md §4.1, TODO 0.1).

The locked §4.1 default is ``intfloat/multilingual-e5-base`` at 768d via fastembed.
fastembed does *not* ship that model in ``list_supported_models()``, so this script
records that fact and then resolves the model through the ``add_custom_model``
registration path rather than silently dropping to a different model.

If custom registration cannot be made to work on this machine, the §4.1 fallback chain
applies and this script walks it in order:

    1. sentence-transformers with the onnxruntime backend
    2. MLX
    3. bge-base-en-v1.5 (English-only; honest fallback for an English + code corpus)

Run:

    uv run python scripts/probe_embedding.py
    uv run python scripts/probe_embedding.py --json   # machine-readable verdict
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The §4.1 default. Registered manually because fastembed has no built-in entry.
TARGET_MODEL = "intfloat/multilingual-e5-base"
TARGET_DIM = 768

# Xenova's export of the same weights ships a full ONNX set. The official repo's only
# quantized export is ``model_qint8_avx512_vnni.onnx``, which is x86-specific and will
# not run on Apple Silicon, so it is not an option here.
CUSTOM_SOURCE = "Xenova/multilingual-e5-base"
CUSTOM_MODEL_FILE = "onnx/model_int8.onnx"

# e5 models are trained with asymmetric prefixes. Omitting them costs retrieval quality
# in a way that is easy to misread as "the model is weak" (see PLAN.md §14.3).
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

PROBE_PAIRS = [
    (
        "What did the offer letter say about my start date?",
        "The offer letter states a start date of November 3.",
    ),
    (
        "How does the indexer handle a deleted file?",
        "Deleting a file cascades removal of its chunks from FTS5 and the vector index.",
    ),
    (
        "storage pricing tiers",
        "Pricing is split into hot, warm, and cold storage tiers billed per GB-month.",
    ),
]


@dataclass
class Verdict:
    ok: bool
    model: str
    dim: int
    source: str
    in_fastembed_catalog: bool
    query_prefix: str
    passage_prefix: str
    detail: str


def cosine(a, b) -> float:
    import numpy as np

    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def build_embedder():
    """Register the §4.1 model with fastembed and return a TextEmbedding."""
    from fastembed import TextEmbedding

    # In fastembed 0.8.1 these live in fastembed.common.model_description, not in
    # fastembed.common directly.
    from fastembed.common.model_description import ModelSource, PoolingType

    TextEmbedding.add_custom_model(
        model=TARGET_MODEL,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=CUSTOM_SOURCE),
        dim=TARGET_DIM,
        model_file=CUSTOM_MODEL_FILE,
    )
    return TextEmbedding(model_name=TARGET_MODEL)


def probe(embedder) -> dict:
    queries = [QUERY_PREFIX + q for q, _ in PROBE_PAIRS]
    passages = [PASSAGE_PREFIX + p for _, p in PROBE_PAIRS]

    t0 = time.perf_counter()
    q_vecs = [v for v in embedder.embed(queries, batch_size=32)]
    p_vecs = [v for v in embedder.embed(passages, batch_size=32)]
    elapsed = time.perf_counter() - t0

    dim = int(q_vecs[0].shape[-1])
    sims = [cosine(q, p) for q, p in zip(q_vecs, p_vecs, strict=True)]
    norms = [float((v**2).sum() ** 0.5) for v in q_vecs]

    return {"dim": dim, "similarities": sims, "norms": norms, "seconds": elapsed}


def try_fallback_chain() -> Verdict:
    """Walk the §4.1 fallback chain in order. Returns the first thing that works."""
    attempts = []

    # 1. sentence-transformers + onnxruntime backend.
    try:
        import sentence_transformers  # noqa: F401
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            TARGET_MODEL, backend="onnxruntime", model_kwargs={"file_name": CUSTOM_MODEL_FILE}
        )
        dim = int(model.get_sentence_embedding_dimension() or 0)
        attempts.append(f"sentence-transformers+onnxruntime: OK (dim={dim})")
        return Verdict(
            ok=True,
            model=TARGET_MODEL,
            dim=dim,
            source="sentence-transformers+onnxruntime",
            in_fastembed_catalog=False,
            query_prefix=QUERY_PREFIX,
            passage_prefix=PASSAGE_PREFIX,
            detail="; ".join(attempts),
        )
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"sentence-transformers+onnxruntime: FAILED ({type(exc).__name__}: {exc})")

    # 2. MLX.
    try:
        from mlx_embeddings import load  # type: ignore[import-not-found]

        model = load(TARGET_MODEL)
        dim = int(model.dim)
        attempts.append(f"mlx: OK (dim={dim})")
        return Verdict(
            ok=True,
            model=TARGET_MODEL,
            dim=dim,
            source="mlx",
            in_fastembed_catalog=False,
            query_prefix=QUERY_PREFIX,
            passage_prefix=PASSAGE_PREFIX,
            detail="; ".join(attempts),
        )
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"mlx: FAILED ({type(exc).__name__}: {exc})")

    # 3. bge-base-en-v1.5, natively supported. Needs no prefix pair.
    try:
        from fastembed import TextEmbedding

        embedder = TextEmbedding(model_name="BAAI/bge-base-en-v1.5")
        dim = int(next(iter(embedder.embed(["probe"]))).shape[-1])
        attempts.append(f"BAAI/bge-base-en-v1.5: OK (dim={dim})")
        return Verdict(
            ok=True,
            model="BAAI/bge-base-en-v1.5",
            dim=dim,
            source="fastembed-catalog",
            in_fastembed_catalog=True,
            query_prefix="",
            passage_prefix="",
            detail="; ".join(attempts),
        )
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"BAAI/bge-base-en-v1.5: FAILED ({type(exc).__name__}: {exc})")

    raise RuntimeError("Every §4.1 fallback failed:\n  " + "\n  ".join(attempts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    ap.add_argument("--cache-dir", default=None, help="override the fastembed cache dir")
    args = ap.parse_args()

    if args.cache_dir:
        Path(args.cache_dir).mkdir(parents=True, exist_ok=True)

    from fastembed import TextEmbedding

    catalog = {m["model"] for m in TextEmbedding.list_supported_models()}
    in_catalog = TARGET_MODEL in catalog

    log = (lambda *a: None) if args.json else print
    log(f"fastembed catalog size: {len(catalog)}")
    log(f"'{TARGET_MODEL}' in catalog: {in_catalog}")
    if not in_catalog:
        log("  -> not a built-in entry; resolving via add_custom_model (§4.1 pre-flight)")

    verdict: Verdict
    if in_catalog:
        embedder = TextEmbedding(model_name=TARGET_MODEL)
        result = probe(embedder)
        verdict = Verdict(
            ok=True,
            model=TARGET_MODEL,
            dim=result["dim"],
            source="fastembed-catalog",
            in_fastembed_catalog=True,
            query_prefix=QUERY_PREFIX,
            passage_prefix=PASSAGE_PREFIX,
            detail="",
        )
    else:
        try:
            embedder = build_embedder()
            result = probe(embedder)
        except Exception as exc:  # noqa: BLE001
            log(f"  -> custom registration FAILED ({type(exc).__name__}: {exc})")
            log("  -> walking the §4.1 fallback chain")
            verdict = try_fallback_chain()
            result = None
        else:
            if result["dim"] != TARGET_DIM:
                log(f"  -> dim mismatch: got {result['dim']}, §4.1 expects {TARGET_DIM}")
                verdict = try_fallback_chain()
                result = None
            else:
                verdict = Verdict(
                    ok=True,
                    model=TARGET_MODEL,
                    dim=result["dim"],
                    source=f"{CUSTOM_SOURCE}/{CUSTOM_MODEL_FILE}",
                    in_fastembed_catalog=False,
                    query_prefix=QUERY_PREFIX,
                    passage_prefix=PASSAGE_PREFIX,
                    detail="registered with add_custom_model",
                )

    if result:
        log("")
        log(f"resolved model : {verdict.model}")
        log(f"source         : {verdict.source}")
        log(f"dimension      : {verdict.dim}")
        log(f"query prefix   : {verdict.query_prefix!r}")
        log(f"passage prefix : {verdict.passage_prefix!r}")
        log(f"embed 6 texts in {result['seconds']:.2f}s")
        log("")
        log("paired cosine similarity (should be clearly > unrelated baseline):")
        for (q, _), sim in zip(PROBE_PAIRS, result["similarities"], strict=True):
            log(f"  {sim:.4f}  {q}")
        log(f"  norms: {[round(n, 4) for n in result['norms']]}")

        related = min(result["similarities"])
        if related < 0.5:
            log("")
            log(f"WARNING: weakest paired similarity is {related:.4f}; expected > 0.5.")
            log("Check the query:/passage: prefixes before trusting this model.")

    if args.json:
        print(json.dumps(asdict(verdict), indent=2))
    else:
        print("")
        print("VERDICT: OK" if verdict.ok else "VERDICT: FAILED")
        if verdict.detail:
            print(f"  {verdict.detail}")

    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
