"""Monitoring service: fetch, fingerprint, snapshot, diff and persist."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import db
from .config import DocumentConfig, Settings
from .differ import diff_snapshots
from .extractor import PdfExtractionError, extract_pdf
from .fetcher import FetchError, fetch_pdf
from .timeutil import parse_iso, to_iso, utc_now

logger = logging.getLogger(__name__)

EMPTY_SUMMARY = {
    "pages_changed": 0,
    "pages_added": 0,
    "pages_removed": 0,
    "text_lines_added": 0,
    "text_lines_removed": 0,
    "tables_added": 0,
    "tables_removed": 0,
    "rows_added": 0,
    "rows_removed": 0,
    "table_cells_modified": 0,
    "table_cells_added": 0,
    "table_cells_removed": 0,
}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MonitorService:
    def __init__(self, settings: Settings, documents: tuple[DocumentConfig, ...]):
        self.settings = settings
        self.documents = {document.key: document for document in documents}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check_all(self, *, force_full: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for document in self.documents.values():
            try:
                results.append(
                    await self.check_document(document.key, force_full=force_full)
                )
            except Exception as exc:  # keep one bad document from stopping the rest
                logger.exception("Unhandled error while checking %s", document.key)
                results.append(
                    {
                        "key": document.key,
                        "outcome": "error",
                        "changed": False,
                        "change_id": None,
                        "http_status": None,
                        "checked_at": to_iso(),
                        "duration_ms": 0,
                        "message": f"Unhandled error: {exc}",
                    }
                )
        return results

    async def check_document(
        self, key: str, *, force_full: bool = False
    ) -> dict[str, Any]:
        if key not in self.documents:
            raise KeyError(key)
        async with self._locks[key]:
            started = time.perf_counter()
            try:
                result = await self._check_document(key, force_full=force_full)
            except Exception as exc:
                logger.exception("Check failed for %s", key)
                checked_at = to_iso()
                message = f"{type(exc).__name__}: {exc}"
                try:
                    with db.transaction(self.settings) as conn:
                        db.update_document_on_error(
                            conn,
                            key=key,
                            checked_at=checked_at,
                            http_status=getattr(exc, "status_code", None),
                            error=message,
                        )
                        db.insert_check(
                            conn,
                            document_key=key,
                            checked_at=checked_at,
                            outcome="error",
                            http_status=getattr(exc, "status_code", None),
                            duration_ms=int((time.perf_counter() - started) * 1000),
                            detail=message,
                        )
                except Exception:
                    logger.exception("Could not persist failure for %s", key)
                result = {
                    "key": key,
                    "outcome": "error",
                    "changed": False,
                    "change_id": None,
                    "http_status": getattr(exc, "status_code", None),
                    "checked_at": checked_at,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "message": message,
                }
            logger.info(
                "check document=%s outcome=%s http=%s changed=%s duration_ms=%s",
                key,
                result["outcome"],
                result.get("http_status"),
                result.get("changed"),
                result.get("duration_ms"),
            )
            return result

    async def _check_document(
        self, key: str, *, force_full: bool = False
    ) -> dict[str, Any]:
        started = time.perf_counter()
        document = self.documents[key]

        with db.read(self.settings) as conn:
            state = db.get_document(conn, key)
            latest = db.get_latest_version(conn, key)
        if state is None:
            raise RuntimeError(f"Document {key} is not initialised in the database")

        needs_full = self._needs_full_download(state, force_full=force_full)

        try:
            fetched = await fetch_pdf(
                document.url,
                settings=self.settings,
                etag=state.get("etag"),
                last_modified=state.get("last_modified"),
                conditional=not needs_full,
            )
        except FetchError as exc:
            checked_at = to_iso()
            message = str(exc)
            duration_ms = int((time.perf_counter() - started) * 1000)
            with db.transaction(self.settings) as conn:
                db.update_document_on_error(
                    conn,
                    key=key,
                    checked_at=checked_at,
                    http_status=exc.status_code,
                    error=message,
                )
                db.insert_check(
                    conn,
                    document_key=key,
                    checked_at=checked_at,
                    outcome="error",
                    http_status=exc.status_code,
                    duration_ms=duration_ms,
                    detail=message,
                )
            return {
                "key": key,
                "outcome": "error",
                "changed": False,
                "change_id": None,
                "http_status": exc.status_code,
                "checked_at": checked_at,
                "duration_ms": duration_ms,
                "message": message,
            }

        if fetched.not_modified:
            checked_at = to_iso()
            duration_ms = int((time.perf_counter() - started) * 1000)
            with db.transaction(self.settings) as conn:
                db.update_document_on_not_modified(
                    conn,
                    key=key,
                    checked_at=checked_at,
                    http_status=fetched.status_code,
                    etag=fetched.etag,
                    last_modified=fetched.last_modified,
                )
                db.insert_check(
                    conn,
                    document_key=key,
                    checked_at=checked_at,
                    outcome="not_modified",
                    http_status=fetched.status_code,
                    duration_ms=duration_ms,
                    detail="Origin returned 304 Not Modified",
                )
            return {
                "key": key,
                "outcome": "not_modified",
                "changed": False,
                "change_id": None,
                "http_status": fetched.status_code,
                "checked_at": checked_at,
                "duration_ms": duration_ms,
                "message": "304 Not Modified",
            }

        if fetched.body is None:
            raise RuntimeError("Fetch succeeded without a body or a 304 status")

        sha256 = _sha256_hex(fetched.body)
        checked_at = to_iso()

        if latest is not None and latest["sha256"] == sha256:
            duration_ms = int((time.perf_counter() - started) * 1000)
            with db.transaction(self.settings) as conn:
                db.update_document_on_download(
                    conn,
                    key=key,
                    checked_at=checked_at,
                    http_status=fetched.status_code,
                    etag=fetched.etag,
                    last_modified=fetched.last_modified,
                    current_version_id=latest["id"],
                )
                db.insert_check(
                    conn,
                    document_key=key,
                    checked_at=checked_at,
                    outcome="unchanged",
                    http_status=fetched.status_code,
                    duration_ms=duration_ms,
                    detail=f"Same bytes as version {latest['id']}",
                )
            return {
                "key": key,
                "outcome": "unchanged",
                "changed": False,
                "change_id": None,
                "http_status": fetched.status_code,
                "checked_at": checked_at,
                "duration_ms": duration_ms,
                "message": f"Same bytes as version {latest['id']}",
            }

        snapshot = self._extract_with_fallback(key, sha256, fetched.body, latest)
        pdf_rel, snapshot_rel = self._store_snapshot(
            key, sha256, fetched.body, snapshot, fetched, document
        )

        content_changed = latest is None or latest["content_hash"] != snapshot["content_hash"]
        warnings = list(snapshot.get("warnings", []))
        duration_ms = int((time.perf_counter() - started) * 1000)

        with db.transaction(self.settings) as conn:
            version_id = db.insert_version(
                conn,
                document_key=key,
                fetched_at=checked_at,
                sha256=sha256,
                content_hash=snapshot["content_hash"],
                page_count=int(snapshot.get("page_count", 0)),
                size_bytes=len(fetched.body),
                pdf_path=pdf_rel,
                snapshot_path=snapshot_rel,
                etag=fetched.etag,
                last_modified=fetched.last_modified,
                content_changed=bool(content_changed and latest is not None),
                previous_version_id=latest["id"] if latest else None,
                text_extractable=bool(snapshot.get("text_extractable", False)),
                warnings=warnings,
            )

            change_id: int | None = None
            change_headline: str | None = None
            if latest is None:
                outcome = "initialized"
                detail = f"Initial baseline version {version_id}"
            elif content_changed:
                previous_snapshot = self._load_snapshot(latest)
                diff = self._build_change_diff(
                    key=key,
                    previous=latest,
                    previous_snapshot=previous_snapshot,
                    current=snapshot,
                    detected_at=checked_at,
                    from_version_id=latest["id"],
                    to_version_id=version_id,
                    warnings=warnings,
                )
                change_id = db.insert_change(
                    conn,
                    document_key=key,
                    from_version_id=latest["id"],
                    to_version_id=version_id,
                    detected_at=checked_at,
                    headline=diff["headline"],
                    summary=diff["summary"],
                    diff=diff,
                    unified_diff=diff["unified_diff"],
                    markdown=diff["markdown"],
                )
                change_headline = diff["headline"]
                outcome = "content_changed"
                detail = f"change_id={change_id}; {diff['headline']}"
            else:
                outcome = "metadata_only"
                detail = (
                    f"PDF bytes changed but extracted content is identical to "
                    f"version {latest['id']}"
                )

            db.update_document_on_download(
                conn,
                key=key,
                checked_at=checked_at,
                http_status=fetched.status_code,
                etag=fetched.etag,
                last_modified=fetched.last_modified,
                current_version_id=version_id,
                file_changed_at=checked_at,
                content_changed_at=checked_at if content_changed and latest else None,
            )
            db.insert_check(
                conn,
                document_key=key,
                checked_at=checked_at,
                outcome=outcome,
                http_status=fetched.status_code,
                duration_ms=duration_ms,
                detail=detail,
            )

        if content_changed and latest is not None:
            logger.warning(
                "CONTENT CHANGED document=%s from_version=%s to_version=%s change_id=%s headline=%s",
                key,
                latest["id"],
                version_id,
                change_id,
                change_headline,
            )

        return {
            "key": key,
            "outcome": outcome,
            "changed": bool(content_changed and latest is not None),
            "change_id": change_id,
            "http_status": fetched.status_code,
            "checked_at": checked_at,
            "duration_ms": duration_ms,
            "message": detail,
            "version_id": version_id,
        }

    def _needs_full_download(self, state: dict[str, Any], *, force_full: bool) -> bool:
        if force_full or self.settings.full_check_interval_hours == 0:
            return True
        last_full = state.get("last_full_check_at")
        if not last_full:
            return True
        try:
            last_full_at = parse_iso(last_full)
        except ValueError:
            return True
        return utc_now() - last_full_at >= timedelta(
            hours=self.settings.full_check_interval_hours
        )

    def _extract_with_fallback(
        self,
        key: str,
        sha256: str,
        body: bytes,
        latest: dict[str, Any] | None,
    ) -> dict[str, Any]:
        previous_text_extractable = (
            bool(latest["text_extractable"]) if latest is not None else True
        )

        pdf_path = self.settings.snapshots_dir / key / f"{sha256}.pdf"
        try:
            _atomic_write(pdf_path, body)
            snapshot = extract_pdf(pdf_path)
        except PdfExtractionError as exc:
            logger.error("PDF extraction failed for %s: %s", key, exc)
            snapshot = {
                "page_count": 0,
                "pages": [],
                "metadata": {},
                "warnings": [f"PDF extraction failed: {exc}"],
                "text_extractable": False,
                "total_chars": 0,
                "content_hash": "raw:" + sha256,
                "extraction_failed": True,
            }
        except Exception as exc:
            logger.exception("PDF extraction crashed for %s", key)
            snapshot = {
                "page_count": 0,
                "pages": [],
                "metadata": {},
                "warnings": [f"PDF extraction crashed: {type(exc).__name__}: {exc}"],
                "text_extractable": False,
                "total_chars": 0,
                "content_hash": "raw:" + sha256,
                "extraction_failed": True,
            }

        if not snapshot.get("text_extractable", False):
            # Without a text layer, the only honest fingerprint is the raw bytes.
            snapshot["content_hash"] = "raw:" + sha256
            if previous_text_extractable and not snapshot.get("extraction_failed"):
                snapshot.setdefault("warnings", []).append(
                    "The PDF no longer has an extractable text layer; "
                    "content-level diffing is unavailable for this version."
                )
        return snapshot

    def _store_snapshot(
        self,
        key: str,
        sha256: str,
        body: bytes,
        snapshot: dict[str, Any],
        fetched: Any,
        document: DocumentConfig,
    ) -> tuple[str, str]:
        pdf_path = self.settings.snapshots_dir / key / f"{sha256}.pdf"
        if not pdf_path.exists() or pdf_path.stat().st_size != len(body):
            _atomic_write(pdf_path, body)

        snapshot_path = self.settings.snapshots_dir / key / f"{sha256}.json"
        payload = {
            **snapshot,
            "document_key": key,
            "sha256": sha256,
            "source_url": document.url,
            "final_url": fetched.final_url,
            "fetched_at": to_iso(),
            "http_etag": fetched.etag,
            "http_last_modified": fetched.last_modified,
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(snapshot_path, encoded)

        return (
            pdf_path.relative_to(self.settings.data_dir).as_posix(),
            snapshot_path.relative_to(self.settings.data_dir).as_posix(),
        )

    def _load_snapshot(self, version: dict[str, Any]) -> dict[str, Any] | None:
        path = self.settings.data_dir / version["snapshot_path"]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Cannot load snapshot for version %s (%s): %s",
                version["id"],
                path,
                exc,
            )
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
            logger.warning("Snapshot for version %s has an invalid shape", version["id"])
            return None
        return payload

    def _build_change_diff(
        self,
        *,
        key: str,
        previous: dict[str, Any],
        previous_snapshot: dict[str, Any] | None,
        current: dict[str, Any],
        detected_at: str,
        from_version_id: int,
        to_version_id: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        if not current.get("text_extractable", False):
            return self._unavailable_diff(
                key=key,
                detected_at=detected_at,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
                reason=(
                    "当前版本没有可提取的文本层（可能是扫描件或文字被转成曲线）。"
                    "系统仍能根据 SHA-256 发现文件变化，但无法定位变化内容。"
                ),
                warnings=warnings,
            )
        if not previous.get("text_extractable", True):
            return self._unavailable_diff(
                key=key,
                detected_at=detected_at,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
                reason=(
                    "上一版本没有可提取的文本层，无法做内容级比较。"
                    "当前版本已恢复文本层，后续变化可以正常 diff。"
                ),
                warnings=warnings,
            )
        if previous_snapshot is None:
            return self._unavailable_diff(
                key=key,
                detected_at=detected_at,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
                reason="上一版本的抽取快照不可用或已损坏，无法做内容级比较。",
                warnings=warnings,
            )
        try:
            return diff_snapshots(
                previous_snapshot,
                current,
                document_key=key,
                detected_at=detected_at,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
                max_diff_chars=self.settings.max_diff_chars,
            )
        except Exception as exc:
            logger.exception("Diff failed for %s", key)
            warnings.append(f"Diff failed: {type(exc).__name__}: {exc}")
            return self._unavailable_diff(
                key=key,
                detected_at=detected_at,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
                reason=f"差异计算失败：{type(exc).__name__}: {exc}",
                warnings=warnings,
            )

    def _unavailable_diff(
        self,
        *,
        key: str,
        detected_at: str,
        from_version_id: int,
        to_version_id: int,
        reason: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        warning_markdown = "\n".join(f"- {warning}" for warning in warnings if warning)
        markdown = (
            "# PDF 文件变更报告\n\n"
            f"- 文档：`{key}`\n"
            f"- 检测时间：{detected_at}\n"
            f"- 版本：{from_version_id} → {to_version_id}\n"
            "- 摘要：PDF 文件发生变化，但当前无法进行内容级比较\n\n"
            "## 原因\n\n"
            f"{reason}\n"
        )
        if warning_markdown:
            markdown += "\n## 提取/比较警告\n\n" + warning_markdown + "\n"
        return {
            "document_key": key,
            "detected_at": detected_at,
            "from_version_id": from_version_id,
            "to_version_id": to_version_id,
            "headline": "PDF 文件发生变化，但当前无法进行内容级比较",
            "summary": dict(EMPTY_SUMMARY),
            "pages": [],
            "unified_diff": "",
            "markdown": markdown,
            "truncated": False,
            "unavailable": True,
            "reason": reason,
        }
