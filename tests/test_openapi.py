"""OpenAPI / Swagger 仅暴露 4 个产品 API。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from research_automation.api.openapi_meta import PUBLIC_API_PATHS
from research_automation.main import app


def test_openapi_json_exactly_four_paths():
    client = TestClient(app)
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert set(paths.keys()) == set(PUBLIC_API_PATHS)
    assert len(paths) == 4


def test_openapi_tags_only_product_groups():
    tags = {t["name"] for t in app.openapi()["tags"]}
    assert tags == {"morning-brief", "sector-reports"}


def test_swagger_ui_and_redoc():
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_root_still_works_but_hidden_from_schema():
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert "/" not in app.openapi()["paths"]


def test_list_sectors_endpoint():
    client = TestClient(app)
    r = client.get("/api/v1/sector-reports/sectors")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
