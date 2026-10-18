from __future__ import annotations

from pathlib import Path

from app.differ import diff_snapshots
from app.extractor import canonical_page, extract_pdf, normalize_text
from tests.pdf_factory import make_metadata_variant, make_test_pdf


def test_table_cell_change_is_located(tmp_path: Path) -> None:
    before = tmp_path / "before.pdf"
    after = tmp_path / "after.pdf"
    make_test_pdf(before)
    make_test_pdf(after, code="MATH3271", note="Version two")

    old_snapshot = extract_pdf(before)
    new_snapshot = extract_pdf(after)
    assert old_snapshot["content_hash"] != new_snapshot["content_hash"]

    diff = diff_snapshots(
        old_snapshot,
        new_snapshot,
        document_key="test-doc",
        detected_at="2026-10-19T00:00:00Z",
        from_version_id=1,
        to_version_id=2,
    )
    assert diff["summary"]["pages_changed"] == 1
    assert diff["summary"]["table_cells_modified"] == 1
    assert diff["summary"]["pages_added"] == 0
    assert diff["summary"]["pages_removed"] == 0
    assert diff["pages"][0]["status"] == "modified"
    cell_changes = [
        change
        for change in diff["pages"][0]["table_changes"]
        if change["type"] == "cell_modified"
    ]
    assert cell_changes == [
        {
            "type": "cell_modified",
            "table_index": 1,
            "old_row": 2,
            "new_row": 2,
            "column": 1,
            "old": "MATH3270",
            "new": "MATH3271",
            "row_context": "MATH3271 | Ordinary Differential Equations | MAT2001",
        }
    ]
    assert "MATH3270" in diff["unified_diff"]
    assert "MATH3271" in diff["unified_diff"]
    assert "MATH3270" in diff["markdown"]
    assert "MATH3271" in diff["markdown"]
    assert diff["truncated"] is False


def test_metadata_only_rewrite_has_same_content_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    variant = tmp_path / "variant.pdf"
    make_test_pdf(source)
    make_metadata_variant(source, variant)

    assert source.read_bytes() != variant.read_bytes()
    original = extract_pdf(source)
    rewritten = extract_pdf(variant)
    assert original["content_hash"] == rewritten["content_hash"]
    assert [canonical_page(page) for page in original["pages"]] == [
        canonical_page(page) for page in rewritten["pages"]
    ]


def test_added_page_is_aligned_not_reported_as_all_changed(tmp_path: Path) -> None:
    before = tmp_path / "before.pdf"
    after = tmp_path / "after.pdf"
    make_test_pdf(before)
    make_test_pdf(after, extra_page=True)

    diff = diff_snapshots(
        extract_pdf(before),
        extract_pdf(after),
        document_key="test-doc",
        detected_at="2026-10-19T00:00:00Z",
        from_version_id=1,
        to_version_id=2,
    )
    assert diff["summary"]["pages_added"] == 1
    assert diff["summary"]["pages_changed"] == 0
    assert diff["summary"]["pages_removed"] == 0
    assert diff["pages"][0]["status"] == "added"
    assert diff["pages"][0]["new_page"] == 2


def test_multiple_modified_pages_are_paired_instead_of_add_remove(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before-two-pages.pdf"
    after = tmp_path / "after-two-pages.pdf"
    make_test_pdf(
        before, note="Version one", extra_page=True, second_page_note="Page two before"
    )
    make_test_pdf(
        after,
        code="MATH3271",
        note="Version two",
        extra_page=True,
        second_page_note="Page two after",
    )

    diff = diff_snapshots(
        extract_pdf(before),
        extract_pdf(after),
        document_key="test-doc",
        detected_at="2026-10-19T00:00:00Z",
        from_version_id=1,
        to_version_id=2,
    )
    assert diff["summary"]["pages_changed"] == 2
    assert diff["summary"]["pages_added"] == 0
    assert diff["summary"]["pages_removed"] == 0
    assert [page["new_page"] for page in diff["pages"]] == [1, 2]


def test_normalize_text_reduces_layout_noise() -> None:
    raw = "Ａ  Course\r\n\r\n\r\n  Code\t\tMATH3270  \r\n"
    assert normalize_text(raw) == "A Course\n\nCode MATH3270"


def test_pdf_extraction_marks_text_layer(tmp_path: Path) -> None:
    source = tmp_path / "text-layer.pdf"
    make_test_pdf(source)
    snapshot = extract_pdf(source)
    assert snapshot["page_count"] == 1
    assert snapshot["text_extractable"] is True
    assert len(snapshot["pages"][0]["tables"]) == 1
    assert snapshot["warnings"] == []
