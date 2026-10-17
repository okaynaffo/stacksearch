"""Unit tests for the secret scrubber (PLAN.md §9, TODO 7.1)."""

from __future__ import annotations

from core.privacy.scrubber import scrub


def test_scrubs_aws_key():
    text = "key AKIAIOSFODNN7EXAMPLE here"
    out, n = scrub(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert n >= 1


def test_scrubs_openai_key():
    out, n = scrub("token sk-abcdefghijklmnopqrstuvwxyz123456")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out


def test_scrubs_private_key_block():
    pem = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"
    out, n = scrub(f"before {pem} after")
    assert "abc123" not in out
    assert n >= 1


def test_scrubs_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturevalue1234567890"
    out, n = scrub(f"auth: {jwt}")
    assert jwt not in out


def test_clean_text_unchanged():
    out, n = scrub("hello world, no secrets here")
    assert out == "hello world, no secrets here"
    assert n == 0