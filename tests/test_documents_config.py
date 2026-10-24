"""Regression guard for the production documents.yaml registry."""

from __future__ import annotations

from app.config import PROJECT_ROOT, load_documents

CATEGORY_TAGS = {"equivalence-cuhk", "equivalence-sz", "u-core", "other"}


def test_production_documents_config_loads() -> None:
    documents = load_documents(PROJECT_ROOT / "config" / "documents.yaml")
    assert len(documents) >= 8, "expected the full curriculum registry"


def test_production_documents_keys_and_urls_unique() -> None:
    documents = load_documents(PROJECT_ROOT / "config" / "documents.yaml")
    keys = [document.key for document in documents]
    assert len(set(keys)) == len(keys), "duplicate document key"
    urls = [document.url for document in documents]
    assert len(set(urls)) == len(urls), "two documents must not watch the same file"


def test_production_documents_first_tag_is_category() -> None:
    """/updates groups cards by the first tag; keep the convention enforceable."""
    documents = load_documents(PROJECT_ROOT / "config" / "documents.yaml")
    for document in documents:
        assert document.tags, f"{document.key} has no tags"
        assert document.tags[0] in CATEGORY_TAGS, (
            f"{document.key}: first tag {document.tags[0]!r} is not a display category"
        )
