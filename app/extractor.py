"""PDF extraction and content fingerprinting.

Text is normalised so that pure typesetting noise (extra spaces, CRLF, Unicode
compatibility forms) does not look like a content change. Table structure is
kept separately so that cell-level changes can be reported.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import pymupdf

# The built-in table detector is sufficient for these rule-based tables. Hide the
# one-off suggestion printed by PyMuPDF about an optional layout add-on.
pymupdf.no_recommend_layout()


class PdfExtractionError(RuntimeError):
    """Raised when a PDF cannot be opened or parsed."""


_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]")


def normalize_text(text: str) -> str:
    """Normalise a multi-line block while keeping line boundaries."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\u00ad", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    text = _CONTROL_RE.sub("", text)
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines).strip()
    return _BLANK_LINES_RE.sub("\n\n", text)


def normalize_cell(value: Any) -> str:
    """Normalise a table cell into one line."""
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", normalize_text(str(value)).replace("\n", " ")).strip()


def _clean_rows(rows: Iterable[Iterable[Any]]) -> list[list[str]]:
    cleaned: list[list[str]] = []
    for raw_row in rows:
        row = [normalize_cell(cell) for cell in raw_row]
        if any(row):
            cleaned.append(row)
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    padded = [row + [""] * (width - len(row)) for row in cleaned]
    while padded and padded[0] and not any(padded[0]):
        padded.pop(0)
    while padded and padded[-1] and not any(padded[-1]):
        padded.pop()
    # Drop trailing columns that are empty in every row (usually extractor noise).
    while width > 0 and all(row[width - 1] == "" for row in padded):
        for row in padded:
            row.pop()
        width -= 1
    return padded


def canonical_page(page: dict[str, Any]) -> dict[str, Any]:
    """The subset of a page that defines its content for hashing and diffing."""
    return {
        "page": page["page"],
        "text": page.get("text", ""),
        "tables": [
            {"rows": table.get("rows", [])} for table in page.get("tables", [])
        ],
    }


def page_signature(page: dict[str, Any]) -> str:
    """Content-only page identity, independent of the page's ordinal position."""
    payload = json.dumps(
        {
            "text": page.get("text", ""),
            "tables": [
                {"rows": table.get("rows", [])} for table in page.get("tables", [])
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_content_hash(pages: Iterable[dict[str, Any]]) -> str:
    payload = [canonical_page(page) for page in pages]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def extract_pdf(path: Path) -> dict[str, Any]:
    """Extract text and tables from a PDF.

    Returns a JSON-serialisable snapshot. ``content_hash`` covers only
    normalised text and table cell values, not PDF metadata or coordinates.
    """
    try:
        document = pymupdf.open(path)
    except Exception as exc:  # pragma: no cover - depends on malformed input
        raise PdfExtractionError(f"Cannot open PDF: {exc}") from exc

    pages: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_chars = 0
    try:
        if document.needs_pass:
            raise PdfExtractionError("PDF is encrypted and requires a password")
        for index, page in enumerate(document, start=1):
            try:
                text = normalize_text(page.get_text("text", sort=True))
            except Exception as exc:  # pragma: no cover - PyMuPDF edge cases
                text = ""
                warnings.append(f"page {index}: text extraction failed: {exc}")

            tables: list[dict[str, Any]] = []
            try:
                finder = page.find_tables()
                for table_index, table in enumerate(finder.tables, start=1):
                    try:
                        rows = _clean_rows(table.extract() or [])
                    except Exception as exc:  # pragma: no cover
                        warnings.append(
                            f"page {index}, table {table_index}: extraction failed: {exc}"
                        )
                        continue
                    if not rows:
                        continue
                    tables.append(
                        {
                            "rows": rows,
                            "bbox": [round(float(v), 2) for v in table.bbox],
                        }
                    )
            except Exception as exc:  # pragma: no cover
                warnings.append(f"page {index}: table detection failed: {exc}")

            total_chars += len(text.replace("\n", "").replace(" ", ""))
            total_chars += sum(
                len(cell.replace(" ", ""))
                for table in tables
                for row in table["rows"]
                for cell in row
            )
            pages.append({"page": index, "text": text, "tables": tables})

        metadata = {
            key: str(value)
            for key, value in (document.metadata or {}).items()
            if value is not None and str(value).strip()
        }
        page_count = document.page_count
    finally:
        document.close()

    text_extractable = total_chars >= 80
    if not text_extractable:
        warnings.append(
            "No meaningful text layer was found; only binary changes can be detected. "
            "Visual/OCR comparison would be required for this document."
        )

    return {
        "page_count": page_count,
        "pages": pages,
        "metadata": metadata,
        "warnings": warnings,
        "text_extractable": text_extractable,
        "total_chars": total_chars,
        "content_hash": compute_content_hash(pages),
    }
