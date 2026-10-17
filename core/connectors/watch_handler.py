"""watchdog event relay (translates FS events into watcher callbacks)."""

from __future__ import annotations

from collections.abc import Callable

from watchdog.events import FileSystemEventHandler


class RelayHandler(FileSystemEventHandler):
    """Translate watchdog events into ``emit(root, path, kind)`` calls."""

    def __init__(self, emit: Callable[[str, str, str], None], root: str) -> None:
        super().__init__()
        self._emit = emit
        self._root = root

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._emit(self._root, event.src_path, "changed")

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._emit(self._root, event.src_path, "changed")

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._emit(self._root, event.src_path, "deleted")

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._emit(self._root, event.src_path, "deleted")
            self._emit(self._root, event.dest_path, "changed")


# Backwards-compatible alias for the manager.
_RelayHandler = RelayHandler
