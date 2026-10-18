"""SQLite persistence layer.

The database stores document state, immutable version metadata, change reports
and a short audit log of checks. PDF and extraction snapshots live on disk.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from .config import DocumentConfig, Settings
from .timeutil import to_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    etag TEXT,
    last_modified TEXT,
    current_version_id INTEGER,
    file_changed_at TEXT,
    content_changed_at TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_full_check_at TEXT,
    last_http_status INTEGER,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_key TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    pdf_path TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_changed INTEGER NOT NULL DEFAULT 0,
    previous_version_id INTEGER,
    text_extractable INTEGER NOT NULL DEFAULT 1,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (document_key) REFERENCES documents(key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_key TEXT NOT NULL,
    from_version_id INTEGER,
    to_version_id INTEGER NOT NULL UNIQUE,
    detected_at TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    unified_diff TEXT NOT NULL,
    markdown TEXT NOT NULL,
    FOREIGN KEY (document_key) REFERENCES documents(key) ON DELETE CASCADE,
    FOREIGN KEY (to_version_id) REFERENCES versions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_key TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    duration_ms INTEGER,
    detail TEXT,
    FOREIGN KEY (document_key) REFERENCES documents(key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_versions_document ON versions(document_key, id DESC);
CREATE INDEX IF NOT EXISTS idx_changes_document ON changes(document_key, id DESC);
CREATE INDEX IF NOT EXISTS idx_checks_document ON checks(document_key, id DESC);
"""


def connect(settings: Settings) -> sqlite3.Connection:
    settings.ensure_directories()
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings)
    try:
        yield conn
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    settings.ensure_directories()
    conn = connect(settings)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def seed_documents(settings: Settings, documents: Sequence[DocumentConfig]) -> None:
    now = to_iso()
    with transaction(settings) as conn:
        for document in documents:
            conn.execute(
                """
                INSERT INTO documents (key, title, url, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    tags_json = excluded.tags_json,
                    updated_at = excluded.updated_at
                """,
                (
                    document.key,
                    document.title,
                    document.url,
                    json.dumps(list(document.tags), ensure_ascii=False),
                    now,
                    now,
                ),
            )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_document(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM documents WHERE key = ?", (key,)).fetchone())


def list_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM documents ORDER BY key").fetchall()
    ]


def get_version(conn: sqlite3.Connection, version_id: int | None) -> dict[str, Any] | None:
    if version_id is None:
        return None
    return _row(conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone())


def get_latest_version(
    conn: sqlite3.Connection, document_key: str
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM versions WHERE document_key = ? ORDER BY id DESC LIMIT 1",
            (document_key,),
        ).fetchone()
    )


def list_versions(
    conn: sqlite3.Connection, document_key: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM versions
            WHERE document_key = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (document_key, limit, offset),
        ).fetchall()
    ]


def insert_version(
    conn: sqlite3.Connection,
    *,
    document_key: str,
    fetched_at: str,
    sha256: str,
    content_hash: str,
    page_count: int,
    size_bytes: int,
    pdf_path: str,
    snapshot_path: str,
    etag: str | None,
    last_modified: str | None,
    content_changed: bool,
    previous_version_id: int | None,
    text_extractable: bool,
    warnings: Sequence[str],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO versions (
            document_key, fetched_at, sha256, content_hash, page_count, size_bytes,
            pdf_path, snapshot_path, etag, last_modified, content_changed,
            previous_version_id, text_extractable, warnings_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_key,
            fetched_at,
            sha256,
            content_hash,
            page_count,
            size_bytes,
            pdf_path,
            snapshot_path,
            etag,
            last_modified,
            1 if content_changed else 0,
            previous_version_id,
            1 if text_extractable else 0,
            json.dumps(list(warnings), ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def insert_change(
    conn: sqlite3.Connection,
    *,
    document_key: str,
    from_version_id: int | None,
    to_version_id: int,
    detected_at: str,
    headline: str,
    summary: Mapping[str, Any],
    diff: Mapping[str, Any],
    unified_diff: str,
    markdown: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO changes (
            document_key, from_version_id, to_version_id, detected_at, headline,
            summary_json, diff_json, unified_diff, markdown
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_key,
            from_version_id,
            to_version_id,
            detected_at,
            headline,
            json.dumps(summary, ensure_ascii=False),
            json.dumps(diff, ensure_ascii=False),
            unified_diff,
            markdown,
        ),
    )
    return int(cursor.lastrowid)


def update_document_on_not_modified(
    conn: sqlite3.Connection,
    *,
    key: str,
    checked_at: str,
    http_status: int,
    etag: str | None,
    last_modified: str | None,
) -> None:
    conn.execute(
        """
        UPDATE documents SET
            last_checked_at = ?,
            last_success_at = ?,
            last_http_status = ?,
            last_error = NULL,
            consecutive_failures = 0,
            etag = COALESCE(?, etag),
            last_modified = COALESCE(?, last_modified),
            updated_at = ?
        WHERE key = ?
        """,
        (checked_at, checked_at, http_status, etag, last_modified, checked_at, key),
    )


def update_document_on_download(
    conn: sqlite3.Connection,
    *,
    key: str,
    checked_at: str,
    http_status: int,
    etag: str | None,
    last_modified: str | None,
    current_version_id: int | None = None,
    file_changed_at: str | None = None,
    content_changed_at: str | None = None,
) -> None:
    """Record a full 200 response.

    A 200 always means the full body was downloaded, so last_full_check_at is
    refreshed. Validators are replaced by whatever the origin returned.
    """
    conn.execute(
        """
        UPDATE documents SET
            last_checked_at = ?,
            last_success_at = ?,
            last_http_status = ?,
            last_error = NULL,
            consecutive_failures = 0,
            etag = ?,
            last_modified = ?,
            last_full_check_at = ?,
            current_version_id = COALESCE(?, current_version_id),
            file_changed_at = COALESCE(?, file_changed_at),
            content_changed_at = COALESCE(?, content_changed_at),
            updated_at = ?
        WHERE key = ?
        """,
        (
            checked_at,
            checked_at,
            http_status,
            etag,
            last_modified,
            checked_at,
            current_version_id,
            file_changed_at,
            content_changed_at,
            checked_at,
            key,
        ),
    )


def update_document_on_error(
    conn: sqlite3.Connection,
    *,
    key: str,
    checked_at: str,
    http_status: int | None,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE documents SET
            last_checked_at = ?,
            last_http_status = ?,
            last_error = ?,
            consecutive_failures = consecutive_failures + 1,
            updated_at = ?
        WHERE key = ?
        """,
        (checked_at, http_status, error[:2000], checked_at, key),
    )


def insert_check(
    conn: sqlite3.Connection,
    *,
    document_key: str,
    checked_at: str,
    outcome: str,
    http_status: int | None,
    duration_ms: int,
    detail: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO checks (document_key, checked_at, outcome, http_status, duration_ms, detail)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (document_key, checked_at, outcome, http_status, duration_ms, detail),
    )
    conn.execute(
        """
        DELETE FROM checks
        WHERE document_key = ? AND id NOT IN (
            SELECT id FROM checks WHERE document_key = ? ORDER BY id DESC LIMIT 500
        )
        """,
        (document_key, document_key),
    )


def get_last_check(conn: sqlite3.Connection, document_key: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM checks WHERE document_key = ? ORDER BY id DESC LIMIT 1",
            (document_key,),
        ).fetchone()
    )


def list_checks(
    conn: sqlite3.Connection, document_key: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM checks
            WHERE document_key = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (document_key, limit, offset),
        ).fetchall()
    ]


def get_change(
    conn: sqlite3.Connection, document_key: str, change_id: int
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM changes WHERE document_key = ? AND id = ?",
            (document_key, change_id),
        ).fetchone()
    )


def get_latest_change(
    conn: sqlite3.Connection, document_key: str
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM changes WHERE document_key = ? ORDER BY id DESC LIMIT 1",
            (document_key,),
        ).fetchone()
    )


def list_changes(
    conn: sqlite3.Connection, document_key: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM changes
            WHERE document_key = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (document_key, limit, offset),
        ).fetchall()
    ]
