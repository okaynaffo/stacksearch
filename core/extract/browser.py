"""Browser history reader: Chrome / Firefox / Safari (§6.2, TODO 5.2).

The browser owns its SQLite file (often WAL-locked while running), so the DB
is always copied to a temp file before reading — never opened in place.
Stdlib ``sqlite3`` only. Page-text fetching (opt-in, TODO 5.2) is intentionally
absent: history chunks are title + URL until that item is scheduled.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Chrome epoch (1601-01-01) → Unix epoch offset, in seconds.
_CHROME_EPOCH_DELTA = 11644473600

MAX_ENTRIES = 10_000


@dataclass(frozen=True)
class HistoryEntry:
    url: str
    title: str | None
    last_visit: str | None  # ISO date, YYYY-MM-DD
    visit_count: int = 0


def _chrome_ts(micros: int | None) -> str | None:
    if not micros:
        return None
    try:
        import datetime as _dt

        unix = micros / 1_000_000 - _CHROME_EPOCH_DELTA
        return _dt.datetime.fromtimestamp(unix).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _unix_ts(micros_or_s) -> str | None:
    if not micros_or_s:
        return None
    try:
        import datetime as _dt

        ts = float(micros_or_s)
        if ts > 1e12:  # microseconds
            ts /= 1_000_000
        elif ts > 1e10:  # milliseconds
            ts /= 1_000
        return _dt.datetime.fromtimestamp(ts).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def read_history(
    db_path: Path, *, since: str | None = None, limit: int = MAX_ENTRIES
) -> list[HistoryEntry]:
    """Copy ``db_path`` to temp storage and read history entries.

    ``since`` is an ISO date; entries without a parseable date always pass the
    filter so a cursor never hides undated rows. Auto-detects the Chrome,
    Firefox, and Safari schemas by table name.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    try:
        shutil.copyfile(db_path, tmp.name)
        conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            tables = _tables(conn)
            if "urls" in tables:
                entries = _read_chrome(conn, limit * 2)
            elif "moz_places" in tables:
                entries = _read_firefox(conn, limit * 2)
            elif "history_items" in tables:
                entries = _read_safari(conn, limit * 2)
            else:
                raise ValueError(f"unrecognized history schema in {db_path}: {sorted(tables)}")
        finally:
            conn.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    if since:
        entries = [e for e in entries if e.last_visit is None or e.last_visit > since]
    # Newest first; undated last. Cap after filtering.
    entries.sort(key=lambda e: (e.last_visit is not None, e.last_visit or ""), reverse=True)
    return entries[:limit]


def _read_chrome(conn: sqlite3.Connection, limit: int) -> list[HistoryEntry]:
    try:
        rows = conn.execute(
            "SELECT url, title, last_visit_time, visit_count FROM urls "
            "ORDER BY last_visit_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read Chrome history: {exc}") from exc
    return [
        HistoryEntry(
            url=r["url"],
            title=r["title"] or None,
            last_visit=_chrome_ts(r["last_visit_time"]),
            visit_count=int(r["visit_count"] or 0),
        )
        for r in rows
        if r["url"]
    ]


def _read_firefox(conn: sqlite3.Connection, limit: int) -> list[HistoryEntry]:
    try:
        rows = conn.execute(
            "SELECT url, title, last_visit_date, visit_count FROM moz_places "
            "ORDER BY last_visit_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read Firefox history: {exc}") from exc
    return [
        HistoryEntry(
            url=r["url"],
            title=r["title"] or None,
            last_visit=_unix_ts(r["last_visit_date"]),
            visit_count=int(r["visit_count"] or 0),
        )
        for r in rows
        if r["url"]
    ]


def _read_safari(conn: sqlite3.Connection, limit: int) -> list[HistoryEntry]:
    try:
        rows = conn.execute(
            "SELECT i.url AS url, v.title AS title, MAX(v.visit_time) AS visit_time "
            "FROM history_items i LEFT JOIN history_visits v "
            "ON v.history_item = i.id GROUP BY i.id ORDER BY visit_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read Safari history: {exc}") from exc
    entries = []
    for r in rows:
        if not r["url"]:
            continue
        # Safari visit_time is Cocoa CoreData time (seconds since 2001-01-01).
        last_visit = None
        if r["visit_time"]:
            try:
                import datetime as _dt

                last_visit = (
                    _dt.datetime(2001, 1, 1)
                    + _dt.timedelta(seconds=float(r["visit_time"]))
                ).date().isoformat()
            except (OverflowError, OSError, ValueError):
                last_visit = None
        entries.append(
            HistoryEntry(url=r["url"], title=r["title"] or None, last_visit=last_visit)
        )
    return entries
