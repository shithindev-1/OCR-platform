"""Builds postman/OCR_Platform.postman_collection.json (and the local environment file).

    python scripts/build_postman.py

Every endpoint of the platform, grouped by service, with every direct-upload (form-data) request
spelled out: pick your files in the Body tab and send. tests/combined/test_postman_collection.py fails
if an endpoint is added to the API without a request here, so the collection cannot drift.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "postman"

SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


# --- small builders ----------------------------------------------------------------------------

def _url(path: str, query: dict[str, str] | None = None) -> dict:
    segments = [segment for segment in path.split("/") if segment]
    url = {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": segments}
    if query:
        url["raw"] += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        url["query"] = [{"key": k, "value": v} for k, v in query.items()]
    return url


def _file(key: str, description: str = "") -> dict:
    row = {"key": key, "type": "file", "src": []}
    if description:
        row["description"] = description
    return row


def _text(key: str, value: str, description: str = "", disabled: bool = False) -> dict:
    row = {"key": key, "value": value, "type": "text"}
    if description:
        row["description"] = description
    if disabled:
        row["disabled"] = True
    return row


def _form(rows: list[dict]) -> dict:
    return {"mode": "formdata", "formdata": rows}


def _json(body: dict) -> dict:
    return {"mode": "raw", "raw": json.dumps(body, indent=2), "options": {"raw": {"language": "json"}}}


def _request(name: str, method: str, path: str, *, body: dict | None = None, query: dict | None = None,
             headers: list[dict] | None = None, description: str = "", test: str = "", noauth: bool = False) -> dict:
    request: dict = {"method": method, "header": headers or [], "url": _url(path, query)}
    if body is not None:
        request["body"] = body
    if description:
        request["description"] = description
    if noauth:
        request["auth"] = {"type": "noauth"}
    item: dict = {"name": name, "request": request}
    if test:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": test.strip().splitlines()}}]
    return item


def _folder(name: str, description: str, items: list[dict]) -> dict:
    return {"name": name, "description": description, "item": items}


SAVE_JOB_ID = """
pm.test("Job started", () => pm.response.to.have.status(202));
pm.collectionVariables.set("jobId", pm.response.json().job_id);
"""

JSON_HEADER = [{"key": "Content-Type", "value": "application/json"}]

# Leasing's three documents of one individual tenant, as three attached files.
LEASING_UPLOAD_ROWS = [
    _text("tenant_type", "individual", "individual or company - sent once per request"),
    _text("document_refnumber", "1001", "Your reference for this request - sent once per request"),
    _file("file", "National ID, front"),
    _text("document_id", "16"),
    _text("document_type", "National Id"),
    _text("document_name", "National ID Front"),
    _file("file", "National ID, back"),
    _text("document_id", "17"),
    _text("document_type", "National Id"),
    _text("document_name", "National ID Back"),
    _file("file", "Passport"),
    _text("document_id", "18"),
    _text("document_type", "Passport"),
    _text("document_name", "Passport"),
]
LEASING_SOURCES = [
    {"source": "ApplicationFiles/LeasingRequestOCR/1/abc/NID Front.jpg", "document_id": "16",
     "document_type": "National Id", "document_name": "National ID Front"},
    {"source": "ApplicationFiles/LeasingRequestOCR/1/abc/NID Back.jpg", "document_id": "17",
     "document_type": "National Id", "document_name": "National ID Back"},
    {"source": "ApplicationFiles/LeasingRequestOCR/1/abc/Passport.pdf", "document_id": "18",
     "document_type": "Passport", "document_name": "Passport"},
]
DETECT_UPLOAD_ROWS = [
    _text("tenant_type", "individual"),
    _text("document_refnumber", "1001"),
    _file("file", "Any file: the API finds each document's type itself (several documents may share one file)"),
    _file("file", "Another file (optional)"),
]
OCR_UPLOAD_ROWS = [
    _file("file", "First document"),
    _text("documentName", "National ID Front"),
    _text("documentType", "National Id"),
    _file("file", "Second document"),
    _text("documentName", "Passport"),
    _text("documentType", "Passport"),
]
PAYMENTS = [
    {"RentPaymentId": 94814, "Installment": 1, "ServiceId": 1, "ServiceDescription": "Property Rent",
     "Amount": 8400, "DueDate": "2026-11-05T00:00:00"},
    {"RentPaymentId": 94815, "Installment": 2, "ServiceId": 1, "ServiceDescription": "Property Rent",
     "Amount": 8400, "DueDate": "2026-10-17T00:00:00"},
    {"RentPaymentId": 94819, "Installment": 1, "ServiceId": 4, "ServiceDescription": "VAT on Rent",
     "Amount": 840, "DueDate": "2026-08-14T00:00:00"},
]
CHEQUE_INFO = {"ChequepayeeName": None, "ChequeHolderName": None, "ChequeHolderNameAr": None,
               "ChequeHolderIBAN": None}


def build_collection() -> dict:
    health = _folder("Health", "Open (no API key). `/health` is the load-balancer probe; `/api/v1/health` adds the "
                               "version and which services this process runs.", [
        _request("Health", "GET", "/health", noauth=True),
        _request("Health - version and running services", "GET", "/api/v1/health", noauth=True),
    ])

    leasing_upload = _folder("Upload files (direct)", "Attach the files in the Body tab (pick a file in each `file` "
                                                      "row). Rows with the same key are paired by position.", [
        _request("Leasing - upload files", "POST", "/api/v1/ocr/leasing/upload", body=_form(LEASING_UPLOAD_ROWS),
                 description="Several documents at once. `file`, `document_id`, `document_type`, `document_name` "
                             "are repeated and paired by position."),
        _request("Leasing - upload files (background job, for a progress bar)", "POST",
                 "/api/v1/ocr/leasing/jobs/upload", body=_form(LEASING_UPLOAD_ROWS), test=SAVE_JOB_ID,
                 description="Same form as the upload above, answered at once with a job id (saved to `jobId`). "
                             "Poll **Job progress and result**."),
        _request("Leasing - upload files, links in the form", "POST", "/api/v1/ocr/leasing/upload",
                 body=_form([_text("tenant_type", "individual"), _text("document_refnumber", "1001"),
                             _text("sources[0][source]", LEASING_SOURCES[0]["source"]),
                             _text("sources[0][document_id]", "16"), _text("sources[0][document_type]", "National Id"),
                             _text("sources[0][document_name]", "National ID Front"),
                             _text("sources[1][source]", LEASING_SOURCES[2]["source"]),
                             _text("sources[1][document_id]", "18"), _text("sources[1][document_type]", "Passport"),
                             _text("sources[1][document_name]", "Passport")]),
                 description="The same documents the JSON route takes, for clients that can only send form-data."),
        _request("Leasing - original /ocr, upload files", "POST", "/api/v1/ocr", body=_form(OCR_UPLOAD_ROWS),
                 headers=[{"key": "X-Detection", "value": "off", "disabled": True,
                           "description": "off = skip crop/rotate for this request"},
                          {"key": "X-Validation", "value": "off", "disabled": True,
                           "description": "off = skip the verification pass for this request"}],
                 description="The original multipart endpoint: `file`, `documentName`, `documentType` repeated and "
                             "paired by position. Errors come back in its own `errorInfo` shape."),
        _request("Leasing - type detection, upload files", "POST", "/api/v1/ocr/leasing/detect/upload",
                 body=_form(DETECT_UPLOAD_ROWS),
                 description="No document types to send: the API finds each document's type itself."),
        _request("Leasing - type detection, upload files (background job)", "POST",
                 "/api/v1/ocr/leasing/detect/jobs/upload", body=_form(DETECT_UPLOAD_ROWS), test=SAVE_JOB_ID),
    ])

    leasing_urls = _folder("By link (JSON)", "Documents named by URL or blob path; the API downloads them itself.", [
        _request("Leasing - by links", "POST", "/api/v1/ocr/leasing", headers=JSON_HEADER, body=_json(
            {"tenant_type": "individual", "document_refnumber": "1001", "sources": LEASING_SOURCES})),
        _request("Leasing - by links (background job)", "POST", "/api/v1/ocr/leasing/jobs", headers=JSON_HEADER,
                 test=SAVE_JOB_ID, body=_json(
                     {"tenant_type": "individual", "document_refnumber": "1001", "sources": LEASING_SOURCES})),
        _request("Leasing - type detection, by links", "POST", "/api/v1/ocr/leasing/detect", headers=JSON_HEADER,
                 body=_json({"tenant_type": "individual", "document_refnumber": "1001",
                             "sources": [{"source": LEASING_SOURCES[0]["source"]},
                                         {"source": LEASING_SOURCES[2]["source"]}]})),
        _request("Leasing - type detection, by links (background job)", "POST", "/api/v1/ocr/leasing/detect/jobs",
                 headers=JSON_HEADER, test=SAVE_JOB_ID,
                 body=_json({"tenant_type": "individual", "document_refnumber": "1001",
                             "sources": [{"source": LEASING_SOURCES[0]["source"]}]})),
    ])

    leasing_results = _folder("Job progress and results", "", [
        _request("Job progress and result", "GET", "/api/v1/ocr/leasing/jobs/{{jobId}}",
                 description="Poll until `status` is Completed. `result` is null while Processing."),
        _request("View an OCR result file, by URL", "GET", "/api/v1/ocr/leasing/files",
                 query={"url": "{{ocrFileUrl}}"},
                 description="Pass a source's `ocr_file` URL as `url`. Shown inline."),
        _request("View an OCR result file, by blob path", "GET", "/api/v1/ocr/leasing/files/{{ocrBlobPath}}"),
    ])

    leasing_negative = _folder("Checks", "Requests that should be refused - a quick look that auth and "
                                         "validation work.", [
        _request("Auth - missing API key (expect 401)", "POST", "/api/v1/ocr/leasing", headers=JSON_HEADER,
                 noauth=True, body=_json({"tenant_type": "individual", "document_refnumber": "1", "sources": []})),
        _request("Validation - empty body (expect 422)", "POST", "/api/v1/ocr/leasing", headers=JSON_HEADER,
                 body=_json({})),
    ])

    leasing = _folder("Leasing OCR", "Identity and company documents. Errors are `{\"detail\": ...}` (the original "
                                     "`/api/v1/ocr` answers in its own `errorInfo` shape). For plain-http local "
                                     "testing set `REQUIRE_HTTPS=false` in .env.",
                      [leasing_upload, leasing_urls, leasing_results, leasing_negative])

    cheque_requests = _folder("Extract", "Read cheques only - no payments to match.", [
        _request("Cheque - extract, upload one cheque", "POST", "/api/v1/cheques/extract",
                 body=_form([_file("files", "One cheque image or PDF")])),
        _request("Cheque - extract, upload several cheques", "POST", "/api/v1/cheques/extract",
                 body=_form([_file("files", "Cheque 1"), _file("files", "Cheque 2"), _file("files", "Cheque 3")]),
                 description="Up to `max_files_per_request` (default 20) files; one file may hold several cheques."),
    ])
    cheque_match = _folder("Match to payments", "Read the cheques and match each to the expected rent payments by "
                                                "Amount + DueDate. `cheque_info` is optional and sent once.", [
        _request("Cheque - match, upload files", "POST", "/api/v1/cheques/match/upload", body=_form([
            _file("files", "Cheque 1"), _file("files", "Cheque 2"),
            _text("payments", json.dumps(PAYMENTS), "JSON list of the expected payments"),
            _text("cheque_info", json.dumps(CHEQUE_INFO), "Optional JSON object checked for every payment"),
        ])),
        _request("Cheque - match, by file links", "POST", "/api/v1/cheques/match", headers=JSON_HEADER, body=_json(
            {"file_urls": ["cheque1.jpg", "cheque2.jpg"], "cheque_info": CHEQUE_INFO, "payments": PAYMENTS}),
                 description="`file_urls`: blob URLs inside the configured container, or (APP_ENV=local) file names "
                             "in `local_files_dir`."),
    ])
    cheque_negative = _folder("Checks", "Requests that should be refused.", [
        _request("Auth - missing API key (expect 401)", "POST", "/api/v1/cheques/match", headers=JSON_HEADER,
                 noauth=True, body=_json({"file_urls": ["cheque_1.pdf"], "payments": [{"Amount": 1,
                                                                                         "DueDate": "2026-01-01"}]})),
        _request("Validation - no files (expect 422)", "POST", "/api/v1/cheques/extract", body=_form([])),
        _request("Validation - empty payments (expect 400)", "POST", "/api/v1/cheques/match", headers=JSON_HEADER,
                 body=_json({"file_urls": ["cheque1.jpg"], "payments": []})),
        _request("Validation - unsupported file type", "POST", "/api/v1/cheques/extract",
                 body=_form([_text("files", "this is not a cheque")])),
    ])
    cheque = _folder("Cheque OCR", "Cheque extraction and matching. Errors are `{\"error\": ...}`; every response "
                                   "carries an `X-Request-ID`.", [cheque_requests, cheque_match, cheque_negative])

    return {
        "info": {
            "name": "OCR Platform API",
            "description": "One project, two services: **Leasing OCR** and **Cheque OCR**.\n\n"
                           "1. Start it: `python -m app` (both services) - Swagger at `{{baseUrl}}/docs`.\n"
                           "2. Set `baseUrl` (default http://localhost:8000) and `apiKey` (leave empty when API_KEYS "
                           "is empty locally).\n"
                           "3. For each upload request, open the Body tab and pick your files in the `file` / "
                           "`files` rows.\n\n"
                           "One `X-API-Key` header is accepted by both services.",
            "schema": SCHEMA,
        },
        "auth": {"type": "apikey", "apikey": [
            {"key": "key", "value": "X-API-Key", "type": "string"},
            {"key": "value", "value": "{{apiKey}}", "type": "string"},
            {"key": "in", "value": "header", "type": "string"},
        ]},
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000"},
            {"key": "apiKey", "value": ""},
            {"key": "jobId", "value": ""},
            {"key": "ocrFileUrl", "value": ""},
            {"key": "ocrBlobPath", "value": ""},
        ],
        "item": [health, leasing, cheque],
    }


def build_environment() -> dict:
    return {
        "name": "OCR Platform - local",
        "values": [
            {"key": "baseUrl", "value": "http://localhost:8000", "enabled": True},
            {"key": "apiKey", "value": "", "type": "secret", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "OCR_Platform.postman_collection.json").write_text(
        json.dumps(build_collection(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT / "OCR_Platform.local.postman_environment.json").write_text(
        json.dumps(build_environment(), indent=2) + "\n", encoding="utf-8")
    print("Wrote", OUT / "OCR_Platform.postman_collection.json")


if __name__ == "__main__":
    main()
