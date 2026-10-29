"""Configuration and model registry (PLAN.md §4.1, §4.1 "Locked model configuration").

Every model choice, path, and runtime flag lives here so that swapping a model is a
config change and never a code change. Two rules from §4.1 are load-bearing and must
not drift from this file:

* ``torch`` never enters the venv. Embeddings and reranking are ONNX via fastembed.
* Context stays at 8192. Do not raise it to chase advertised long context.

Values are overridable through ``STACKSEARCH_*`` environment variables, which is how
the Tauri host hands the per-launch API token to the core service (§9).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

# llama.cpp release pin, read by scripts/fetch_llama_server.sh (PLAN.md §4.1).
LLAMA_TAG = "b11462"

# --------------------------------------------------------------------------------
# Embedding model (PLAN.md §4.1)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingModel:
    """An embedding model plus everything needed to reproduce its vectors."""

    key: str
    # Canonical id. Also written to meta.embedding_model and compared on open, because
    # changing it invalidates the vector index (PLAN.md §5).
    model_id: str
    dim: int
    # e5 models are trained with asymmetric query/passage prefixes. Omitting them
    # silently costs retrieval quality, so they are part of the model definition rather
    # than a call-site detail.
    query_prefix: str
    passage_prefix: str
    # fastembed catalog entry. When None the model is registered at import time via
    # add_custom_model() from `source_repo` + `model_file`.
    catalog_id: str | None
    source_repo: str | None
    model_file: str
    size_gb: float
    note: str = ""


EMBEDDING_MODELS: dict[str, EmbeddingModel] = {
    "multilingual-e5-base": EmbeddingModel(
        key="multilingual-e5-base",
        model_id="intfloat/multilingual-e5-base",
        dim=768,
        query_prefix="query: ",
        passage_prefix="passage: ",
        catalog_id=None,  # NOT in fastembed.list_supported_models(); see probe_embedding.py
        source_repo="Xenova/multilingual-e5-base",
        model_file="onnx/model_int8.onnx",
        size_gb=0.28,
        note=(
            "§4.1 default. Registered via add_custom_model because fastembed has no "
            "built-in entry. Xenova's export is used rather than the official repo's "
            "only quantized file, model_qint8_avx512_vnni.onnx, which is x86-only."
        ),
    ),
    # §14.3 ablation arms. Both are native fastembed catalog entries.
    "bge-base-en-v1.5": EmbeddingModel(
        key="bge-base-en-v1.5",
        model_id="BAAI/bge-base-en-v1.5",
        dim=768,
        query_prefix="",
        passage_prefix="",
        catalog_id="BAAI/bge-base-en-v1.5",
        source_repo=None,
        model_file="onnx/model_quantized.onnx",
        size_gb=0.21,
        note="§14.3 ablation arm: English-only, same 768d as the default.",
    ),
    "bge-small-en-v1.5": EmbeddingModel(
        key="bge-small-en-v1.5",
        model_id="BAAI/bge-small-en-v1.5",
        dim=384,
        query_prefix="",
        passage_prefix="",
        catalog_id="BAAI/bge-small-en-v1.5",
        source_repo=None,
        model_file="onnx/model_quantized.onnx",
        size_gb=0.07,
        note=(
            "§14.3 ablation arm: low-power / battery mode, and exercises the "
            "dimension-rebuild path in PLAN.md §5."
        ),
    ),
}

DEFAULT_EMBEDDING = "multilingual-e5-base"

# --------------------------------------------------------------------------------
# Reranker (PLAN.md §4.1, §7.3)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RerankerModel:
    key: str
    model_id: str
    #: Non-null when fastembed has no catalog entry and the model must be registered
    #: explicitly. When set, ``model_file`` picks the ONNX variant to load.
    catalog_id: str | None
    model_file: str
    size_gb: float
    license: str = ""
    note: str = ""


RERANKER_MODELS: dict[str, RerankerModel] = {
    "jina-reranker-v2-base-multilingual": RerankerModel(
        key="jina-reranker-v2-base-multilingual",
        model_id="jinaai/jina-reranker-v2-base-multilingual",
        catalog_id="jinaai/jina-reranker-v2-base-multilingual",
        # PLAN.md §4.1 budgets ~280 MB, which is achievable: the repo's int8 export is
        # 279.6 MB. fastembed's catalog entry hardcodes the fp32 `onnx/model.onnx`
        # (1114 MB), so the model is registered explicitly to get the quantized file.
        # This keeps §2.1's indexing-state budget honest.
        model_file="onnx/model_int8.onnx",
        size_gb=0.28,
        license="cc-by-nc-4.0",
        note=(
            "§4.1 locked reranker. Interface reserved in Phase 2 (TODO 2.2), enabled in "
            "Phase 3 (TODO 3.3). Non-commercial licence — fine for coursework, but a "
            "commercial use of this project would need a different reranker."
        ),
    ),
}

DEFAULT_RERANKER = "jina-reranker-v2-base-multilingual"

# --------------------------------------------------------------------------------
# Answer LLM (PLAN.md §2.1, §4.1)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMModel:
    key: str
    label: str
    role: str
    hf_repo: str
    gguf_filename: str
    size_gb: float
    # Peak system memory estimate from the PLAN.md §2.1 budget table, for the settings
    # UI to show alongside the 4B / 8B toggle (TODO 6.4).
    peak_system_gb: float


LLM_MODELS: dict[str, LLMModel] = {
    "4B": LLMModel(
        key="4B",
        label="Qwen3-4B Q5_K_M",
        role="default",
        hf_repo="Qwen/Qwen3-4B-GGUF",
        gguf_filename="Qwen3-4B-Q5_K_M.gguf",
        size_gb=3.0,
        peak_system_gb=9.7,
    ),
    "8B": LLMModel(
        key="8B",
        label="Qwen3-8B Q5_K_M",
        role="quality opt-in",
        hf_repo="Qwen/Qwen3-8B-GGUF",
        gguf_filename="Qwen3-8B-Q5_K_M.gguf",
        size_gb=5.5,
        peak_system_gb=12.2,
    ),
}

DEFAULT_LLM = "4B"

# --------------------------------------------------------------------------------
# llama-server runtime (PLAN.md §4.1 invocation, §6.7 lazy load / idle unload)
# --------------------------------------------------------------------------------

LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8081

#: §4.1 invocation, verbatim. `-c` is the load-bearing 8k context cap; `-ctk/-ctv q8_0`
#: halve the KV cache to fit the §2.1 budget; `-np 1` because a single-user local app
#: gains nothing from parallel slots.
LLAMA_BASE_ARGS: tuple[str, ...] = (
    "--host",
    LLAMA_HOST,
    "--port",
    str(LLAMA_PORT),
    "-c",
    "8192",
    "-ngl",
    "99",
    "-np",
    "1",
    "-ctk",
    "q8_0",
    "-ctv",
    "q8_0",
    "--no-webui",
)

#: Health-check poll cadence while waiting for the server to accept requests.
LLAMA_STARTUP_POLL_S = 0.25
LLAMA_STARTUP_TIMEOUT_S = 120.0

# --------------------------------------------------------------------------------
# Retrieval / answering constants (PLAN.md §6.6, §7.2, §8.2)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Retrieval:
    """Retrieval tuning from PLAN.md §7.2 and §8.2."""

    bm25_top_k: int = 50
    vector_top_k: int = 50
    #: Reciprocal Rank Fusion constant. §7.2 fixes this at 60.
    rrf_k: int = 60
    #: Candidates handed to the reranker before fusion cutoff (PLAN.md §7.3).
    rerank_top_k: int = 30
    #: §7.2: at most 2 chunks per document so one long PDF cannot drown the list.
    max_chunks_per_doc: int = 2
    #: §8.2: 6-8 chunks, ~3-4k tokens.
    answer_min_chunks: int = 6
    answer_max_chunks: int = 8
    #: Hard ceiling on tokens fed to the LLM. §4.1 caps context at 8192 and the §8.2
    #: prompt is 3-4k; this leaves headroom rather than trusting either number.
    answer_context_token_budget: int = 4096


# --------------------------------------------------------------------------------
# Indexing constants (PLAN.md §6.3, §6.4, §6.6)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Indexing:
    """Ingestion tuning from PLAN.md §6.3, §6.4, §6.6."""

    #: §6.6: embed in batches of 32. Also the granularity at which the indexer can be
    #: paused for a query (§6.7 state 2), bounding extra latency to ~0.5s.
    embed_batch_size: int = 32
    #: §6.3: skip files larger than this.
    max_file_bytes: int = 50 * 1024 * 1024
    #: §6.4 prose chunk target range, in tokens.
    prose_chunk_min_tokens: int = 300
    prose_chunk_max_tokens: int = 500
    prose_overlap_ratio: float = 0.15
    #: §6.4 email and chat ceilings.
    email_chunk_max_tokens: int = 500
    chat_window_messages: int = 20
    chat_chunk_max_tokens: int = 400
    #: §6.4 code ceilings.
    code_chunk_max_tokens: int = 600


# --------------------------------------------------------------------------------
# Job queue (PLAN.md §6.6)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Queue:
    """Job priorities and retry policy from PLAN.md §6.6."""

    #: Lower number = higher priority.
    priority_reindex: int = 1
    priority_live_change: int = 2
    priority_bulk: int = 5
    priority_reconcile: int = 9
    #: §6.6: after 3 failures mark the document `error` and surface it in settings.
    max_attempts: int = 3
    backoff_base_s: float = 5.0


# --------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------


class Settings(BaseSettings):
    """Runtime settings, overridable via ``STACKSEARCH_*`` env vars."""

    model_config = SettingsConfigDict(env_prefix="STACKSEARCH_", extra="ignore")

    # -- storage (PLAN.md §9) ----------------------------------------------------
    app_name: str = "stacksearch"
    #: PLAN.md §9: ~/Library/Application Support/<app>/
    data_dir: Path = Field(
        default_factory=lambda: Path.home()
        / "Library"
        / "Application Support"
        / "stacksearch"
    )
    #: Debug logs live in ~/Library/Logs (macOS convention), not the data dir, so they
    #: are easy to find and can be pruned without touching the index. Overridable with
    #: STACKSEARCH_LOG_DIR; core/debug_log.py resolves the same path for early logging.
    logs_dir: Path = Field(
        default_factory=lambda: Path.home() / "Library" / "Logs" / "StackSearch"
    )
    models_dir: Path = REPO_ROOT / "models"
    vendor_dir: Path = REPO_ROOT / "vendor"

    # -- API (PLAN.md §9, §10) --------------------------------------------------
    #: PLAN.md §9: bind to loopback only. Never 0.0.0.0.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    #: §9: random per-launch token required on every route. Generated when absent so a
    #: bare `uv run core.api.run` is still authenticated.
    api_token: str = field(
        default_factory=lambda: os.environ.get("STACKSEARCH_TOKEN") or secrets.token_urlsafe(32)
    )

    # -- models (PLAN.md §4.1) --------------------------------------------------
    embedding_model: str = DEFAULT_EMBEDDING
    answer_llm: str = DEFAULT_LLM
    reranker: str = DEFAULT_RERANKER
    #: §6.7: default is *keep resident*; the process is only killed after this idle
    #: window. Killing on every transition would pay the ~4s cold start each session.
    llm_unload_after_idle_minutes: int = 60

    #: Qwen3's chat template enables reasoning by default. §8.1 is extractive
    #: summarisation with citation tagging, so reasoning contributes nothing, and it is
    #: actively harmful to the budget: measured on the 4B, a 200-token ceiling was
    #: consumed entirely by `reasoning_content`, leaving the answer truncated with
    #: `finish_reason: "length"` and no citation. Disabling it returned `stop` in 25
    #: tokens with the correct cited answer.
    #:
    #: Kept config-driven so §14.3 can ablate it rather than assume the choice.
    llm_enable_thinking: bool = False

    # -- paths ------------------------------------------------------------------
    llama_server_path: Path | None = None

    retrieval: Retrieval = field(default_factory=Retrieval)
    indexing: Indexing = field(default_factory=Indexing)
    queue: Queue = field(default_factory=Queue)

    @field_validator("api_host")
    @classmethod
    def _must_be_loopback(cls, value: str) -> str:
        """Reject any non-loopback bind address (PLAN.md §9).

        The API exposes the user's entire personal index, so "reachable from the
        network" is not a tunable. A stray ``STACKSEARCH_API_HOST`` should fail loudly
        at startup rather than silently exposing the index on a shared network.
        """
        allowed = {"127.0.0.1", "::1", "localhost"}
        if value not in allowed:
            raise ValueError(
                f"api_host must be a loopback address ({', '.join(sorted(allowed))}), "
                f"got {value!r}. PLAN.md §9 requires binding to 127.0.0.1 only."
            )
        return value

    @property
    def index_db_path(self) -> Path:
        """SQLite file holding metadata, FTS5, and the vector index (PLAN.md §5)."""
        return self.data_dir / "index.db"

    @property
    def blob_cache_dir(self) -> Path:
        """Extracted text cache (PLAN.md §3)."""
        return self.data_dir / "blobs"

    @property
    def fastembed_cache_dir(self) -> Path:
        return self.data_dir / "fastembed"

    @property
    def llama_server_bin(self) -> Path:
        """Resolved path to the pinned ``llama-server`` binary."""
        if self.llama_server_path:
            return self.llama_server_path
        return self.vendor_dir / "llama-server" / f"llama-{LLAMA_TAG}" / "llama-server"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.blob_cache_dir, self.models_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def get_settings(**overrides) -> Settings:
    """Build settings, applying explicit overrides last."""
    return Settings(**overrides)


# --------------------------------------------------------------------------------
# Model lookup helpers
# --------------------------------------------------------------------------------


def embedding_model(key: str | None = None) -> EmbeddingModel:
    """Resolve an embedding model by key, or the configured default."""
    settings = get_settings()
    return EMBEDDING_MODELS[key or settings.embedding_model]


def llm_model(key: str | None = None) -> LLMModel:
    """Resolve an answer LLM by key ('4B' / '8B'), or the configured default."""
    settings = get_settings()
    return LLM_MODELS[key or settings.answer_llm]


def model_repo_id(model: LLMModel | EmbeddingModel) -> str:
    """HF repo id for a model."""
    return model.hf_repo if isinstance(model, LLMModel) else (model.source_repo or model.model_id)


def model_path(key: str | None = None) -> Path:
    """Local path to a downloaded GGUF."""
    settings = get_settings()
    model = LLM_MODELS[key or settings.answer_llm]
    return settings.models_dir / model.gguf_filename


def llama_args(model_path_: Path | str | None = None, api_key: str | None = None) -> list[str]:
    """Full argv for llama-server, from the §4.1 flags.

    ``api_key`` becomes ``--api-key``, which §9 uses so the LLM endpoint is not
    reachable by other local processes.
    """
    settings = get_settings()
    mp = Path(model_path_) if model_path_ else model_path()
    argv = [
        str(settings.llama_server_bin),
        "-m",
        str(mp),
        *LLAMA_BASE_ARGS,
    ]
    if api_key:
        argv += ["--api-key", api_key]
    return argv


llm_choice = Literal["4B", "8B"]
