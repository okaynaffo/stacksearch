"""Chat connector: importers for WhatsApp / Telegram / Discord exports (§6.2, TODO 5.3).

One document per export file; re-import on file change flows through the
standard watcher + content-hash short-circuit, so an unchanged export costs
nothing (§6.5). Point chat sources at export directories — ``.txt``/``.json``
files owned by the files connector would otherwise be indexed twice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from core.connectors.base import Change, ItemRef
from core.connectors.files import _excluded

#: Export formats owned by this connector (sniffed further at parse time).
CHAT_SUFFIXES = frozenset({".txt", ".json"})


class ChatConnector:
    kind = "chat"

    def scan(self, config: dict) -> Iterator[ItemRef]:
        """Yield one ItemRef per chat export file under the configured paths.

        ``paths`` entries may be files or directories.
        """
        excludes = tuple(config.get("exclude", ()))
        for raw in config.get("paths", []):
            p = Path(raw).expanduser().resolve()
            if p.is_file():
                if p.suffix.lower() in CHAT_SUFFIXES:
                    yield ItemRef(
                        external_id=f"{p.parent}::{p.name}",
                        uri=p.as_uri(),
                        title=p.name,
                    )
                continue
            if not p.is_dir():
                continue
            for path in sorted(p.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    rel = path.relative_to(p)
                except ValueError:
                    continue
                if _excluded(rel, excludes):
                    continue
                if path.suffix.lower() not in CHAT_SUFFIXES:
                    continue
                yield ItemRef(
                    external_id=f"{p}::{rel.as_posix()}",
                    uri=path.as_uri(),
                    title=rel.as_posix(),
                )

    def open_uri(self, external_id: str) -> str:
        _, _, rel = external_id.partition("::")
        return rel or external_id

    def watch(self, config: dict, emit: Callable[[Change], None]) -> Callable[[], None]:
        """Watch export paths; edits re-import the whole export (§6.2)."""
        from core.connectors.base import watch_connector

        return watch_connector(self, 0, config, emit)
