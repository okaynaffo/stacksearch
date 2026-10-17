"""Heuristic throttle policy: query pause + battery pause (PLAN.md §6.6).

Deliberately heuristic (no macOS thermal APIs): two pause signals, both
auto-resuming. The §6.7 model handoff lands here too — ``POST /search`` and
``POST /answer`` hold :meth:`query_pause` while running, and the answer path
(``core.llm.answer``) additionally evicts the embedder/reranker and spawns
``llama-server`` lazily, keeping only one model group resident at a time.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class PausePolicy:
    """Thread-safe pause flags shared by the API routes and the worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._query_active = 0
        self._battery_low = False

    # -- query pause (§6.7 state 2: indexer yields within one batch) ---------

    @contextmanager
    def query_pause(self) -> Iterator[None]:
        """Hold while a search runs; the worker stalls between jobs."""
        with self._lock:
            self._query_active += 1
        try:
            yield
        finally:
            with self._lock:
                self._query_active -= 1

    @property
    def query_paused(self) -> bool:
        with self._lock:
            return self._query_active > 0

    # -- battery pause (§6.6: pause bulk work, keep live changes) ------------

    def set_battery_low(self, low: bool) -> None:
        with self._lock:
            self._battery_low = low

    @property
    def battery_paused(self) -> bool:
        with self._lock:
            return self._battery_low

    # -- combined ------------------------------------------------------------

    def bulk_allowed(self) -> bool:
        """Whether bulk/reconcile jobs (priority >= 5) may run now."""
        return not self.battery_paused

    def paused_reason(self) -> str | None:
        """Human-readable pause state for ``GET /status``."""
        if self.query_paused and self.battery_paused:
            return "query-active+battery"
        if self.query_paused:
            return "query-active"
        if self.battery_paused:
            return "battery"
        return None


#: Process-wide policy. Routes hold :meth:`query_pause`; the worker reads it.
policy = PausePolicy()
