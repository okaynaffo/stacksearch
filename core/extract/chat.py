"""Chat export parsers: WhatsApp .txt, Telegram JSON, Discord JSON (§6.2, TODO 5.3).

All three normalize to ``(conversation_id, [Message])``. No third-party
dependencies — stdlib only. Unparseable lines/entries are skipped, never fatal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: ``[12/03/2025, 10:15:23] Alice: hello`` (iOS + Android bracket styles).
_WHATSAPP_BRACKET_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+(?P<sender>[^:]+):\s(?P<text>.*)$"
)
#: ``12/03/2025, 10:15 - Alice: hello`` (Android plain style).
_WHATSAPP_PLAIN_RE = re.compile(
    r"^(?P<ts>\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)"
    r"\s*[-–]\s*(?P<sender>[^:]+):\s(?P<text>.*)$"
)


@dataclass(frozen=True)
class Message:
    sender: str
    ts: str | None
    text: str


def parse_chat_export(path: Path) -> tuple[str, list[Message]]:
    """Parse an export file; conversation id defaults to the file stem."""
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _parse_whatsapp(path)
    if suffix == ".json":
        return _parse_json_export(path)
    raise ValueError(f"unsupported chat export suffix: {suffix} ({path})")


def _parse_whatsapp(path: Path) -> tuple[str, list[Message]]:
    messages: list[Message] = []
    current: Message | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _WHATSAPP_BRACKET_RE.match(line) or _WHATSAPP_PLAIN_RE.match(line)
        if m:
            if current is not None:
                messages.append(current)
            current = Message(
                sender=m.group("sender").strip(),
                ts=m.group("ts").strip(),
                text=m.group("text").strip(),
            )
        elif current is not None:
            # Continuation of a multi-line message.
            current = Message(sender=current.sender, ts=current.ts, text=f"{current.text}\n{line}")
        # Else: system line before any message (e.g. encryption notice) — skip.
    if current is not None:
        messages.append(current)
    if not messages:
        raise ValueError(f"no parseable WhatsApp messages in {path}")
    return path.stem, messages


def _flatten_text(value) -> str:
    """Telegram/Discord text may be a string, or a list of strings/entities."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(value) if value is not None else ""


def _parse_json_export(path: Path) -> tuple[str, list[Message]]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data.get("name") or path.stem, _parse_telegram(data["messages"])
    if isinstance(data, list):
        return path.stem, _parse_discord(data)
    raise ValueError(f"unrecognized chat JSON shape in {path}")


def _parse_telegram(entries: list) -> list[Message]:
    messages: list[Message] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("type") not in (None, "message"):
            # Skip service entries unless they carry text.
            if not isinstance(e, dict) or not e.get("text"):
                continue
        text = _flatten_text(e.get("text")).strip()
        if not text:
            continue
        sender = str(e.get("from") or e.get("actor") or "unknown")
        ts = e.get("date")
        messages.append(Message(sender=sender, ts=str(ts) if ts else None, text=text))
    return messages


def _parse_discord(entries: list) -> list[Message]:
    messages: list[Message] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        content = e.get("content")
        text = content.strip() if isinstance(content, str) else _flatten_text(content).strip()
        if not text:
            continue
        author = e.get("author")
        sender = (
            author.get("username", "unknown")
            if isinstance(author, dict)
            else str(e.get("author") or "unknown")
        )
        ts = e.get("timestamp")
        messages.append(Message(sender=sender, ts=str(ts) if ts else None, text=text))
    return messages
