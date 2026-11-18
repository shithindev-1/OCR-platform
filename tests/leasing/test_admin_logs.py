"""GET /admin/logs: read the log files remotely with an admin key; off (404) without one."""

import pytest
from fastapi.testclient import TestClient

from app.api.v1.endpoints.leasing import admin_logs
from app.core.leasing.config import settings
from app.factory import build_app

ADMIN = {"X-Admin-Key": "admin-secret"}


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_logs, "log_directory", lambda: tmp_path)
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret, second-admin")
    return tmp_path


@pytest.fixture
def client():
    return TestClient(build_app(["leasing"]))


def test_off_without_admin_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_keys", "")
    assert client.get("/admin/logs", headers=ADMIN).status_code == 404
    assert client.get("/admin/logs/files", headers=ADMIN).status_code == 404


def test_the_normal_api_key_or_no_key_is_refused(client, logs, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "caller-key")
    assert client.get("/admin/logs").status_code == 401
    assert client.get("/admin/logs", headers={"X-Admin-Key": "caller-key"}).status_code == 401
    assert client.get("/admin/logs", headers={"X-API-Key": "caller-key"}).status_code == 401
    assert client.get("/admin/logs", headers={"X-Admin-Key": "second-admin"}).status_code == 200


def test_the_last_lines_and_the_offset(client, logs):
    (logs / "errors.log").write_text("".join(f"line {n}\n" for n in range(1, 11)), encoding="utf-8")
    answer = client.get("/admin/logs", params={"file": "errors", "lines": 3}, headers=ADMIN).json()
    assert answer["lines"] == ["line 8", "line 9", "line 10"]
    assert answer["offset"] == (logs / "errors.log").stat().st_size and answer["exists"]


def test_following_returns_only_new_whole_lines(client, logs):
    path = logs / "ocr-api.log"
    path.write_text("old 1\nold 2\n", encoding="utf-8")
    offset = client.get("/admin/logs", params={"file": "ocr-api"}, headers=ADMIN).json()["offset"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("new 1\nnew 2\nhalf a li")
    answer = client.get("/admin/logs", params={"file": "ocr-api", "after": offset}, headers=ADMIN).json()
    assert answer["lines"] == ["new 1", "new 2"]  # the half-written line waits for the next call
    with path.open("a", encoding="utf-8") as handle:
        handle.write("ne\n")
    answer = client.get("/admin/logs", params={"file": "ocr-api", "after": answer["offset"]}, headers=ADMIN).json()
    assert answer["lines"] == ["half a line"]


def test_a_rotated_file_starts_again_from_the_top(client, logs):
    (logs / "errors.log").write_text("today 1\n", encoding="utf-8")
    answer = client.get("/admin/logs", params={"file": "errors", "after": 10_000}, headers=ADMIN).json()
    assert answer["restarted"] and answer["lines"] == ["today 1"]


def test_a_past_day_and_the_file_list(client, logs):
    (logs / "errors.log").write_text("today\n", encoding="utf-8")
    (logs / "errors.log.2026-11-11").write_text("yesterday\n", encoding="utf-8")
    answer = client.get("/admin/logs", params={"file": "errors", "date": "2026-11-11"}, headers=ADMIN).json()
    assert answer["lines"] == ["yesterday"]
    names = [entry["name"] for entry in client.get("/admin/logs/files", headers=ADMIN).json()["files"]]
    assert names == ["errors.log", "errors.log.2026-11-11"]
    assert client.get("/admin/logs", params={"date": "11-11-2026"}, headers=ADMIN).status_code == 400


@pytest.mark.parametrize("name", ["results", "../.env", "errors.log", "C:/Windows/win.ini", ""])
def test_only_named_logs_never_results_or_paths(client, logs, name):
    (logs / "results.log").write_text("ID data\n", encoding="utf-8")
    response = client.get("/admin/logs", params={"file": name}, headers=ADMIN)
    assert response.status_code == 400 and "ID data" not in response.text


def test_a_missing_log_is_reported_not_an_error(client, logs):
    answer = client.get("/admin/logs", params={"file": "timing"}, headers=ADMIN).json()
    assert answer == {"file": "timing", "date": None, "exists": False, "size": 0, "offset": 0,
                      "restarted": False, "lines": []}


def test_not_listed_in_swagger(client):
    assert not any(path.startswith("/admin") for path in client.get("/openapi.json").json()["paths"])
