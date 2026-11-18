"""The Postman collection covers every endpoint, and every upload request really is form-data."""

import json
import re
import sys
from pathlib import Path

import pytest

from app.factory import build_app

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import build_postman  # noqa: E402


def _requests(items: list[dict]) -> list[dict]:
    found = []
    for item in items:
        if "item" in item:
            found += _requests(item["item"])
        else:
            found.append(item["request"])
    return found


def _shape(path: str) -> str:
    """`/api/v1/ocr/leasing/jobs/{{jobId}}` and `/.../jobs/{job_id}` compare equal."""
    path = path.split("?")[0].removeprefix("{{baseUrl}}")
    return re.sub(r"\{\{?[^}]+\}\}?", "{x}", path)


@pytest.fixture(scope="module")
def collection() -> dict:
    return build_postman.build_collection()


@pytest.fixture(scope="module")
def operations() -> set[tuple[str, str]]:
    spec = build_app(["leasing", "cheque"]).openapi()
    return {(method.upper(), _shape(path)) for path, ops in spec["paths"].items() for method in ops}


def test_every_endpoint_has_a_postman_request(collection, operations) -> None:
    in_postman = {(r["method"], _shape(r["url"]["raw"])) for r in _requests(collection["item"])}
    assert operations - in_postman == set(), "endpoints missing from the Postman collection"


def test_every_postman_request_points_at_a_real_endpoint(collection, operations) -> None:
    in_postman = {(r["method"], _shape(r["url"]["raw"])) for r in _requests(collection["item"])}
    assert in_postman - operations == set(), "Postman requests for endpoints that do not exist"


def test_upload_requests_are_form_data_with_file_pickers(collection) -> None:
    uploads = [r for r in _requests(collection["item"]) if r["url"]["raw"].endswith(("/upload", "/extract", "/ocr"))
               and r["method"] == "POST"]
    assert len(uploads) >= 7
    for request in uploads:
        assert request["body"]["mode"] == "formdata", request["url"]["raw"]
    with_files = [r for r in uploads if any(row["type"] == "file" for row in r["body"]["formdata"])]
    assert len(with_files) >= 7


def test_the_committed_collection_is_up_to_date(collection) -> None:
    committed = json.loads((ROOT / "postman" / "OCR_Platform.postman_collection.json").read_text(encoding="utf-8"))
    assert committed == collection, "run: python scripts/build_postman.py"
