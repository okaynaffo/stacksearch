"""Connector interface (PLAN.md §6.1, TODO 1.1)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ItemRef:
    """Pointer to one logical item, from ``scan``."""

    external_id: str  # stable id within the source (e.g. relpath)
    uri: str  # how the UI opens it (file://, message://, https://)
    title: str | None = None
    modified_at: str | None = None


@dataclass(frozen=True)
class Change:
    kind: str  # new | changed | deleted
    ref: ItemRef


@dataclass(frozen=True)
class RawItem:
    """Content fetched by a connector that has no local filesystem path.

    Network connectors (IMAP) yield these instead of resolving an
    ``external_id`` to a file. ``data`` is the raw RFC822 message bytes.
    """

    data: bytes
    uri: str  # message://<id> for email
    title: str | None = None
    author: str | None = None
    modified_at: str | None = None
    meta: dict | None = None  # connector index state, e.g. IMAP uidvalidity


class Connector(Protocol):
    kind: str

    def scan(self, config: dict) -> Iterator[ItemRef]: ...
    def open_uri(self, external_id: str) -> str: ...
    def fetch(self, config: dict, external_id: str) -> RawItem | None:
        """Return content for a non-file item, or None to read ``external_id`` as a path.

        Optional: file-backed connectors omit this and let the worker resolve a
        local path instead (§6.1).
        """
        ...
    def watch(self, config: dict, emit: Callable[[Change], None]) -> Callable[[], None]:
        """Watch configured roots, calling ``emit`` per change (§6.2).

        Returns a zero-argument stop callable. The default owner is
        :class:`core.connectors.watch.WatchManager`, which multiplexes all
        sources and translates changes into queue jobs; this method is the
        single-source convenience form of the same subscription.
        """
        ...


WatcherEmit = Callable[[Change], None]


def watch_connector(
    connector: Connector,
    source_id: int,
    config: dict,
    emit: WatcherEmit,
):
    """Subscribe one connector, adapting queue-style intake to ``Change`` events."""
    from core.connectors.watch import WatchManager

    def _to_change(kind: str, payload: dict, _priority: int) -> int:
        external_id = payload["external_id"]
        _, _, rel = external_id.partition("::")
        emit(
            Change(
                kind="deleted" if kind == "delete_doc" else "changed",
                ref=ItemRef(external_id=external_id, uri=rel or external_id, title=rel),
            )
        )
        return 0

    manager = WatchManager(None, enqueue=_to_change)
    manager.add_source(source_id, connector.kind, config)
    manager.start()
    return manager.stop
