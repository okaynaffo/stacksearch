"""Code chunker: one chunk per function/class, small files whole (§6.4, TODO 5.4).

Uses tree-sitter when importable (``tree-sitter-language-pack`` is a Phase 5
optional extra) and falls back to a regex symbol splitter otherwise, so the
pipeline never hard-depends on the extra. Either way each chunk carries
``loc_json`` with ``{"symbol", "line_start", "line_end"}`` and text prefixed
with a ``[code] path::symbol`` header line (§6.4).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from core.config import get_settings
from core.extract.normalize import estimate_tokens

#: Suffix → tree-sitter-language-pack language name. Only consulted when the
#: package is importable; unknown suffixes use the regex fallback.
SUFFIX_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".lua": "lua",
    ".sh": "bash",
}

#: Node types that delimit a code chunk across tree-sitter grammars.
_SYMBOL_NODE_TYPES = frozenset(
    {
        "function_definition",
        "function_declaration",
        "method_definition",
        "class_definition",
        "class_declaration",
        "struct_item",
        "enum_item",
        "impl_item",
        "interface_declaration",
    }
)

#: Regex fallback: ``<keyword> <symbol>`` at line start (plus Go methods).
_FALLBACK_RE = re.compile(
    r"^\s*(?:"
    r"def\s+(?P<py>[\w]+)"
    r"|class\s+(?P<cls>[\w]+)"
    r"|function\s+(?P<fn>[\w$]+)"
    r"|func\s+(?:\([^)]*\)\s*)?(?P<go>[\w]+)"
    r"|fn\s+(?P<rs>[\w]+)"
    r"|(?:public|private|protected|static|async|export|default|\s)*"
    r"(?:class|function|def|sub)\s+(?P<gen>[\w$]+)"
    r")\b"
)


@dataclass(frozen=True)
class CodeChunk:
    text: str  # includes the [code] header line
    loc_json: str
    token_count: int


def _loc(symbol: str | None, line_start: int, line_end: int) -> str:
    return json.dumps(
        {"symbol": symbol, "line_start": line_start, "line_end": line_end}, sort_keys=True
    )


def _hard_split(body: str, max_tokens: int) -> list[str]:
    """Split an oversized body by characters; last resort, keeps order."""
    step = max_tokens * 4
    return [body[i : i + step] for i in range(0, len(body), step)] or [body]


def split_code(
    text: str,
    *,
    header: str,
    language: str | None = None,
    max_tokens: int | None = None,
) -> list[CodeChunk]:
    """Split source text into per-symbol chunks (§6.4: ≤ 600 tokens each).

    ``header`` is the ``[code] relpath`` prefix; the symbol is appended as
    ``header::symbol``. Files at or under budget are kept whole.
    """
    settings = get_settings()
    max_tokens = max_tokens or settings.indexing.code_chunk_max_tokens
    lines = (text or "").splitlines()
    if not lines:
        return []
    if estimate_tokens(text) <= max_tokens:
        full = f"{header}\n\n{text}" if header else text
        return [
            CodeChunk(
                text=full,
                loc_json=_loc(None, 1, len(lines)),
                token_count=estimate_tokens(full),
            )
        ]

    spans = _split_treesitter(text, language) or _split_regex(lines)
    chunks: list[CodeChunk] = []
    for symbol, start, end in spans:
        body = "\n".join(lines[start - 1 : end])
        bodies = [body] if estimate_tokens(body) <= max_tokens else _hard_split(
            body, max_tokens
        )
        for i, piece in enumerate(bodies):
            sym = symbol if len(bodies) == 1 else f"{symbol}#part{i + 1}"
            head = f"{header}::{sym}" if header and sym else header
            full = f"{head}\n\n{piece}" if head else piece
            chunks.append(
                CodeChunk(
                    text=full,
                    loc_json=_loc(sym, start, end),
                    token_count=estimate_tokens(full),
                )
            )
    return chunks


def _split_regex(lines: list[str]) -> list[tuple[str | None, int, int]]:
    """Symbol spans via the ``keyword + name`` heuristic.

    Returns ``(symbol, line_start, line_end)`` 1-indexed; trailing
    non-symbol text attaches to the preceding chunk, leading text becomes a
    ``(None, …)`` chunk so nothing is dropped.
    """
    marks: list[tuple[str, int]] = []
    for i, line in enumerate(lines, start=1):
        m = _FALLBACK_RE.match(line)
        if m:
            symbol = next((g for g in m.groups() if g), None)
            if symbol:
                marks.append((symbol, i))
    if not marks:
        return [(None, 1, len(lines))]
    spans: list[tuple[str | None, int, int]] = []
    if marks[0][1] > 1:
        spans.append((None, 1, marks[0][1] - 1))
    for idx, (symbol, start) in enumerate(marks):
        end = marks[idx + 1][1] - 1 if idx + 1 < len(marks) else len(lines)
        spans.append((symbol, start, max(end, start)))
    return spans


def _split_treesitter(text: str, language: str | None) -> list[tuple[str, int, int]] | None:
    """Symbol spans via tree-sitter; None when unavailable or on any failure."""
    if not language:
        return None
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]

        parser = get_parser(language)
        tree = parser.parse(text.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — optional extra; regex fallback covers it
        return None
    try:
        lines = text.splitlines()
        spans: list[tuple[str, int, int]] = []
        _collect_symbols(tree.root_node, text, spans)
        if not spans:
            return None
        spans.sort(key=lambda s: s[1])
        # Attach leading text before the first symbol so nothing is dropped.
        if spans[0][1] > 1:
            spans.insert(0, (None, 1, spans[0][1] - 1))
        # Clamp ends to the next symbol start (grammars include trailing trivia).
        clamped: list[tuple[str | None, int, int]] = []
        for idx, (symbol, start, end) in enumerate(spans):
            if idx + 1 < len(spans):
                end = min(end, spans[idx + 1][1] - 1)
            clamped.append((symbol, start, max(end, start)))
        _ = lines
        return clamped
    except Exception:  # noqa: BLE001 — never break indexing on a parse quirk
        return None


def _collect_symbols(node, source: str, out: list[tuple[str, int, int]]) -> None:
    """Depth-first collect of top-level symbol nodes (nested walk, top record)."""
    for child in node.children:
        is_symbol = child.type in _SYMBOL_NODE_TYPES
        parent_is_symbol = (
            child.parent is not None and child.parent.type in _SYMBOL_NODE_TYPES
        )
        if is_symbol and not parent_is_symbol:
            name_node = child.child_by_field_name("name")
            symbol = (
                source[name_node.start_byte : name_node.end_byte] if name_node else None
            )
            out.append((symbol, child.start_point[0] + 1, child.end_point[0] + 1))
        _collect_symbols(child, source, out)
