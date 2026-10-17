# Phase 5 findings: more sources

Corpus: synthetic + fixture files only. No ONNX in the connector tests
(`embed=False`), so this pass measures parsing, chunking, indexing, and cursors
rather than retrieval quality. 28 Phase 5 tests (`test_phase5_connectors.py`,
`test_phase5_sources.py`, `test_phase5_email.py`, `test_phase5_email_sources.py`).

## 1. Email (TODO 5.1)

Email is the sixth and last source type, so Phase 5's "all six source types are
indexed" is now met for the local-first path.

**Local-first by default.** `core/extract/email.py` and
`core/connectors/email.py` cover `.eml`, Apple Mail `.emlx`, `mbox`, and
Maildir using only the stdlib (`email`, `mailbox`, `html.parser`). This is the
same optional-dependency discipline the tree-sitter code chunker already uses,
and it matters here because the `phase5` extra does not install cleanly in this
environment: `uv sync --extra phase5` stalls, and `imapclient` / `mail-parser` /
`trafilatura` / `tree-sitter` / `keyring` are all absent from the venv.

**Two deviations from the plan text, both forced by the dependency stall and
both documented inline:**

1. **`mail-parser` → stdlib `email` + `mailbox`.** The stdlib parsers handle
   RFC822, MIME multipart, charset decoding, and HTML fallback without an extra.
2. **`keyring` → `/usr/bin/security`.** `core/privacy/keychain.py` shells out to
   the macOS `security` CLI rather than adding a package. Credentials stay in the
   Keychain (§9); the account address is the lookup key, service `stacksearch-imap`.

**IMAP is implemented but behind the optional extra.** `core/connectors/email_imap.py`
lazy-imports `imapclient`, stores a per-folder `UIDVALIDITY` + highest-UID cursor
as JSON in `sources.cursor`, and opens mail via `message://<id>`. It cannot be
exercised against a live server here, so its scan/fetch/cursor logic is pinned
by a fake `imapclient` module in `test_phase5_email_sources.py` (three messages,
UIDVALIDITY 7; asserts the cursor advances to `{"uid": 1, "uidvalidity": 7}`
after indexing UID 1).

**New connector surface.** Network items have no filesystem path, so
`core/connectors/base.py` gains an optional `RawItem` + `fetch()` hook. File-backed
connectors omit it and the worker resolves a path exactly as before; IMAP returns
bytes, URI, title, author, and date. `run_index_doc` prefers `fetch()` when
present, which keeps the existing file pipeline untouched.

**Semantics.** One chunk per message body (`[email] From: … Subject: …`, ≤ 500
tokens, packed by paragraph with a hard-split last resort). Quoted replies below
`On … wrote:` and `-- ` signatures are stripped by the existing
`normalize_text`; attachments are excluded during body selection. Single-message
containers (`.eml`/`.emlx`/IMAP) set document `author`/`modified_at` from the
headers; multi-message containers (mbox/Maildir) leave document-level metadata
empty because it is ambiguous at file granularity.

## 2. Deferred

- **IMAP flag-sync for deletions (§6.2).** The cursor covers new mail; removing a
  message server-side is not yet reflected. Reconcile scans index new UIDs only.
- **Browser opt-in page-text fetch (TODO 5.2).** Still title + URL; the only
  remaining Phase 5 item.
- **`tree-sitter-language-pack`.** Code still uses the regex fallback; the
  `phase5` install issue is the blocker, not the code.
