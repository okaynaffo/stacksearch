"""PDF extraction via PyMuPDF with OCR fallback (PLAN.md §6.2, §6.3, TODO 1.1).

Page-level text with ``{"page": N}`` locators. OCR runs only for pages with no
text layer: Apple Vision first (macOS), Tesseract CLI as fallback. If neither is
available the page is recorded as skipped, not silently dropped.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.extract.normalize import normalize_text


@dataclass(frozen=True)
class PdfPage:
    page: int  # 1-based, matches what a reader UI shows
    text: str
    ocr: bool = False


@dataclass(frozen=True)
class PdfResult:
    pages: list[PdfPage]
    skipped_pages: list[int]


def _try_apple_vision_ocr(png: bytes) -> str | None:
    """OCR one page PNG via Apple Vision. Returns None when unavailable."""
    try:
        import Vision  # type: ignore[import]  # noqa: F401
    except ImportError:
        return None
    # PyObjC Vision bindings vary across macOS releases; kept as a best-effort
    # hook rather than a hard dependency. Until wired per-release, report
    # unavailable so the Tesseract fallback is attempted.
    _ = png
    return None


def _try_tesseract_ocr(png: bytes) -> str | None:
    """OCR one page PNG via the Tesseract CLI. Returns None when unavailable."""
    if shutil.which("tesseract") is None:
        return None
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng"],
            input=png,
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace").strip()
    return text or None


def extract_pdf(path: Path) -> PdfResult:
    """Extract per-page text from a PDF."""
    import fitz

    pages: list[PdfPage] = []
    skipped: list[int] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            raw = (page.get_text("text") or "").strip()
            if raw:
                text = normalize_text(raw)
                if text:
                    pages.append(PdfPage(page=i + 1, text=text))
                continue
            # No text layer → OCR fallback (§6.3: only for such pages).
            pix = page.get_pixmap(dpi=200)
            png = pix.tobytes("png")
            ocr_text: str | None = _try_apple_vision_ocr(png) or _try_tesseract_ocr(png)
            if ocr_text:
                pages.append(PdfPage(page=i + 1, text=normalize_text(ocr_text), ocr=True))
            else:
                skipped.append(i + 1)
    return PdfResult(pages=pages, skipped_pages=skipped)
