"""Convert database rows into stable API representations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .config import Settings


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def version_info(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version["id"],
        "fetched_at": version["fetched_at"],
        "sha256": version["sha256"],
        "content_hash": version["content_hash"],
        "page_count": version["page_count"],
        "size_bytes": version["size_bytes"],
        "content_changed": bool(version["content_changed"]),
        "text_extractable": bool(version["text_extractable"]),
        "previous_version_id": version["previous_version_id"],
        "warnings": _json_list(version.get("warnings_json")),
    }


def check_info(check: dict[str, Any] | None) -> dict[str, Any] | None:
    if check is None:
        return None
    return {
        "checked_at": check["checked_at"],
        "outcome": check["outcome"],
        "http_status": check["http_status"],
        "duration_ms": check["duration_ms"],
        "detail": check["detail"],
    }


def change_summary(change: dict[str, Any] | None) -> dict[str, Any] | None:
    if change is None:
        return None
    return {
        "id": change["id"],
        "document_key": change["document_key"],
        "detected_at": change["detected_at"],
        "headline": change["headline"],
        "summary": _json_object(change["summary_json"]),
        "from_version_id": change["from_version_id"],
        "to_version_id": change["to_version_id"],
        "detail_url": f"/api/v1/documents/{change['document_key']}/changes/{change['id']}",
    }


def build_document_status(
    conn: sqlite3.Connection, settings: Settings, key: str
) -> dict[str, Any] | None:
    document = db.get_document(conn, key)
    if document is None:
        return None
    current_version = db.get_version(conn, document["current_version_id"])
    latest_change = db.get_latest_change(conn, key)
    last_check = db.get_last_check(conn, key)
    return {
        "key": document["key"],
        "title": document["title"],
        "url": document["url"],
        "tags": _json_list(document["tags_json"]),
        "source": {
            "etag": document["etag"],
            "last_modified": document["last_modified"],
            "last_full_check_at": document["last_full_check_at"],
        },
        "monitor": {
            "scheduler_enabled": settings.scheduler_enabled,
            "check_interval_minutes": settings.check_interval_minutes,
            "last_checked_at": document["last_checked_at"],
            "last_success_at": document["last_success_at"],
            "last_http_status": document["last_http_status"],
            "consecutive_failures": document["consecutive_failures"],
            "last_error": document["last_error"],
        },
        "content": version_info(current_version),
        "file_changed_at": document["file_changed_at"],
        "content_changed_at": document["content_changed_at"],
        "latest_change": change_summary(latest_change),
        "last_check": check_info(last_check),
    }


def build_document_list(
    conn: sqlite3.Connection, settings: Settings
) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for document in db.list_documents(conn):
        status = build_document_status(conn, settings, document["key"])
        if status is not None:
            statuses.append(status)
    return statuses


def build_change_list(
    conn: sqlite3.Connection, document_key: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    return [
        change_summary(change)
        for change in db.list_changes(conn, document_key, limit, offset)
        if change is not None
    ]


def build_version_list(
    conn: sqlite3.Connection, document_key: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    return [
        version_info(version)
        for version in db.list_versions(conn, document_key, limit, offset)
        if version is not None
    ]


def build_change_detail(
    conn: sqlite3.Connection, document_key: str, change_id: int
) -> dict[str, Any] | None:
    change = db.get_change(conn, document_key, change_id)
    if change is None:
        return None
    from_version = db.get_version(conn, change["from_version_id"])
    to_version = db.get_version(conn, change["to_version_id"])
    if to_version is None:
        return None
    diff = _json_object(change["diff_json"])
    return {
        "id": change["id"],
        "document_key": change["document_key"],
        "detected_at": change["detected_at"],
        "headline": change["headline"],
        "summary": _json_object(change["summary_json"]),
        "from_version": version_info(from_version),
        "to_version": version_info(to_version),
        "diff": diff,
        "unified_diff": change["unified_diff"],
        "markdown": change["markdown"],
        "truncated": bool(diff.get("truncated", False)),
    }
