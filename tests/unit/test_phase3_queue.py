"""Phase 3: persistent queue, worker execution, throttle policy (PLAN.md §6.6)."""

from __future__ import annotations

import pytest

from core.index import open_index
from core.jobs import queue as job_queue
from core.jobs.policy import PausePolicy
from core.jobs.tasks import run_delete_doc, run_index_doc, run_scan
from core.jobs.worker import Worker
from core.query.bm25 import bm25_search

pytestmark = pytest.mark.unit


def _db(tmp_path):
    ix = open_index(tmp_path / "index.db")
    ix.conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('files','v','{}')")
    src = ix.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return ix, src


def test_priority_order_and_complete(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        job_queue.enqueue(conn, "scan", {"source_id": 1}, priority=9)
        job_queue.enqueue(conn, "index_doc", {"source_id": 1}, priority=2)
        job_queue.enqueue(conn, "scan", {"source_id": 1}, priority=1)
        first = job_queue.claim_next(conn)
        assert first["priority"] == 1
        job_queue.complete(conn, first["id"])
        second = job_queue.claim_next(conn)
        assert second["priority"] == 2
        job_queue.complete(conn, second["id"])
        assert job_queue.pending_count(conn) == 1


def test_fail_retries_then_drops(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        jid = job_queue.enqueue(conn, "index_doc", {}, priority=2)
        for _ in range(2):
            job = job_queue.claim_next(conn)
            assert job["id"] == jid
            # run_after is in the future: not claimable until backoff elapses.
            assert job_queue.fail(conn, jid, max_attempts=3, backoff_base_s=60) == "retry"
            conn.execute("UPDATE jobs SET run_after = NULL WHERE id = ?", (jid,))
        job = job_queue.claim_next(conn)
        assert job_queue.fail(conn, jid, max_attempts=3, backoff_base_s=60) == "dropped"
        assert job_queue.pending_count(conn) == 0


def test_reclaim_orphans(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        jid = job_queue.enqueue(conn, "index_doc", {}, priority=2)
        job = job_queue.claim_next(conn)
        assert job["id"] == jid
        assert job_queue.claim_next(conn) is None  # stuck running
        assert job_queue.reclaim_orphans(conn) == 1
        assert job_queue.claim_next(conn)["id"] == jid


def test_battery_pause_skips_bulk_only(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        job_queue.enqueue(conn, "scan", {"source_id": 1}, priority=9)
        job_queue.enqueue(conn, "index_doc", {"source_id": 1}, priority=2)
        policy = PausePolicy()
        policy.set_battery_low(True)
        assert policy.paused_reason() == "battery"
        worker = Worker(tmp_path / "index.db", policy=policy)
        job = job_queue.claim_next(conn, bulk_allowed=policy.bulk_allowed())
        assert job["priority"] == 2  # live change still flows
        worker.drain_once(conn)  # executes against unknown source, completes anyway
        assert job_queue.claim_next(conn, bulk_allowed=policy.bulk_allowed()) is None
        policy.set_battery_low(False)
        assert job_queue.claim_next(conn)["priority"] == 9


def test_worker_indexes_and_deletes_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("worker lighthouse content here")
    (vault / "gone.md").write_text("temporary zephyr winds note")
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute(
            "INSERT INTO sources(kind, name, config_json) VALUES ('files','v', ?)",
            (f'{{"paths": ["{vault}"]}}',),
        )
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        worker = Worker(tmp_path / "index.db", embed=False)

        assert run_index_doc(conn, src, f"{vault}::a.md", embed=False)["ok"] is True
        assert run_index_doc(conn, src, f"{vault}::gone.md", embed=False)["ok"] is True
        assert bm25_search(conn, "lighthouse")

        assert run_delete_doc(conn, src, f"{vault}::gone.md")["removed"] is True
        assert bm25_search(conn, "zephyr") == []

        # drain_once executes through the queue on the caller's connection.
        job_queue.enqueue(
            conn,
            "delete_doc",
            {"source_id": src, "external_id": f"{vault}::a.md"},
            priority=2,
        )
        assert worker.drain_once(conn)["kind"] == "delete_doc"
        assert bm25_search(conn, "lighthouse") == []


def test_scan_enqueues_children_and_detects_deletes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("scan target content here")
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute(
            "INSERT INTO sources(kind, name, config_json) VALUES ('files','v', ?)",
            (f'{{"paths": ["{vault}"]}}',),
        )
        src = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Stale row for a file that no longer exists.
        from core.index.writer import ChunkWrite, index_document

        index_document(
            conn,
            source_id=src,
            external_id=f"{vault}::ghost.md",
            uri="file:///ghost.md",
            title="ghost.md",
            text="ghost words",
            chunks=[ChunkWrite(text="ghost words", loc_json="{}", token_count=2)],
        )
        report = run_scan(conn, src, child_priority=2)
        assert report == {"ok": True, "enqueued": 1, "deleted": 1}
        kinds = sorted(r["kind"] for r in conn.execute("SELECT kind FROM jobs").fetchall())
        assert kinds == ["delete_doc", "index_doc"]
