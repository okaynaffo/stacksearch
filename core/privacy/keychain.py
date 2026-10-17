"""macOS Keychain access via the ``security`` CLI (PLAN.md §9, TODO 5.1).

Email credentials live in the Keychain, never in config files. The stdlib
``/usr/bin/security`` tool is used directly rather than the ``keyring`` package,
which keeps the runtime dependency set unchanged (the ``phase5`` extra that
would carry it does not install cleanly here). On non-macOS hosts these
functions degrade to a clear error rather than silently dropping a secret.
"""

from __future__ import annotations

import platform
import shutil
import subprocess

SECURITY_BIN = "/usr/bin/security"


class KeychainError(RuntimeError):
    """Raised when the Keychain cannot be read or written."""


def _security() -> str:
    if platform.system() != "Darwin" or not shutil.which(SECURITY_BIN):
        raise KeychainError("macOS 'security' tool not available on this host")
    return SECURITY_BIN


def set_secret(service: str, account: str, password: str) -> None:
    """Store or update a generic password."""
    proc = subprocess.run(
        [_security(), "add-generic-password", "-a", account, "-s", service, "-w", password, "-U"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise KeychainError(f"failed to store secret {account!r}: {proc.stderr.strip()}")


def get_secret(service: str, account: str) -> str | None:
    """Return the stored password, or None when no item exists."""
    proc = subprocess.run(
        [_security(), "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def delete_secret(service: str, account: str) -> bool:
    """Delete a stored password; returns True if an item was removed."""
    proc = subprocess.run(
        [_security(), "delete-generic-password", "-a", account, "-s", service],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0
