from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


def _check(client: TestClient, *, full: bool = False) -> dict:
    params = {"full": "true"} if full else None
    response = client.post("/api/v1/documents/test-doc/check", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_end_to_end_change_detection_and_diff(
    client: TestClient,
    origin,
    changed_pdf: Path,
    metadata_only_pdf: Path,
    original_again_pdf: Path,
) -> None:
    initial = _check(client)
    assert initial["outcome"] == "initialized"
    assert initial["changed"] is False
    assert initial["version_id"] == 1

    initial_status = client.get("/api/v1/documents/test-doc").json()
    assert initial_status["content"]["page_count"] == 1
    assert initial_status["content"]["text_extractable"] is True
    assert initial_status["latest_change"] is None
    initial_hash = initial_status["content"]["content_hash"]

    second = _check(client)
    assert second["outcome"] == "not_modified"
    assert origin.requests[-1]["if_none_match"] == origin.etag

    origin.update_file(changed_pdf)
    changed = _check(client)
    assert changed["outcome"] == "content_changed"
    assert changed["changed"] is True
    assert changed["change_id"] == 1

    status_after_change = client.get("/api/v1/documents/test-doc").json()
    assert status_after_change["content"]["content_hash"] != initial_hash
    assert status_after_change["content_changed_at"] is not None
    assert status_after_change["latest_change"]["id"] == 1

    change_list = client.get("/api/v1/documents/test-doc/changes").json()
    assert len(change_list["changes"]) == 1
    summary = change_list["changes"][0]["summary"]
    assert summary["pages_changed"] == 1
    assert summary["table_cells_modified"] == 1

    detail_response = client.get("/api/v1/documents/test-doc/changes/1")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["to_version"]["id"] == 2
    cell_changes = [
        change
        for page in detail["diff"]["pages"]
        for change in page["table_changes"]
        if change["type"] == "cell_modified"
    ]
    assert cell_changes[0]["old"] == "MATH3270"
    assert cell_changes[0]["new"] == "MATH3271"
    assert "MATH3270" in detail["unified_diff"]
    assert "MATH3271" in detail["markdown"]

    markdown_response = client.get(
        "/api/v1/documents/test-doc/changes/1/markdown"
    )
    assert markdown_response.status_code == 200
    assert "MATH3271" in markdown_response.text
    diff_response = client.get("/api/v1/documents/test-doc/changes/1/diff")
    assert diff_response.status_code == 200
    assert "MATH3270" in diff_response.text

    # A byte-level rewrite that preserves text/tables must not create a false change.
    hash_after_change = status_after_change["content"]["content_hash"]
    origin.update_file(metadata_only_pdf)
    metadata_only = _check(client)
    assert metadata_only["outcome"] == "metadata_only"
    assert metadata_only["changed"] is False
    status_after_metadata = client.get("/api/v1/documents/test-doc").json()
    assert status_after_metadata["content"]["content_hash"] == hash_after_change
    assert status_after_metadata["content"]["id"] == 3
    assert status_after_metadata["latest_change"]["id"] == 1
    assert len(client.get("/api/v1/documents/test-doc/changes").json()["changes"]) == 1

    full = _check(client, full=True)
    assert full["outcome"] == "unchanged"
    assert origin.requests[-1]["if_none_match"] is None

    # Reverting to the original content is a real content change and a new version.
    origin.update_file(original_again_pdf)
    reverted = _check(client)
    assert reverted["outcome"] == "content_changed"
    assert reverted["changed"] is True
    assert reverted["change_id"] == 2
    status_after_revert = client.get("/api/v1/documents/test-doc").json()
    assert status_after_revert["content"]["content_hash"] == initial_hash
    assert len(client.get("/api/v1/documents/test-doc/changes").json()["changes"]) == 2
    versions = client.get("/api/v1/documents/test-doc/versions").json()["versions"]
    assert [version["id"] for version in versions] == [4, 3, 2, 1]


def test_api_key_protection(settings: Settings) -> None:
    secured = replace(settings, api_key="s3cret")
    app = create_app(secured)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/documents").status_code == 401
        assert (
            client.get(
                "/api/v1/documents", headers={"X-API-Key": "wrong"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/documents", headers={"X-API-Key": "s3cret"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/documents",
                headers={"Authorization": "Bearer s3cret"},
            ).status_code
            == 200
        )


def test_health_and_unknown_document(client: TestClient) -> None:
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["database"] == "ok"
    assert health["scheduler"]["enabled"] is False
    assert health["documents"] == 1
    assert client.get("/api/v1/documents/missing").status_code == 404
    assert client.get("/api/v1/documents/missing/changes").status_code == 404


def test_openapi_contains_monitoring_paths_and_security(
    client: TestClient,
) -> None:
    spec = client.get("/openapi.json").json()
    assert "/api/v1/documents/{key}" in spec["paths"]
    assert "/api/v1/documents/{key}/changes/latest" in spec["paths"]
    assert "/api/v1/documents/{key}/check" in spec["paths"]
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]
