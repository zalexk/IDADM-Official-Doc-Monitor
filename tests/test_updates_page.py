from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings

UPDATES_HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "updates.html"


def _check(client: TestClient, *, full: bool = False) -> dict:
    params = {"full": "true"} if full else None
    response = client.post("/api/v1/documents/test-doc/check", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_updates_page_script_has_valid_syntax(tmp_path: Path) -> None:
    html = UPDATES_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match, "updates.html must contain a script block"
    script_path = tmp_path / "updates-page.js"
    script_path.write_text(match.group(1), encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(script_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"JS syntax error:\n{result.stderr}"


def test_updates_page_served(client: TestClient) -> None:
    response = client.get("/updates")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "官方文档更新监控" in response.text
    # Trilingual i18n assets: dictionaries, switcher buttons and static hooks.
    for marker in ('data-lang="zh-CN"', 'data-lang="zh-TW"', 'data-lang="en"',
                   "data-i18n", "navigator.languages"):
        assert marker in response.text, marker


def test_updates_page_end_to_end(
    client: TestClient,
    origin,
    changed_pdf: Path,
) -> None:
    _check(client)

    data = client.get("/updates/data.json").json()
    assert data["generated_at"]
    assert data["scheduler"]["enabled"] is False
    assert data["scheduler"]["next_run_at"] is None
    assert data["scheduler"]["interval_minutes"] == 30
    assert data["recent_changes"] == []

    (document,) = data["documents"]
    assert document["key"] == "test-doc"
    assert document["title"] == "Test Course List"
    assert document["latest_change"] is None
    assert document["current_version"]["page_count"] == 1
    assert [check["outcome"] for check in document["checks"]] == ["initialized"]

    origin.update_file(changed_pdf)
    changed = _check(client)
    assert changed["outcome"] == "content_changed"

    data = client.get("/updates/data.json").json()
    (document,) = data["documents"]
    # Page bar is oldest -> newest.
    assert [check["outcome"] for check in document["checks"]] == [
        "initialized",
        "content_changed",
    ]
    assert document["latest_change"]["id"] == 1
    assert document["content_changed_at"] is not None

    (recent,) = data["recent_changes"]
    assert recent["id"] == 1
    assert recent["document_key"] == "test-doc"
    assert recent["document_title"] == "Test Course List"
    assert recent["headline"]

    detail = client.get("/updates/changes/1")
    assert detail.status_code == 200
    detail_json = detail.json()
    assert detail_json["document_title"] == "Test Course List"
    assert detail_json["markdown_url"] == "/updates/changes/1/markdown"
    assert "pages" in detail_json["diff"]
    assert "MATH3270" in detail_json["unified_diff"]

    markdown = client.get("/updates/changes/1/markdown")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "MATH3271" in markdown.text

    assert client.get("/updates/changes/999").status_code == 404
    assert client.get("/updates/changes/999/markdown").status_code == 404


def test_updates_page_is_public_even_with_api_key(
    settings: Settings, origin
) -> None:
    secured = replace(settings, api_key="s3cret")
    app = create_app(secured)
    with TestClient(app) as client:
        assert client.get("/updates").status_code == 200
        data_response = client.get("/updates/data.json")
        assert data_response.status_code == 200
        assert data_response.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/documents").status_code == 401
        assert client.get("/api/v1/documents/test-doc/checks").status_code == 401


def test_updates_endpoints_are_readonly(client: TestClient) -> None:
    _check(client)
    before = client.get("/api/v1/documents/test-doc/checks").json()["checks"]
    assert len(before) == 1

    client.get("/updates/data.json")
    client.get("/updates/changes/1")
    client.get("/updates/changes/1/markdown")

    after = client.get("/api/v1/documents/test-doc/checks").json()["checks"]
    assert len(after) == len(before)


def test_document_checks_endpoint(client: TestClient, origin) -> None:
    _check(client)
    _check(client)  # ETag matches -> not_modified

    payload = client.get("/api/v1/documents/test-doc/checks").json()
    assert payload["document_key"] == "test-doc"
    # Newest first.
    assert [check["outcome"] for check in payload["checks"]] == [
        "not_modified",
        "initialized",
    ]
    assert payload["checks"][0]["http_status"] == 304

    paged = client.get(
        "/api/v1/documents/test-doc/checks", params={"limit": 1, "offset": 1}
    ).json()
    assert [check["outcome"] for check in paged["checks"]] == ["initialized"]

    assert client.get("/api/v1/documents/missing/checks").status_code == 404
