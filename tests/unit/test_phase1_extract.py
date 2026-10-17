"""Phase 1 extractor tests (PLAN.md §6.3, TODO 1.1)."""

from __future__ import annotations

import pytest

from core.extract.normalize import estimate_tokens, is_binary, normalize_text

pytestmark = pytest.mark.unit


def test_normalize_collapses_whitespace():
    assert normalize_text("hello   \t  world") == "hello world"


def test_normalize_strips_quoted_reply():
    text = "Answer here.\nOn Monday, Bob wrote:\nold quoted text"
    assert "old quoted" not in normalize_text(text)
    assert "Answer here." in normalize_text(text)


def test_normalize_strips_signature():
    assert normalize_text("Hi\n-- \nJohn Doe") == "Hi"


def test_normalize_bytes_input():
    assert normalize_text("héllo".encode()) == "héllo"


def test_is_binary_detects_nulls():
    assert is_binary(b"hello\x00world") is True
    assert is_binary(b"plain text here") is False


def test_estimate_tokens_heuristic():
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 1


def test_docx_extraction(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "doc.docx"
    doc = docx.Document()
    doc.add_paragraph("Hello docx world")
    doc.save(str(path))
    from core.extract.text import extract_docx

    assert "Hello docx world" in extract_docx(path)


def test_extract_by_suffix_rejects_unknown(tmp_path):
    from core.extract.text import extract_by_suffix

    path = tmp_path / "file.bin"
    path.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        extract_by_suffix(path)


def test_pdf_text_extraction(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "a.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello PDF world")
    doc.save(str(path))
    doc.close()
    from core.extract.pdf import extract_pdf

    result = extract_pdf(path)
    assert len(result.pages) == 1
    assert result.pages[0].page == 1
    assert "Hello PDF world" in result.pages[0].text


def test_pdf_blank_page_records_skip(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    from core.extract.pdf import extract_pdf

    result = extract_pdf(path)
    # No text layer and (usually) no OCR backend in CI → skipped page recorded.
    assert result.pages == [] or result.pages[0].ocr in (True, False)
    if not result.pages:
        assert result.skipped_pages == [1]
