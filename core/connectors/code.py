"""Code connector: repo roots with .gitignore respect (§6.2, TODO 5.4).

One document per source file; ``external_id`` is ``<abs root>::<rel>`` so the
worker resolves it with the shared ``resolve_fs_path``. Binary and oversized
files are filtered at index time (§6.3), not here.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterator
from pathlib import Path

from core.chunking.code import SUFFIX_LANGUAGE
from core.connectors.base import Change, ItemRef
from core.connectors.files import DEFAULT_EXCLUDES

#: Extra never-index dirs for code roots (build output, IDE state, vendored code).
CODE_EXCLUDE_DIRS = frozenset(
    {
        "target",
        ".idea",
        ".vscode",
        "vendor",
        "coverage",
        ".next",
        ".nuxt",
        "Pods",
        "Carthage",
    }
)

CODE_SUFFIXES = frozenset(SUFFIX_LANGUAGE)


def _load_gitignore_patterns(root: Path) -> list[str]:
    """Best-effort subset of .gitignore: blank lines, comments, and ``!`` negations
    are ignored; ``dir/`` entries become dir names; globs are matched with fnmatch."""
    patterns: list[str] = []
    ignore = root / ".gitignore"
    if not ignore.is_file():
        return patterns
    try:
        text = ignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patterns
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        line = line.lstrip("/")
        if line.endswith("/"):
            patterns.append(line.rstrip("/"))
        else:
            patterns.append(line)
    return patterns


def _ignored(rel: Path, gitignore: list[str], extra: tuple[str, ...] | list[str]) -> bool:
    parts = set(rel.parts)
    # Built-in + code-specific directory excludes.
    if parts & (set(DEFAULT_EXCLUDES) | CODE_EXCLUDE_DIRS):
        return True
    names = [rel.name, *rel.parts, rel.as_posix()]
    for pat in (*gitignore, *extra):
        if "/" not in pat and "*" not in pat and "?" not in pat and "[" not in pat:
            if pat in parts:
                return True
        elif any(fnmatch.fnmatch(n, pat) for n in names):
            return True
    return False


class CodeConnector:
    kind = "code"

    def scan(self, config: dict) -> Iterator[ItemRef]:
        """Yield one ItemRef per source file under the configured repo roots."""
        roots = [Path(p).expanduser().resolve() for p in config.get("paths", [])]
        excludes = tuple(config.get("exclude", ()))
        for root in roots:
            if not root.is_dir():
                continue
            gitignore = _load_gitignore_patterns(root)
            for path in sorted(root.rglob("*")):
                if not path.is_file() and not path.is_symlink():
                    continue
                if path.is_symlink() and not path.exists():
                    continue
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                if _ignored(rel, gitignore, excludes):
                    continue
                if path.suffix.lower() not in CODE_SUFFIXES:
                    continue
                yield ItemRef(
                    external_id=f"{root}::{rel.as_posix()}",
                    uri=path.resolve().as_uri(),
                    title=rel.as_posix(),
                )

    def open_uri(self, external_id: str) -> str:
        _, _, rel = external_id.partition("::")
        return rel or external_id

    def watch(self, config: dict, emit: Callable[[Change], None]) -> Callable[[], None]:
        """Watch repo roots; git HEAD changes surface as file events (§6.2)."""
        from core.connectors.base import watch_connector

        return watch_connector(self, 0, config, emit)
