"""Generate small, deterministic PDFs for tests."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def make_test_pdf(
    path: Path,
    *,
    code: str = "MATH3270",
    note: str = "Version one",
    extra_page: bool = False,
    second_page_note: str = "This page did not exist before.",
) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 60), "Course Equivalence List", fontsize=16)
    page.insert_text((72, 88), note, fontsize=10)
    rows = [
        ["CUHK Code", "CUHK Course", "CUHK(SZ) Code"],
        [code, "Ordinary Differential Equations", "MAT2001"],
        ["MATH3280", "Introductory Probability", "MAT3280"],
    ]
    x0, y0 = 72, 120
    widths = [110, 250, 110]
    row_height = 30
    for row_index, row in enumerate(rows):
        x = x0
        for column_index, value in enumerate(row):
            rect = pymupdf.Rect(
                x,
                y0 + row_index * row_height,
                x + widths[column_index],
                y0 + (row_index + 1) * row_height,
            )
            page.draw_rect(rect, color=(0, 0, 0), width=1)
            page.insert_text(
                (x + 4, y0 + row_index * row_height + 20),
                value,
                fontsize=9,
            )
            x += widths[column_index]

    if extra_page:
        page2 = document.new_page(width=595, height=842)
        page2.insert_text((72, 60), "Additional Notes", fontsize=16)
        page2.insert_text((72, 88), second_page_note, fontsize=10)

    document.set_metadata(
        {
            "title": "Test course list",
            "author": "Test suite",
            "producer": "PyMuPDF test fixture",
        }
    )
    document.save(path)
    document.close()


def make_metadata_variant(source: Path, target: Path) -> None:
    """Rewrite the same pages with different document metadata."""
    document = pymupdf.open(source)
    document.set_metadata(
        {
            **document.metadata,
            "producer": "PyMuPDF metadata-only variant",
            "creator": "Different creator",
            "modDate": "D:20261019000100+08'00'",
        }
    )
    document.save(target, garbage=3, deflate=True)
    document.close()
