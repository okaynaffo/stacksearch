"""Background indexing worker: drains the persistent queue on its own thread (PLAN.md §6.6).

One worker, one write connection: the thread opens a dedicated SQLite
connection to the same index file (WAL mode lets reads proceed while it
writes) so the API's read connection is never shared across threads.
Each document update remains exactly one transaction (§6.5.4); ONNX
embedding always happens outside the write lock.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.config import Queue, get_settings
from core.index.db import connect
from core.index.writer import record_error
from core.jobs import queue as job_queue
from core.jobs.policy import PausePolicy
from core.jobs.tasks import run_delete_doc, run_index_doc, run_reembed, run_scan

ProgressFn = Callable[..., None]


class Worker:
    """Single-threaded queue drainer with pause-aware polling."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        policy: PausePolicy | None = None,
        queue_cfg: Queue | None = None,
        progress: ProgressFn | None = None,
        poll_idle_s: float = 0.25,
        embed: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.policy = policy or PausePolicy()
        self.queue_cfg = queue_cfg or get_settings().queue
        self.progress = progress or (lambda *a, **k: None)
        self.poll_idle_s = poll_idle_s
        self.embed = embed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "idle"

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        job_queue.reclaim_orphans(connect(self.db_path))
        self._thread = threading.Thread(target=self._loop, name="index-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None
        self.state = "stopped"

    # -- main loop -----------------------------------------------------------

    def _loop(self) -> None:
        conn = connect(self.db_path)
        try:
            self._reconcile(conn)
            last_reconcile = time.monotonic()
            while not self._stop.is_set():
                if self.policy.query_paused:
                    self.state = "paused-query"
                    time.sleep(0.1)
                    continue
                # Daily reconcile (§6.5.5): catch events missed while closed.
                if time.monotonic() - last_reconcile > 3600:
                    self._reconcile(conn)
                    last_reconcile = time.monotonic()
                job = job_queue.claim_next(conn, bulk_allowed=self.policy.bulk_allowed())
                if job is None:
                    self.state = "idle"
                    time.sleep(self.poll_idle_s)
                    continue
                self.state = f"working:{job['kind']}"
                self._execute(conn, job)
        finally:
            conn.close()
            if self.state != "stopped":
                self.state = "idle"

    def _reconcile(self, conn: sqlite3.Connection) -> None:
        from core.jobs.tasks import schedule_reconcile

        try:
            scheduled = schedule_reconcile(conn)
        except Exception:  # noqa: BLE001 — reconcile must never stall the worker
            return
        if scheduled:
            self.progress("reconcile-scheduled", source_ids=scheduled)

    def _execute(self, conn: sqlite3.Connection, job: dict[str, Any]) -> None:
        kind = job["kind"]
        payload = job["payload"]
        try:
            if kind == "index_doc":
                result = run_index_doc(
                    conn, payload["source_id"], payload["external_id"], embed=self.embed
                )
            elif kind == "delete_doc":
                result = run_delete_doc(conn, payload["source_id"], payload["external_id"])
            elif kind == "scan":
                result = run_scan(
                    conn,
                    payload["source_id"],
                    child_priority=int(payload.get("child_priority", 5)),
                )
            elif kind == "reembed":
                result = run_reembed(conn, payload["chunk_id"])
            else:
                result = {"ok": False, "error": f"unknown job kind {kind!r}"}
        except Exception as exc:  # noqa: BLE001 — one bad doc must not kill the worker
            outcome = job_queue.fail(
                conn,
                job["id"],
                max_attempts=self.queue_cfg.max_attempts,
                backoff_base_s=self.queue_cfg.backoff_base_s,
            )
            error = f"{type(exc).__name__}: {exc}"
            if outcome == "dropped":
                self._record_doc_error(conn, payload, error)
            self.progress("job-failed", job=job, error=error, outcome=outcome)
            return

        if not result.get("ok", False) or result.get("skipped", False):
            # Skips (binary, too big, missing) are terminal, not retries.
            job_queue.complete(conn, job["id"])
        else:
            job_queue.complete(conn, job["id"])
        self.progress("job-done", job=job, result=result)

    def _record_doc_error(
        self, conn: sqlite3.Connection, payload: dict[str, Any], error: str
    ) -> None:
        """After 3 failures mark the document ``error`` (§6.6) so settings can surface it."""
        if "source_id" in payload and "external_id" in payload:
            try:
                record_error(
                    conn,
                    source_id=payload["source_id"],
                    external_id=payload["external_id"],
                    uri=payload["external_id"],
                    error=error,
                )
            except Exception:  # noqa: BLE001, S110 — best effort
                pass

    # -- test helper ---------------------------------------------------------

    def drain_once(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """Claim and execute a single job on the caller's connection.

        Used by tests to exercise the full path without threads.
        """
        job = job_queue.claim_next(conn, bulk_allowed=self.policy.bulk_allowed())
        if job is None:
            return None
        self._execute(conn, job)
        return job
