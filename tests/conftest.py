from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


class MockOrigin:
    """A tiny HTTP server that supports ETag based conditional GET."""

    def __init__(self, pdf_path: Path):
        self.payload = pdf_path.read_bytes()
        self.etag = self._etag(self.payload)
        self.last_modified = self._http_date(0)
        self.requests: list[dict[str, str | None]] = []
        self._revision = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _etag(payload: bytes) -> str:
        return '"' + hashlib.sha256(payload).hexdigest()[:24] + '"'

    def _http_date(self, offset: int) -> str:
        return format_datetime(
            datetime.now(timezone.utc) + timedelta(seconds=offset), usegmt=True
        )

    def update_file(self, pdf_path: Path) -> None:
        self.payload = pdf_path.read_bytes()
        self.etag = self._etag(self.payload)
        self._revision += 1
        self.last_modified = self._http_date(self._revision)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/doc.pdf"

    def _handler(self):
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                origin.requests.append(
                    {
                        "path": self.path,
                        "if_none_match": self.headers.get("If-None-Match"),
                        "if_modified_since": self.headers.get("If-Modified-Since"),
                    }
                )
                if self.path != "/doc.pdf":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == origin.etag:
                    self.send_response(304)
                    self.send_header("ETag", origin.etag)
                    self.send_header("Last-Modified", origin.last_modified)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(origin.payload)))
                self.send_header("ETag", origin.etag)
                self.send_header("Last-Modified", origin.last_modified)
                self.end_headers()
                self.wfile.write(origin.payload)

            def log_message(self, format, *args):  # noqa: A002
                return

        return Handler

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def origin(tmp_path: Path):
    from tests.pdf_factory import make_test_pdf

    source = tmp_path / "origin.pdf"
    make_test_pdf(source)
    server = MockOrigin(source)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def settings(tmp_path: Path, origin: MockOrigin) -> Settings:
    documents_config = tmp_path / "documents.yaml"
    documents_config.write_text(
        yaml.safe_dump(
            {
                "documents": [
                    {
                        "key": "test-doc",
                        "title": "Test Course List",
                        "url": origin.url,
                        "tags": ["test"],
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir,
        documents_config=documents_config,
        database_path=data_dir / "monitor.db",
        scheduler_enabled=False,
        check_on_start=False,
        check_interval_minutes=30,
        full_check_interval_hours=24,
        http_timeout_seconds=5.0,
        max_download_bytes=5 * 1024 * 1024,
        api_key=None,
        user_agent="pytest-cdmp-monitor",
        log_level="WARNING",
        max_diff_chars=250_000,
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def changed_pdf(tmp_path: Path) -> Path:
    from tests.pdf_factory import make_test_pdf

    path = tmp_path / "changed.pdf"
    make_test_pdf(path, code="MATH3271", note="Version two")
    return path


@pytest.fixture
def metadata_only_pdf(tmp_path: Path, changed_pdf: Path) -> Path:
    from tests.pdf_factory import make_metadata_variant

    path = tmp_path / "metadata-only.pdf"
    make_metadata_variant(changed_pdf, path)
    return path


@pytest.fixture
def original_again_pdf(tmp_path: Path) -> Path:
    from tests.pdf_factory import make_test_pdf

    path = tmp_path / "original-again.pdf"
    make_test_pdf(path)
    return path
