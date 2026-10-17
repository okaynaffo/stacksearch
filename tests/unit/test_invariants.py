"""Tests for the PLAN.md §4.1 / §16 non-negotiables.

These are cheap enough to run on every change, which is the point: a torch dependency
reaching the venv is a silent ~2.5 GB regression against the §2.1 budget, and by the time
it shows up in a memory measurement the cause is far from obvious.
"""

from __future__ import annotations

import pytest

from scripts.check_no_torch import TORCH_DISTRIBUTIONS, check, locked_torch

pytestmark = pytest.mark.unit


def test_torch_is_not_installed():
    """§4.1 non-negotiable #1: no PyTorch in the runtime or the venv."""
    report = check()
    assert report["installed"] == [], f"torch present in venv: {report['installed']}"


def test_torch_is_not_in_the_lockfile():
    """Catches it before installation, which the installed check cannot."""
    assert locked_torch() == [], f"torch pinned in uv.lock: {locked_torch()}"


def test_torch_is_not_importable():
    report = check()
    assert report["importable"] is False, "a torch on sys.path can still be imported"


def test_guard_covers_the_related_distributions():
    """torchvision/torchaudio pull the same runtime, so they count as violations."""
    assert {"torch", "torchvision", "torchaudio"} <= TORCH_DISTRIBUTIONS


# --------------------------------------------------------------------------------
# the other §4.1 non-negotiable: context stays at 8k
# --------------------------------------------------------------------------------


def test_llama_context_is_capped_at_8192():
    """§4.1 non-negotiable #2: context must not be raised to chase long-context claims.

    32k KV would cost ~4.7 GB and force swapping on a fanless chassis (§2.1).
    """
    from core.config import LLAMA_BASE_ARGS

    idx = LLAMA_BASE_ARGS.index("-c")
    assert LLAMA_BASE_ARGS[idx + 1] == "8192", (
        "PLAN.md §4.1 forbids raising the context cap above 8192"
    )


def test_kv_cache_is_quantized():
    """§2.1: `q8_0` KV halves the cache from 1.2 GB to 0.6 GB; measured saving 0.52 GB."""
    from core.config import LLAMA_BASE_ARGS

    assert LLAMA_BASE_ARGS[LLAMA_BASE_ARGS.index("-ctk") + 1] == "q8_0"
    assert LLAMA_BASE_ARGS[LLAMA_BASE_ARGS.index("-ctv") + 1] == "q8_0"


def test_llama_server_binds_loopback_only():
    """§9: the endpoint must not be reachable off-host."""
    from core.config import LLAMA_BASE_ARGS

    assert LLAMA_BASE_ARGS[LLAMA_BASE_ARGS.index("--host") + 1] == "127.0.0.1"


def test_llama_server_disables_the_webui():
    """Keeps the attack surface to the API only; §4.1 invocation."""
    from core.config import LLAMA_BASE_ARGS

    assert "--no-webui" in LLAMA_BASE_ARGS


def test_single_parallel_slot():
    """§4.1: `-np 1` because a single-user local app gains nothing from parallel slots."""
    from core.config import LLAMA_BASE_ARGS

    assert LLAMA_BASE_ARGS[LLAMA_BASE_ARGS.index("-np") + 1] == "1"


def test_reasoning_is_off_by_default():
    """§4.1's first config fact: Qwen3 reasoning starves the §8.2 token budget."""
    from core.config import get_settings

    assert get_settings().llm_enable_thinking is False


def test_api_binds_loopback_only():
    """§9: the core service binds to 127.0.0.1, never 0.0.0.0."""
    from core.config import get_settings

    assert get_settings().api_host == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
def test_non_loopback_bind_is_rejected(host, monkeypatch):
    """§9 enforced structurally, not by convention.

    The API exposes the user's whole index, so a stray ``STACKSEARCH_API_HOST`` must
    fail loudly at startup rather than silently publishing it on a shared network.
    """
    from pydantic import ValidationError

    from core.config import Settings

    monkeypatch.setenv("STACKSEARCH_API_HOST", host)
    with pytest.raises(ValidationError, match="loopback"):
        Settings()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host, monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("STACKSEARCH_API_HOST", host)
    assert Settings().api_host == host


def test_api_token_is_generated_when_absent(monkeypatch):
    """§9: a bare launch must still be authenticated, never open."""
    from core.config import Settings

    monkeypatch.delenv("STACKSEARCH_TOKEN", raising=False)
    first = Settings().api_token
    second = Settings().api_token
    assert len(first) >= 32
    assert first != second, "token must be per-launch, not a fixed default"


def test_api_token_comes_from_the_environment(monkeypatch):
    """The Tauri host hands the per-launch token to the child process (§9, §13.1)."""
    from core.config import Settings

    monkeypatch.setenv("STACKSEARCH_TOKEN", "tok-from-tauri")
    assert Settings().api_token == "tok-from-tauri"
