"""Public read-only updates page: has the official document changed?

The ``/updates`` namespace backs the human-facing page and is intentionally
unauthenticated: it exposes only non-sensitive fields (titles, public URLs,
timestamps, change reports) and never triggers a check. It is a page-private
contract versioned together with ``app/static/updates.html``, so it stays out
of the versioned ``/api/v1`` surface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from . import db
from .config import Settings
from .scheduler import MonitorScheduler
from .timeutil import to_iso

PAGE_CHECKS_LIMIT = 60
RECENT_CHANGES_LIMIT = 20

_STATIC_DIR = Path(__file__).resolve().parent / "static"


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


def _monitor_block(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_checked_at": document["last_checked_at"],
        "last_success_at": document["last_success_at"],
        "last_http_status": document["last_http_status"],
        "consecutive_failures": document["consecutive_failures"],
        "last_error": document["last_error"],
    }


def _version_block(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version["id"],
        "fetched_at": version["fetched_at"],
        "sha256": version["sha256"],
        "page_count": version["page_count"],
        "size_bytes": version["size_bytes"],
    }


def _latest_change_block(change: dict[str, Any] | None) -> dict[str, Any] | None:
    if change is None:
        return None
    return {
        "id": change["id"],
        "detected_at": change["detected_at"],
        "headline": change["headline"],
        "summary": _json_object(change["summary_json"]),
    }


def _check_block(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "checked_at": check["checked_at"],
        "outcome": check["outcome"],
        "http_status": check["http_status"],
        "duration_ms": check["duration_ms"],
    }


def build_updates_payload(
    conn: sqlite3.Connection,
    settings: Settings,
    scheduler: MonitorScheduler | None,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    for document in db.list_documents(conn):
        version = db.get_version(conn, document["current_version_id"])
        latest_change = db.get_latest_change(conn, document["key"])
        checks = db.list_checks(conn, document["key"], PAGE_CHECKS_LIMIT, 0)
        documents.append(
            {
                "key": document["key"],
                "title": document["title"],
                "url": document["url"],
                "tags": _json_list(document["tags_json"]),
                "monitor": _monitor_block(document),
                "current_version": _version_block(version),
                "file_changed_at": document["file_changed_at"],
                "content_changed_at": document["content_changed_at"],
                "latest_change": _latest_change_block(latest_change),
                "checks": [_check_block(check) for check in reversed(checks)],
            }
        )
    return {
        "generated_at": to_iso(),
        "scheduler": {
            "enabled": settings.scheduler_enabled,
            "interval_minutes": settings.check_interval_minutes,
            "next_run_at": scheduler.next_run_at if scheduler else None,
        },
        "documents": documents,
        "recent_changes": [
            {
                "id": change["id"],
                "document_key": change["document_key"],
                "document_title": change["document_title"],
                "detected_at": change["detected_at"],
                "headline": change["headline"],
            }
            for change in db.list_recent_changes(conn, RECENT_CHANGES_LIMIT)
        ],
    }


def create_updates_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/updates", include_in_schema=False)
    def updates_page() -> FileResponse:
        return FileResponse(_STATIC_DIR / "updates.html", media_type="text/html")

    @router.get("/updates/data.json", include_in_schema=False)
    def updates_data(request: Request) -> dict[str, Any]:
        scheduler: MonitorScheduler | None = getattr(
            request.app.state, "scheduler", None
        )
        with db.read(settings) as conn:
            return build_updates_payload(conn, settings, scheduler)

    @router.get("/updates/changes/{change_id}", include_in_schema=False)
    def change_detail(change_id: int) -> dict[str, Any]:
        with db.read(settings) as conn:
            change = db.get_change_by_id(conn, change_id)
            if change is None:
                raise HTTPException(status_code=404, detail="Change not found")
            document = db.get_document(conn, change["document_key"])
        diff = _json_object(change["diff_json"])
        return {
            "id": change["id"],
            "document_key": change["document_key"],
            "document_title": document["title"] if document else change["document_key"],
            "detected_at": change["detected_at"],
            "headline": change["headline"],
            "summary": _json_object(change["summary_json"]),
            "diff": diff,
            "unified_diff": change["unified_diff"],
            "truncated": bool(diff.get("truncated", False)),
            "markdown_url": f"/updates/changes/{change_id}/markdown",
        }

    @router.get(
        "/updates/changes/{change_id}/markdown",
        include_in_schema=False,
        response_class=PlainTextResponse,
    )
    def change_markdown(change_id: int) -> PlainTextResponse:
        with db.read(settings) as conn:
            change = db.get_change_by_id(conn, change_id)
            if change is None:
                raise HTTPException(status_code=404, detail="Change not found")
        return PlainTextResponse(
            change["markdown"], media_type="text/markdown; charset=utf-8"
        )

    return router
