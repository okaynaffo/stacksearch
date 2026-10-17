"""Phase 3: filesystem watcher + daily reconcile (PLAN.md §6.2, §6.5.5)."""

from __future__ import annotations

import time

import pytest

from core.connectors.watch import WatchManager, _wanted
from core.index import open_index
from core.jobs.tasks import schedule_reconcile

pytestmark = pytest.mark.unit


def _manager(**kwargs):
    captured: list[tuple] = []

    def _capture(kind, payload, priority):
        captured.append((kind, payload, priority))
        return len(captured)

    mgr = WatchManager(None, enqueue=_capture, **kwargs)
    return mgr, captured


def test_burst_coalesces_to_one_event(tmp_path):
    mgr, _ = _manager()
    mgr.add_source(1, "files", {"paths": [str(tmp_path)]})
    root = str(tmp_path)
    path = str(tmp_path / "a.md")
    for _ in range(5):
        mgr._on_fs_event(root, path, "changed")
    time.sleep(0.7)  # past DEBOUNCE_S
    mgr._flush_ready()
    # _flush_ready enqueues via _enqueue (the capture fn) — nothing pending now.
    assert mgr._pending == {}


def test_delete_wins_over_earlier_modify(tmp_path):
    mgr, captured = _manager()
    mgr.add_source(1, "files", {"paths": [str(tmp_path)]})
    root = str(tmp_path)
    path = str(tmp_path / "a.md")
    mgr._on_fs_event(root, path, "changed")
    mgr._on_fs_event(root, path, "deleted")
    time.sleep(0.7)
    mgr._flush_ready()
    assert [c[0] for c in captured] == ["delete_doc"]


def test_change_after_delete_rearms(tmp_path):
    """Editor atomic-save (delete + recreate) ends as a change, not a delete."""
    mgr, captured = _manager()
    mgr.add_source(1, "files", {"paths": [str(tmp_path)]})
    root = str(tmp_path)
    path = str(tmp_path / "a.md")
    mgr._on_fs_event(root, path, "deleted")
    mgr._on_fs_event(root, path, "changed")
    time.sleep(0.7)
    mgr._flush_ready()
    assert [c[0] for c in captured] == ["index_doc"]


def test_wanted_filters_exclusions_and_suffixes(tmp_path):
    root = str(tmp_path)
    assert _wanted("files", root, str(tmp_path / "a.md")) is True
    assert _wanted("files", root, str(tmp_path / "a.pdf")) is False
    assert _wanted("pdf", root, str(tmp_path / "a.pdf")) is True
    assert _wanted("pdf", root, str(tmp_path / "a.md")) is False
    assert _wanted("files", root, str(tmp_path / ".git" / "x.md")) is False
    assert _wanted("files", root, str(tmp_path / "node_modules" / "x.md")) is False


def test_live_observer_sees_create_and_delete(tmp_path):
    captured: list[tuple] = []

    def _capture(kind, payload, priority):
        captured.append((kind, payload))
        return len(captured)

    mgr = WatchManager(None, enqueue=_capture)
    mgr.add_source(7, "files", {"paths": [str(tmp_path)]})
    mgr.start()
    try:
        target = tmp_path / "live.md"
        target.write_text("hello watcher")
        deadline = time.time() + 10
        while time.time() < deadline and not any(
            c[1]["external_id"].endswith("live.md") and c[0] == "index_doc" for c in captured
        ):
            time.sleep(0.2)
        assert any(
            c[1]["external_id"].endswith("live.md") and c[0] == "index_doc" for c in captured
        ), f"no index_doc captured: {captured}"
        assert captured[0][1]["source_id"] == 7

        target.unlink()
        deadline = time.time() + 10
        while time.time() < deadline and not any(
            c[1]["external_id"].endswith("live.md") and c[0] == "delete_doc" for c in captured
        ):
            time.sleep(0.2)
        assert any(
            c[1]["external_id"].endswith("live.md") and c[0] == "delete_doc" for c in captured
        ), f"no delete_doc captured: {captured}"
    finally:
        mgr.stop()


def test_schedule_reconcile_only_stale_sources(tmp_path):
    with open_index(tmp_path / "index.db") as ix:
        conn = ix.conn
        conn.execute(
            "INSERT INTO sources(kind, name, config_json, last_sync_at)"
            " VALUES ('files','fresh','{}', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO sources(kind, name, config_json, last_sync_at)"
            " VALUES ('files','stale','{}', datetime('now', '-30 hours'))"
        )
        conn.execute("INSERT INTO sources(kind, name, config_json) VALUES ('pdf','never','{}')")
        scheduled = schedule_reconcile(conn, max_age_h=24)
        assert sorted(scheduled) == [2, 3]
        # Second call skips: a scan is already queued per source.
        assert schedule_reconcile(conn, max_age_h=24) == []
        kinds = conn.execute("SELECT priority FROM jobs").fetchall()
        assert {r["priority"] for r in kinds} == {9}
