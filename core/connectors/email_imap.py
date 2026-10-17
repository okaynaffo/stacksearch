"""IMAP email connector (§6.2, TODO 5.1).

Requires the optional ``phase5`` extra (``imapclient``), imported lazily so the
local-first path never depends on it. Credentials come from the macOS Keychain
(:mod:`core.privacy.keychain`) keyed by the account address; the sync cursor is
``UIDVALIDITY`` + highest UID per folder, stored as JSON in ``sources.cursor``
and advanced by the worker after each successful index (§6.2).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from urllib.parse import quote

from core.connectors.base import Change, ItemRef, RawItem
from core.extract.email import parse_email_bytes
from core.privacy import keychain

#: Keychain service name for IMAP credentials (PLAN.md §9).
KEYCHAIN_SERVICE = "stacksearch-imap"

DEFAULT_PORT = 993


def _require_imapclient():
    try:
        import imapclient  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "IMAP support needs the optional phase5 extra: `uv sync --extra phase5`"
        ) from exc
    return imapclient


def load_cursor(cursor_json: str | None) -> dict[str, dict[str, int]]:
    """Parse ``sources.cursor`` into ``{folder: {"uidvalidity": v, "uid": n}}``."""
    if not cursor_json:
        return {}
    try:
        data = json.loads(cursor_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def advance_cursor(
    cursor_json: str | None, folder: str, uid: int, uidvalidity: int
) -> str:
    """Return an updated cursor JSON string with ``folder`` advanced to ``uid``."""
    data = load_cursor(cursor_json)
    prev = data.get(folder, {})
    if int(prev.get("uidvalidity", 0)) != int(uidvalidity):
        data[folder] = {"uidvalidity": int(uidvalidity), "uid": int(uid)}
    else:
        data[folder] = {"uidvalidity": int(uidvalidity), "uid": max(int(prev.get("uid", 0)), uid)}
    return json.dumps(data, sort_keys=True)


def split_external_id(external_id: str) -> tuple[str, str, int]:
    """Split ``imap://<account>/<folder>::<uid>`` into ``(account, folder, uid)``."""
    prefix, _, uid = external_id.partition("::")
    account, _, folder = prefix.removeprefix("imap://").partition("/")
    return account, folder, int(uid)


class ImapEmailConnector:
    kind = "email_imap"

    def _password(self, config: dict) -> str:
        account = config.get("account")
        if not account:
            raise RuntimeError("imap source is missing an 'account'")
        password = keychain.get_secret(KEYCHAIN_SERVICE, account)
        if password is None:
            raise RuntimeError(f"no IMAP password in the Keychain for {account!r}")
        return password

    def _connect(self, config: dict):
        imapclient = _require_imapclient()
        account = config.get("account")
        if not account:
            raise RuntimeError("imap source is missing an 'account'")
        host = config.get("host") or account.rpartition("@")[2]
        client = imapclient.IMAPClient(
            host,
            port=int(config.get("port", DEFAULT_PORT)),
            ssl=bool(config.get("ssl", True)),
        )
        client.login(account, self._password(config))
        return client

    def scan(self, config: dict) -> Iterator[ItemRef]:
        """Yield one ItemRef per message with a UID above the stored cursor."""
        client = self._connect(config)
        try:
            cursor = load_cursor(config.get("_cursor"))
            account = config["account"]
            for folder in config.get("folders", ["INBOX"]):
                client.select_folder(folder, readonly=True)
                status = client.folder_status(folder, ["UIDVALIDITY"])
                uidvalidity = int(status.get(b"UIDVALIDITY", 0))
                prev = cursor.get(folder, {})
                last = 0
                if int(prev.get("uidvalidity", 0)) == uidvalidity:
                    last = int(prev.get("uid", 0))
                uids = client.search(["UID", f"{last + 1}:*"]) if last else client.search(["ALL"])
                for uid in sorted(uids):
                    if uid <= last:
                        continue
                    yield ItemRef(
                        external_id=f"imap://{account}/{folder}::{uid}",
                        uri="",  # filled in by fetch()
                        title=f"{folder} #{uid}",
                    )
        finally:
            client.logout()

    def fetch(self, config: dict, external_id: str) -> RawItem:
        """Fetch one message's raw RFC822 bytes and its ``message://`` URI."""
        account, folder, uid = split_external_id(external_id)
        client = self._connect(config)
        try:
            client.select_folder(folder, readonly=True)
            status = client.folder_status(folder, ["UIDVALIDITY"])
            fetched = client.fetch([uid], ["RFC822"])
        finally:
            client.logout()
        raw = fetched.get(uid, {}).get(b"RFC822") or b""
        parsed = parse_email_bytes(raw)
        uri = f"message://{quote(parsed.message_id)}" if parsed.message_id else ""
        return RawItem(
            data=raw,
            uri=uri,
            title=parsed.subject,
            author=parsed.sender,
            modified_at=parsed.date,
            meta={"uidvalidity": int(status.get(b"UIDVALIDITY", 0)), "folder": folder, "uid": uid},
        )

    def open_uri(self, external_id: str) -> str:
        return external_id

    def watch(self, config: dict, emit: Callable[[Change], None]) -> Callable[[], None]:
        """IMAP has no push watch in v1; reconcile scans are the change path (§6.2)."""
        return lambda: None
