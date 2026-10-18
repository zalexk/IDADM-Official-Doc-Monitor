"""Pydantic response models for the public API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VersionInfo(BaseModel):
    id: int
    fetched_at: str
    sha256: str
    content_hash: str
    page_count: int
    size_bytes: int
    content_changed: bool
    text_extractable: bool
    previous_version_id: int | None = None
    warnings: list[str] = Field(default_factory=list)


class SourceInfo(BaseModel):
    etag: str | None = None
    last_modified: str | None = None
    last_full_check_at: str | None = None


class MonitorInfo(BaseModel):
    scheduler_enabled: bool
    check_interval_minutes: int
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_http_status: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class ChangeSummary(BaseModel):
    id: int
    document_key: str
    detected_at: str
    headline: str
    summary: dict[str, int]
    from_version_id: int | None = None
    to_version_id: int
    detail_url: str


class CheckInfo(BaseModel):
    checked_at: str
    outcome: str
    http_status: int | None = None
    duration_ms: int | None = None
    detail: str | None = None


class DocumentStatus(BaseModel):
    key: str
    title: str
    url: str
    tags: list[str] = Field(default_factory=list)
    source: SourceInfo
    monitor: MonitorInfo
    content: VersionInfo | None = None
    file_changed_at: str | None = None
    content_changed_at: str | None = None
    latest_change: ChangeSummary | None = None
    last_check: CheckInfo | None = None


class DocumentList(BaseModel):
    documents: list[DocumentStatus]


class VersionList(BaseModel):
    document_key: str
    versions: list[VersionInfo]


class ChangeList(BaseModel):
    document_key: str
    changes: list[ChangeSummary]


class ChangeDetail(BaseModel):
    id: int
    document_key: str
    detected_at: str
    headline: str
    summary: dict[str, int]
    from_version: VersionInfo | None = None
    to_version: VersionInfo
    diff: dict[str, Any]
    unified_diff: str
    markdown: str
    truncated: bool = False


class CheckResult(BaseModel):
    key: str
    outcome: str
    changed: bool
    change_id: int | None = None
    http_status: int | None = None
    checked_at: str
    duration_ms: int
    message: str | None = None
    version_id: int | None = None


class CheckAllResponse(BaseModel):
    results: list[CheckResult]


class SchedulerInfo(BaseModel):
    enabled: bool
    running: bool
    next_run_at: str | None = None


class HealthInfo(BaseModel):
    status: str
    version: str
    time: str
    database: str
    scheduler: SchedulerInfo
    documents: int
