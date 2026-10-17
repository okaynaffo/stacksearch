"""Exclusions, secret scrubbing, keychain (PLAN.md §9)."""

from .scrubber import scrub, scrubbing_enabled

__all__ = ["scrub", "scrubbing_enabled"]
