"""One app, two services: every URL, the shared pieces, and each service's own error format."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.cheque.core.config import get_settings as cheque_settings
from app.factory import build_app
from app.leasing.core.config import settings as leasing_settings

LEASING_PATHS = [
    "/api/v1/ocr",
    "/api/v1/ocr/leasing",
    "/api/v1/ocr/leasing/upload",
    "/api/v1/ocr/leasing/jobs",
    "/api/v1/ocr/leasing/jobs/upload",
    "/api/v1/ocr/leasing/jobs/{job_id}",
    "/api/v1/ocr/leasing/detect",
    "/api/v1/ocr/leasing/detect/upload",
    "/api/v1/ocr/leasing/detect/jobs",
    "/api/v1/ocr/leasing/detect/jobs/upload",
]
CHEQUE_PATHS = ["/api/v1/cheques/extract", "/api/v1/cheques/match", "/api/v1/cheques/match/upload"]


@pytest.fixture(scope="module")
def app():
    return build_app(["leasing", "cheque"])


@pytest.fixture
def client(app, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(leasing_settings, "require_https", False)
    monkeypatch.setattr(leasing_settings, "api_keys", [])
    monkeypatch.setattr(cheque_settings(), "api_keys", [])
    return TestClient(app)


def test_every_endpoint_of_both_services_is_in_one_app(app) -> None:
    paths = app.openapi()["paths"]
    for path in LEASING_PATHS + CHEQUE_PATHS + ["/health", "/api/v1/health"]:
        assert path in paths, path


def test_direct_upload_endpoints_exist_for_every_kind_of_request(app) -> None:
    paths = app.openapi()["paths"]
    for path in ("/api/v1/ocr", "/api/v1/ocr/leasing/upload", "/api/v1/ocr/leasing/jobs/upload",
                 "/api/v1/ocr/leasing/detect/upload", "/api/v1/ocr/leasing/detect/jobs/upload",
                 "/api/v1/cheques/extract", "/api/v1/cheques/match/upload"):
        content = paths[path]["post"]["requestBody"]["content"]
        assert "multipart/form-data" in content, path


def _file_fields(node) -> list[dict]:
    """Every schema node that describes an uploaded file, wherever it is nested."""
    found = []
    if isinstance(node, dict):
        if "contentMediaType" in node:
            found.append(node)
        for value in node.values():
            found += _file_fields(value)
    elif isinstance(node, list):
        for value in node:
            found += _file_fields(value)
    return found


def test_swagger_shows_a_file_picker_for_every_upload_field(app) -> None:
    fields = _file_fields(app.openapi()["components"]["schemas"])
    # Leasing: /ocr, /leasing/upload, /leasing/jobs/upload, /detect/upload, /detect/jobs/upload;
    # Cheque: /extract, /match/upload
    assert len(fields) >= 7
    assert all(field.get("format") == "binary" for field in fields)


def test_both_services_share_one_api_key_scheme(app) -> None:
    schemes = app.openapi()["components"]["securitySchemes"]
    assert len(schemes) == 1
    [scheme] = schemes.values()
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-API-Key",
                      "description": "API key issued for this service"}


def test_health_is_shared_public_and_keeps_both_old_shapes(client, monkeypatch) -> None:
    monkeypatch.setattr(leasing_settings, "api_keys", [SecretStr("k")])
    assert client.get("/health").json() == {"status": "ok"}  # Leasing's probe, unchanged
    detail = client.get("/api/v1/health").json()  # Cheque's probe, unchanged, plus what runs here
    assert detail["status"] == "ok" and detail["version"] and detail["services"] == ["leasing", "cheque"]


def test_each_service_answers_errors_in_its_own_format(client, monkeypatch) -> None:
    monkeypatch.setattr(leasing_settings, "api_keys", [SecretStr("shared")])
    monkeypatch.setattr(cheque_settings(), "api_keys", [SecretStr("shared")])

    cheque = client.post("/api/v1/cheques/extract")
    assert cheque.status_code == 401 and cheque.json() == {"error": "Invalid or missing API key."}

    leasing = client.post("/api/v1/ocr/leasing", json={})
    assert leasing.status_code == 401 and set(leasing.json()) == {"detail"}

    ocr = client.post("/api/v1/ocr")
    assert ocr.status_code == 401 and ocr.json()["success"] is False and "errorInfo" in ocr.json()


def test_one_key_opens_both_services(client, monkeypatch) -> None:
    monkeypatch.setattr(leasing_settings, "api_keys", [SecretStr("shared")])
    monkeypatch.setattr(cheque_settings(), "api_keys", [SecretStr("shared")])
    headers = {"X-API-Key": "shared"}
    # Past authentication an empty request fails validation, each in its own error shape.
    assert client.post("/api/v1/ocr/leasing", json={}, headers=headers).status_code == 422
    cheque = client.post("/api/v1/cheques/extract", headers=headers)
    assert cheque.status_code == 422 and "error" in cheque.json()


def test_each_service_can_have_its_own_key(client, monkeypatch) -> None:
    monkeypatch.setattr(leasing_settings, "api_keys", [SecretStr("leasing-key")])
    monkeypatch.setattr(cheque_settings(), "api_keys", [SecretStr("cheque-key")])
    assert client.post("/api/v1/ocr/leasing", json={}, headers={"X-API-Key": "cheque-key"}).status_code == 401
    assert client.post("/api/v1/cheques/extract", headers={"X-API-Key": "leasing-key"}).status_code == 401
    assert client.post("/api/v1/cheques/extract", headers={"X-API-Key": "cheque-key"}).status_code == 422


def test_unknown_urls_and_wrong_methods_get_a_readable_answer(client) -> None:
    missing = client.get("/api/v1/nothing-here")
    assert missing.status_code == 404 and "does not exist" in missing.json()["detail"]
    wrong_method = client.get("/api/v1/cheques/extract")
    assert wrong_method.status_code == 405 and "error" in wrong_method.json()


def test_cheque_requests_still_get_a_request_id(client) -> None:
    assert client.post("/api/v1/cheques/extract").headers.get("x-request-id")


def test_leasing_https_rule_does_not_touch_cheque(client, monkeypatch) -> None:
    monkeypatch.setattr(leasing_settings, "require_https", True)
    assert client.post("/api/v1/ocr/leasing", json={}).status_code == 400
    assert client.post("/api/v1/cheques/extract").status_code == 422  # reached its own validation


@pytest.mark.parametrize("service, absent, present", [
    ("leasing", "/api/v1/cheques/extract", "/api/v1/ocr/leasing/upload"),
    ("cheque", "/api/v1/ocr/leasing/upload", "/api/v1/cheques/extract"),
])
def test_a_service_can_run_alone(service, absent, present) -> None:
    single = build_app([service])
    paths = single.openapi()["paths"]
    assert present in paths and absent not in paths
    assert TestClient(single).get("/api/v1/health").json()["services"] == [service]
    assert "/health" in paths


def test_legacy_unprefixed_leasing_urls_still_point_to_the_new_ones(client) -> None:
    body = client.post("/ocr/leasing", json={}).json()
    assert "/api/v1/ocr/leasing" in body["detail"]
