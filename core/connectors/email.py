"""Email connector: local ``.eml`` / Apple Mail ``.emlx`` / mbox / Maildir (§6.2, TODO 5.1).

Offline-first: this connector owns the local containers and never touches the
network. IMAP lives in :mod:`core.connectors.email_imap` behind the optional
``phase5`` extra. One document per container file (or Maildir), with per-message
chunks produced at index time (§6.4).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from core.connectors.base import Change, ItemRef
from core.connectors.files import _excluded
from core.extract.email import EMAIL_SUFFIXES

#: mbox-style single files (contents are concatenated messages, not one email).
MBOX_SUFFIXES = frozenset({".mbox", ".mbx"})
_MAILDIR_MARKERS = ("cur", "new", "tmp")


def _is_maildir(path: Path) -> bool:
    return path.is_dir() and all((path / m).is_dir() for m in _MAILDIR_MARKERS)


def _owned_file(path: Path) -> bool:
    return path.suffix.lower() in EMAIL_SUFFIXES or path.suffix.lower() in MBOX_SUFFIXES


class EmailConnector:
    kind = "email"

    def scan(self, config: dict) -> Iterator[ItemRef]:
        """Yield one ItemRef per email container under the configured paths.

        ``paths`` entries may be ``.eml``/``.emlx``/``.mbox`` files, Maildir
        directories, or directories to search.
        """
        excludes = tuple(config.get("exclude", ()))
        for raw in config.get("paths", []):
            p = Path(raw).expanduser().resolve()
            if _is_maildir(p):
                yield ItemRef(
                    external_id=f"{p.parent}::{p.name}",
                    uri=p.as_uri(),
                    title=p.name,
                )
                continue
            if p.is_file():
                if _owned_file(p):
                    yield ItemRef(
                        external_id=f"{p.parent}::{p.name}",
                        uri=p.as_uri(),
                        title=p.name,
                    )
                continue
            if not p.is_dir():
                continue
            for path in sorted(p.rglob("*")):
                if _is_maildir(path):
                    try:
                        rel = path.relative_to(p)
                    except ValueError:
                        continue
                    if _excluded(rel, excludes):
                        continue
                    yield ItemRef(
                        external_id=f"{p}::{rel.as_posix()}",
                        uri=path.as_uri(),
                        title=rel.as_posix() + "/",
                    )
                    continue
                if not path.is_file() or not _owned_file(path):
                    continue
                try:
                    rel = path.relative_to(p)
                except ValueError:
                    continue
                if _excluded(rel, excludes):
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
        """Watch export paths; an edit re-imports the whole container (§6.2)."""
        from core.connectors.base import watch_connector

        return watch_connector(self, 0, config, emit)
