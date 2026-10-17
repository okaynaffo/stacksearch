"""Browser-history connector: Chrome / Firefox / Safari profile DBs (§6.2, TODO 5.2).

One document per profile database (``<profile dir>::<db name>``); each visited
URL becomes one chunk at index time (§6.4). The live browser DB is never
opened in place — ``core.extract.browser.read_history`` copies it to a temp
file first. The last-visit cursor lives in ``sources.cursor`` and is advanced
by the worker after each successful index.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from core.connectors.base import Change, ItemRef

#: Profile database filenames, by browser.
PROFILE_DB_NAMES = {
    "History": "chrome",  # Chrome / Edge / Brave Default/History
    "places.sqlite": "firefox",  # Firefox profiles/*/places.sqlite
    "History.db": "safari",  # Safari History.db
}


def _profile_title(db_path: Path) -> str:
    browser = PROFILE_DB_NAMES.get(db_path.name, "browser")
    return f"{browser}/{db_path.parent.name}"


class BrowserConnector:
    kind = "browser"

    def scan(self, config: dict) -> Iterator[ItemRef]:
        """Yield one ItemRef per history DB found under the configured paths.

        ``paths`` entries may be profile DB files or directories to search
        (e.g. ``~/Library/Application Support/Google/Chrome``).
        """
        for raw in config.get("paths", []):
            p = Path(raw).expanduser().resolve()
            if p.is_file():
                if p.name in PROFILE_DB_NAMES:
                    yield ItemRef(
                        external_id=f"{p.parent}::{p.name}",
                        uri=p.as_uri(),
                        title=_profile_title(p),
                    )
                continue
            if not p.is_dir():
                continue
            for path in sorted(p.rglob("*")):
                if not path.is_file() or path.name not in PROFILE_DB_NAMES:
                    continue
                # Skip lock/journal sidecars.
                if path.suffix in (".journal", ".wal", ".shm"):
                    continue
                try:
                    rel = path.relative_to(p)
                except ValueError:
                    continue
                yield ItemRef(
                    external_id=f"{p}::{rel.as_posix()}",
                    uri=path.as_uri(),
                    title=_profile_title(path),
                )

    def open_uri(self, external_id: str) -> str:
        # History entries open by URL from the chunk loc, not the profile DB.
        return external_id

    def watch(self, config: dict, emit: Callable[[Change], None]) -> Callable[[], None]:
        """Watch profile dirs; history DBs change on every visit (§6.2)."""
        from core.connectors.base import watch_connector

        return watch_connector(self, 0, config, emit)
