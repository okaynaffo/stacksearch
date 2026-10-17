"""Files / notes connector: walk folders with include/exclude globs (§6.2, TODO 1.1)."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterator
from pathlib import Path

from core.connectors.base import Change, ItemRef

#: Built-in defaults (§6.2): never index dependency, VCS, venv, or build output.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    "*.pyc",
    "*.pyo",
)

#: Suffixes owned by this connector in Phase 1 (PDFs have their own connector).
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".text", ".docx"})


def _matches_any(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _excluded(rel: Path, extra_excludes: tuple[str, ...] | list[str] = ()) -> bool:
    parts = set(rel.parts)
    names = [rel.name, *rel.parts]
    for pat in (*DEFAULT_EXCLUDES, *extra_excludes):
        if "/" not in pat and "*" not in pat:
            if pat in parts:
                return True
        elif any(fnmatch.fnmatch(n, pat) for n in names) or fnmatch.fnmatch(rel.as_posix(), pat):
            return True
    return _matches_any(rel.name, tuple(extra_excludes))


class FilesConnector:
    kind = "files"

    def scan(self, config: dict) -> Iterator[ItemRef]:
        """Yield one ItemRef per indexable file under the configured roots."""
        roots = [Path(p).expanduser() for p in config.get("paths", [])]
        includes = config.get("include", ["*"])
        excludes = tuple(config.get("exclude", ()))
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                if _excluded(rel, excludes):
                    continue
                if path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if not _matches_any(rel.as_posix(), tuple(includes)) and not _matches_any(
                    path.name, tuple(includes)
                ):
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
