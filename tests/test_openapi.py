"""OpenAPI / Swagger 文档可用性烟测。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from research_automation.main import app


def test_openapi_json_lists_v1_paths():
    client = TestClient(app)
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/api/v1/companies/{ticker}/financials" in paths
    assert "/api/v1/companies/{ticker}/business-profile" in paths
    assert "/api/v1/companies/{ticker}/earnings" in paths
    assert "/api/v1/news/morning-brief" in paths
    assert "/api/v1/search" in paths


def test_swagger_ui_and_redoc():
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_root_links_docs():
    client = TestClient(app)
    body = client.get("/").json()
    assert body["docs"] == "/docs"
    assert body["openapi"] == "/openapi.json"
