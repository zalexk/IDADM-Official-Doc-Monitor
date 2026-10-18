"""FastAPI application and public monitoring endpoints."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.responses import PlainTextResponse
from fastapi.security import APIKeyHeader

from . import db, presenters
from .config import APP_VERSION, Settings, load_documents
from .monitor import MonitorService
from .scheduler import MonitorScheduler
from .schemas import (
    ChangeDetail,
    ChangeList,
    CheckAllResponse,
    CheckResult,
    DocumentList,
    DocumentStatus,
    HealthInfo,
    VersionList,
)
from .timeutil import to_iso

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    documents = load_documents(settings.documents_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_directories()
        db.init_db(settings)
        db.seed_documents(settings, documents)
        service = MonitorService(settings, documents)
        app.state.service = service
        scheduler: MonitorScheduler | None = None
        if settings.scheduler_enabled:
            scheduler = MonitorScheduler(settings, service)
            scheduler.start()
        app.state.scheduler = scheduler
        logger.info(
            "monitor started documents=%s scheduler=%s data_dir=%s",
            len(documents),
            settings.scheduler_enabled,
            settings.data_dir,
        )
        if not settings.api_key:
            logger.warning(
                "API_KEY is not set; /api/v1 is unprotected. "
                "Set API_KEY before exposing this service."
            )
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()

    app = FastAPI(
        title="CUHK CDMP PDF Monitor",
        description=(
            "Detects changes to monitored PDFs at three levels: HTTP validators, "
            "SHA-256 of the file, and a content fingerprint built from extracted "
            "text and table cells. Change reports include page-level text diffs "
            "and table cell diffs.\n\n"
            "All timestamps are UTC. When API_KEY is configured, send it as "
            "`X-API-Key` or `Authorization: Bearer <key>`."
        ),
        version=APP_VERSION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "system", "description": "Health and service metadata"},
            {"name": "documents", "description": "Monitored document status and history"},
            {"name": "actions", "description": "Trigger an immediate check"},
        ],
    )

    async def require_api_key(
        request: Request,
        header_key: str | None = Security(api_key_header),
    ) -> None:
        if not settings.api_key:
            return
        provided = header_key
        if not provided:
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                provided = authorization.split(" ", 1)[1].strip()
        if not provided or not secrets.compare_digest(provided, settings.api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

    def service_from(request: Request) -> MonitorService:
        service = getattr(request.app.state, "service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Monitor service is not ready")
        return service

    @app.middleware("http")
    async def no_cache_api(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path.startswith("/api/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": "CUHK CDMP PDF Monitor",
            "version": APP_VERSION,
            "docs": "/docs",
            "health": "/healthz",
            "api": "/api/v1/documents",
        }

    @app.get("/healthz", response_model=HealthInfo, tags=["system"])
    def health(request: Request) -> dict[str, Any]:
        try:
            with db.read(settings) as conn:
                conn.execute("SELECT 1").fetchone()
                document_count = len(db.list_documents(conn))
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"Database check failed: {exc}"
            ) from exc
        scheduler: MonitorScheduler | None = getattr(request.app.state, "scheduler", None)
        return {
            "status": "ok",
            "version": APP_VERSION,
            "time": to_iso(),
            "database": "ok",
            "scheduler": {
                "enabled": settings.scheduler_enabled,
                "running": bool(scheduler and scheduler.running),
                "next_run_at": scheduler.next_run_at if scheduler else None,
            },
            "documents": document_count,
        }

    @router.get("/documents", response_model=DocumentList, tags=["documents"])
    def list_documents() -> dict[str, Any]:
        with db.read(settings) as conn:
            return {"documents": presenters.build_document_list(conn, settings)}

    @router.get(
        "/documents/{key}", response_model=DocumentStatus, tags=["documents"]
    )
    def get_document(key: str) -> dict[str, Any]:
        with db.read(settings) as conn:
            status_payload = presenters.build_document_status(conn, settings, key)
        if status_payload is None:
            raise HTTPException(status_code=404, detail=f"Unknown document: {key}")
        return status_payload

    @router.get(
        "/documents/{key}/versions", response_model=VersionList, tags=["documents"]
    )
    def list_versions(
        key: str,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with db.read(settings) as conn:
            if db.get_document(conn, key) is None:
                raise HTTPException(status_code=404, detail=f"Unknown document: {key}")
            versions = presenters.build_version_list(conn, key, limit, offset)
        return {"document_key": key, "versions": versions}

    @router.get(
        "/documents/{key}/changes", response_model=ChangeList, tags=["documents"]
    )
    def list_changes(
        key: str,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with db.read(settings) as conn:
            if db.get_document(conn, key) is None:
                raise HTTPException(status_code=404, detail=f"Unknown document: {key}")
            changes = presenters.build_change_list(conn, key, limit, offset)
        return {"document_key": key, "changes": changes}

    @router.get(
        "/documents/{key}/changes/latest",
        response_model=ChangeDetail,
        tags=["documents"],
    )
    def latest_change(key: str) -> dict[str, Any]:
        with db.read(settings) as conn:
            if db.get_document(conn, key) is None:
                raise HTTPException(status_code=404, detail=f"Unknown document: {key}")
            change = db.get_latest_change(conn, key)
            if change is None:
                raise HTTPException(
                    status_code=404, detail=f"No content change recorded for {key}"
                )
            detail = presenters.build_change_detail(conn, key, change["id"])
        if detail is None:
            raise HTTPException(status_code=404, detail="Change report not found")
        return detail

    @router.get(
        "/documents/{key}/changes/{change_id}",
        response_model=ChangeDetail,
        tags=["documents"],
    )
    def get_change(key: str, change_id: int) -> dict[str, Any]:
        with db.read(settings) as conn:
            if db.get_document(conn, key) is None:
                raise HTTPException(status_code=404, detail=f"Unknown document: {key}")
            detail = presenters.build_change_detail(conn, key, change_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Change report not found")
        return detail

    @router.get(
        "/documents/{key}/changes/{change_id}/markdown",
        response_class=PlainTextResponse,
        tags=["documents"],
    )
    def get_change_markdown(key: str, change_id: int) -> PlainTextResponse:
        with db.read(settings) as conn:
            detail = presenters.build_change_detail(conn, key, change_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Change report not found")
        return PlainTextResponse(detail["markdown"], media_type="text/markdown; charset=utf-8")

    @router.get(
        "/documents/{key}/changes/{change_id}/diff",
        response_class=PlainTextResponse,
        tags=["documents"],
    )
    def get_change_diff(key: str, change_id: int) -> PlainTextResponse:
        with db.read(settings) as conn:
            detail = presenters.build_change_detail(conn, key, change_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Change report not found")
        return PlainTextResponse(
            detail["unified_diff"], media_type="text/plain; charset=utf-8"
        )

    @router.post(
        "/documents/{key}/check", response_model=CheckResult, tags=["actions"]
    )
    async def check_document(
        key: str,
        request: Request,
        full: bool = Query(False, description="Ignore ETag/Last-Modified and download the full file"),
    ) -> dict[str, Any]:
        service = service_from(request)
        if key not in service.documents:
            raise HTTPException(status_code=404, detail=f"Unknown document: {key}")
        return await service.check_document(key, force_full=full)

    @router.post("/check", response_model=CheckAllResponse, tags=["actions"])
    async def check_all(
        request: Request,
        full: bool = Query(False, description="Ignore ETag/Last-Modified for every document"),
    ) -> dict[str, Any]:
        service = service_from(request)
        return {"results": await service.check_all(force_full=full)}

    app.include_router(router)
    return app
