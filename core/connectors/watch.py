"""Filesystem watcher: FSEvents via watchdog, debounced into queue jobs (PLAN.md §6.2).

One :class:`WatchManager` owns a single ``Observer`` and any number of
watched roots. FS events are coalesced per path (bursts from editors that
save-then-chmod collapse to one job) and translated into ``index_doc`` /
``delete_doc`` jobs on a dedicated connection, so the observer thread never
shares a connection with the API or the worker.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from core.connectors.base import Change, ItemRef

#: Coalescing window: events for the same path inside this window collapse.
DEBOUNCE_S = 0.5


class WatchManager:
    """Watch configured roots and enqueue jobs for what changes."""

    def __init__(
        self,
        db_path: Path | str | None,
        enqueue: Callable[[str, dict, int], int] | None = None,
    ) -> None:
        from core.config import get_settings

        self.db_path = Path(db_path) if db_path is not None else None
        self._enqueue = enqueue or self._enqueue_db
        self._queue_cfg = get_settings().queue
        self._observer = None
        self._watched: dict[str, dict] = {}  # root -> {"source_id", "kind"}
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[float, str, int, str]] = {}
        self._flush_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- registration ------------------------------------------------------

    def add_source(self, source_id: int, kind: str, config: dict) -> int:
        """Register a source's roots; returns the number of roots added."""
        n = 0
        for raw in config.get("paths", []):
            root = str(Path(raw).expanduser())
            with self._lock:
                self._watched[root] = {"source_id": source_id, "kind": kind}
            n += 1
        return n

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        from watchdog.observers import Observer

        if self._observer is not None:
            return
        from core.connectors.watch_handler import _RelayHandler

        self._stop.clear()
        observer = Observer()
        seen_roots: set[str] = set()
        with self._lock:
            roots = dict(self._watched)
        for root, _info in roots.items():
            if root in seen_roots or not Path(root).is_dir():
                continue
            seen_roots.add(root)
            try:
                observer.schedule(_RelayHandler(self._on_fs_event, root), root, recursive=True)
            except Exception:  # noqa: BLE001 — one bad root must not drop the rest
                continue
        self._observer = observer
        observer.start()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="watch-flush", daemon=True
        )
        self._flush_thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=timeout_s)
            self._observer = None
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=timeout_s)
            self._flush_thread = None
        self._flush_all()

    # -- event intake (observer thread) -------------------------------------

    def _on_fs_event(self, root: str, path: str, terminal_kind: str) -> None:
        """Record a raw event; the flush loop debounces and enqueues."""
        with self._lock:
            info = self._watched.get(root)
            if info is None:
                return
            if not _wanted(info["kind"], root, path):
                return
            prev = self._pending.get(path)
            # A delete wins over any earlier event for the same path; anything
            # after a delete re-arms it as a change (editor atomic-save pattern).
            if prev is not None and prev[1] == "deleted" and terminal_kind != "deleted":
                kind = "changed"
            elif terminal_kind == "deleted":
                kind = "deleted"
            else:
                kind = prev[1] if prev is not None and prev[1] != "deleted" else "changed"
            self._pending[path] = (time.monotonic(), kind, info["source_id"], root)

    # -- flush loop ----------------------------------------------------------

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(DEBOUNCE_S)
            self._flush_ready()

    def _flush_ready(self) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [
                (p, kind, sid, root)
                for p, (ts, kind, sid, root) in self._pending.items()
                if now - ts >= DEBOUNCE_S
            ]
            for p, *_ in ready:
                del self._pending[p]
        for path, kind, source_id, root in ready:
            self._emit(source_id, root, path, kind)

    def _flush_all(self) -> None:
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for path, (_, kind, source_id, root) in pending:
            self._emit(source_id, root, path, kind)

    def _emit(self, source_id: int, root: str, path: str, kind: str) -> None:
        rel = _rel(root, path)
        external_id = f"{root}::{rel}"
        if kind == "deleted":
            self._enqueue(
                "delete_doc",
                {"source_id": source_id, "external_id": external_id},
                self._queue_cfg.priority_live_change,
            )
        else:
            self._enqueue(
                "index_doc",
                {"source_id": source_id, "external_id": external_id},
                self._queue_cfg.priority_live_change,
            )

    def _enqueue_db(self, kind: str, payload: dict, priority: int) -> int:
        """Default enqueue over a fresh connection (thread-safe by construction)."""
        if self.db_path is None:
            raise RuntimeError("WatchManager has no db_path and no custom enqueue")
        from core.index.db import connect
        from core.jobs import queue as job_queue

        conn = connect(self.db_path)
        try:
            return job_queue.enqueue(conn, kind, payload, priority=priority)
        finally:
            conn.close()

    # -- test helper ----------------------------------------------------------

    def changes_for_test(self) -> list[Change]:
        """Drain debounced events as Changes without enqueueing (tests only)."""
        now = time.monotonic()
        with self._lock:
            ready = [
                (p, kind, root)
                for p, (ts, kind, _sid, root) in self._pending.items()
                if now - ts >= 0
            ]
            self._pending.clear()
        out: list[Change] = []
        for path, kind, root in ready:
            rel = _rel(root, path)
            out.append(
                Change(
                    kind="deleted" if kind == "deleted" else "changed",
                    ref=ItemRef(external_id=f"{root}::{rel}", uri=Path(path).as_uri(), title=rel),
                )
            )
        return out


def _rel(root: str, path: str) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return Path(path).name


def _wanted(kind: str, root: str, path: str) -> bool:
    """Apply the same exclusion + suffix rules as the scan path (§6.2)."""
    from core.connectors.files import _excluded

    p = Path(path)
    if p.is_dir():
        return False
    try:
        rel = p.relative_to(root)
    except ValueError:
        return False
    if _excluded(rel):
        return False
    suffix = p.suffix.lower()
    if kind == "pdf":
        return suffix == ".pdf"
    from core.connectors.files import TEXT_SUFFIXES

    return suffix in TEXT_SUFFIXES
