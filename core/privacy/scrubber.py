"""Secret scrubbing at chunk time (PLAN.md §9, TODO 7.1).

Matching spans are redacted from the text before it is chunked, embedded, and
stored, so secrets never reach the index. The set of patterns is deliberately
conservative: it targets well-known token shapes and private-key blocks rather
than attempting to guess at arbitrary high-entropy strings, which would risk
redacting legitimate content.
"""

from __future__ import annotations

import re

#: Each pattern maps a compiled regex to a replacement label.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # AWS access key id
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-key]"),
    # GitHub tokens
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "[REDACTED:github-token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-pat]"),
    # OpenAI / generic sk- keys
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED:api-key]"),
    # Slack tokens
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    # Private key blocks
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED:private-key]",
    ),
    # Bearer tokens in headers
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [REDACTED]"),
    # Generic key=value assignments for common secret field names
    (
        re.compile(r'(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*["\']?([A-Za-z0-9._\-]{16,})["\']?'),
        r"\1=[REDACTED]",
    ),
    # JWTs
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "[REDACTED:jwt]",
    ),
)


def scrub(text: str) -> tuple[str, int]:
    """Redact secret-like spans. Returns ``(scrubbed_text, redaction_count)``."""
    count = 0
    out = text
    for pattern, replacement in _PATTERNS:
        new, n = pattern.subn(replacement, out)
        out = new
        count += n
    return out, count


def scrubbing_enabled(conn) -> bool:
    """Read the meta flag (defaults to enabled)."""
    try:
        from core.index import get_meta

        value = get_meta(conn, "secret_scrubbing_enabled", "1")
    except Exception:  # noqa: BLE001
        return True
    return str(value).strip() not in {"0", "false", "no", "off", ""}
