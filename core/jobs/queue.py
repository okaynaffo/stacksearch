"""Persistent priority job queue over the ``jobs`` table (PLAN.md §6.6).

Jobs survive restarts because the table is the queue: the worker claims rows
with ``BEGIN IMMEDIATE`` so two claimants can never take the same job, and a
row left ``running`` by a crashed process is reclaimed on startup.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any],
    priority: int = 5,
    run_after: str | None = None,
) -> int:
    """Insert one job; returns its id."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO jobs(kind, payload_json, priority, status, run_after)"
            " VALUES (?, ?, ?, 'queued', ?)",
            (kind, json.dumps(payload), priority, run_after),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return job_id


def claim_next(conn: sqlite3.Connection, *, bulk_allowed: bool = True) -> dict[str, Any] | None:
    """Claim the highest-priority ready job, marking it ``running``.

    When ``bulk_allowed`` is False (battery/bulk pause), jobs with priority
    >= 5 are skipped so live changes (pri 1–2) still flow. Returns the job
    row as a dict, or None when the queue is empty.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT id, kind, payload_json, priority, attempts
            FROM jobs
            WHERE status = 'queued'
              AND (run_after IS NULL OR run_after <= datetime('now'))
              AND (? OR priority < 5)
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """,
            (1 if bulk_allowed else 0,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?",
            (row["id"],),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "payload": json.loads(row["payload_json"]),
        "priority": row["priority"],
        "attempts": row["attempts"] + 1,
    }


def complete(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def fail(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    max_attempts: int = 3,
    backoff_base_s: float = 5.0,
) -> str:
    """Requeue with exponential backoff, or drop after ``max_attempts``.

    Returns ``'retry'`` or ``'dropped'``. The caller records the document
    error itself (it knows the source/external pair); the queue only decides
    retry vs drop.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return "dropped"
        if row["attempts"] >= max_attempts:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            outcome = "dropped"
        else:
            delay_s = backoff_base_s * (2 ** (row["attempts"] - 1))
            conn.execute(
                """UPDATE jobs SET status = 'queued',
                          run_after = datetime('now', '+' || ? || ' seconds')
                   WHERE id = ?""",
                (delay_s, job_id),
            )
            outcome = "retry"
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return outcome


def reclaim_orphans(conn: sqlite3.Connection) -> int:
    """Reset ``running`` rows left by a crashed worker to ``queued``.

    Called once at worker startup so a restart never loses jobs (§6.6:
    "survives restarts").
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        n = cur.rowcount
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return n


def pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()
    return row[0]
