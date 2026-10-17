"""PDF connector: same walk as files, page-level locators (§6.2, TODO 1.1)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from core.connectors.base import Change, ItemRef
from core.connectors.files import _excluded


class PdfConnector:
    kind = "pdf"

    def scan(self, config: dict) -> Iterator[ItemRef]:
        roots = [Path(p).expanduser() for p in config.get("paths", [])]
        excludes = tuple(config.get("exclude", ()))
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.pdf")):
                if not path.is_file():
                    continue
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                if _excluded(rel, excludes):
                    continue
                yield ItemRef(
                    external_id=f"{root}::{rel.as_posix()}",
                    uri=path.as_uri(),
                    title=rel.as_posix(),
                )

    def open_uri(self, external_id: str) -> str:
        _, _, rel = external_id.partition("::")
        return rel or external_id

    def watch(self, config: dict, emit: Callable[[Change], None]) -> Callable[[], None]:
        """Watch configured roots, emitting a Change per create/modify/delete (§6.2)."""
        from core.connectors.base import watch_connector

        return watch_connector(self, 0, config, emit)


def resolve_fs_path(external_id: str) -> Path:
    """Split a ``<root>::<rel>`` external id back into a filesystem path."""
    root, _, rel = external_id.partition("::")
    return Path(root).expanduser() / rel if rel else Path(external_id)
