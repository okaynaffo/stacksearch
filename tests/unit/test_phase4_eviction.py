"""Phase 4 §6.7 regression: evict → reload must not break vector search.

Found by the answer eval: after the first ``POST /answer`` evicted the
embedder, every later reload threw ``ValueError: already registered`` from
``add_custom_model`` (it runs on every ``_build``), and hybrid search silently
degraded to BM25-only — the full live eval measured 40/40 exact-only instead
of 92/95. No network is used here; fastembed is stubbed out.
"""

from __future__ import annotations

import pytest

import core.models.embedder as _emb_mod
from core.models.embedder import Embedder, get_embedder, unload_embedder

pytestmark = pytest.mark.unit


class _FakeRegistry:
    """Stub for ``fastembed.TextEmbedding``: registry + constructor, no weights."""

    registered: list[str] = []
    constructed: list[str] = []

    def __init__(self, model_name=None, **kwargs):
        type(self).constructed.append(model_name)

    @classmethod
    def reset(cls):
        cls.registered = []
        cls.constructed = []

    @classmethod
    def list_supported_models(cls):
        return [{"model": name} for name in cls.registered]

    @classmethod
    def add_custom_model(cls, model, **kwargs):
        if model in cls.registered:
            raise ValueError(f"Model {model} is already registered")
        cls.registered.append(model)


@pytest.fixture
def stub_fastembed(monkeypatch):
    import fastembed

    _FakeRegistry.reset()
    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeRegistry)
    yield _FakeRegistry
    # Don't leak a stub-backed singleton into other tests.
    _emb_mod._embedder = None


def test_rebuild_after_evict_does_not_reregister(stub_fastembed):
    """The §6.7 cycle: load → unload → load must not throw."""
    emb = Embedder()
    emb.load()
    assert emb.is_loaded
    emb.unload()
    assert not emb.is_loaded
    emb.load()  # second add_custom_model would throw without the guard
    assert emb.is_loaded
    assert stub_fastembed.registered.count("intfloat/multilingual-e5-base") == 1


def test_singleton_evict_reload_cycle(stub_fastembed):
    """get_embedder after unload_embedder returns a working instance."""
    first = get_embedder()
    first.load()
    unload_embedder()
    second = get_embedder()
    assert second is not first
    second.load()
    assert second.is_loaded
