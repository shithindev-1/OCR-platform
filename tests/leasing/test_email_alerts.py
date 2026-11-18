"""System errors are mailed in short; throttled; never block or break a request; off by default."""

import logging
import sys

import pytest

from app.leasing.core import alerts
from app.leasing.core.alerts import EmailAlertHandler, build_alert, configure_email_alerts
from app.leasing.core.config import Settings


def _settings(**overrides) -> Settings:
    values = dict(
        gemini_api_key="x", alert_email_to="owner@example.com, prod@example.com", alert_server_name="UAT-test",
        smtp_host="smtp.example.com", smtp_username="alerts@example.com", smtp_password="pw",
        alert_email_cooldown_minutes=15, alert_email_from="", alert_email_subject="OCR API Alert",
        smtp_port=587, smtp_starttls=True,
    )
    values.update(overrides)
    return Settings(**values)


def _error_record(message: str, *args, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    return logging.LogRecord("uae_ocr", logging.ERROR, "/app/services/ocr_pipeline.py", 84, message, args, exc_info)


def _wrapped_gemini_error() -> BaseException:
    try:
        try:
            raise ValueError("400 INVALID_ARGUMENT. API key not valid.")
        except ValueError as provider:
            raise RuntimeError("Gemini OCR request failed") from provider
    except RuntimeError as wrapped:
        return wrapped


def _text_and_html(email) -> tuple[str, str]:
    text = email.get_body(preferencelist=("plain",)).get_content()
    html = email.get_body(preferencelist=("html",)).get_content()
    return text, html


def test_the_email_names_environment_issue_reason_impact_and_action_without_code_details():
    record = _error_record("%s unavailable after retry request_id=%s", "Segmentation", "R-1",
                           exc=_wrapped_gemini_error())
    email = build_alert(record, _settings(alert_email_subject="Errors at Leasing OCR API"))
    text, html = _text_and_html(email)
    assert email["Subject"] == ("Errors at Leasing OCR API | UAT-test | Critical: "
                                "AI service (Google Gemini) rejected the API key")
    assert email["To"] == "owner@example.com, prod@example.com"
    assert email["From"] == "alerts@example.com"
    for body in (text, html):
        assert "UAT-test" in body and "UAE" in body and "UTC" in body and "IST" in body
        assert "400 INVALID_ARGUMENT. API key not valid." in body  # the provider's own reason
        assert "Check GEMINI_API_KEY" in body and "Every request fails" in body
        assert "Request R-1" in body
        # never code details: no file, line, logger, exception class or traceback
        for code_detail in (".py", "line 84", "uae_ocr", "ValueError", "RuntimeError", "Traceback"):
            assert code_detail not in body


@pytest.mark.parametrize("message, cause, title, severity", [
    ("Gemini OCR request failed", RuntimeError("503 UNAVAILABLE. The model is overloaded."),
     "AI service (Google Gemini) is not responding", "Critical"),
    ("Gemini OCR request failed", RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded."),
     "AI service (Google Gemini) quota or rate limit reached", "Critical"),
    ("Job failed unexpectedly job_id=429-503", None, "A background job could not finish", "Critical"),
    ("Unhandled error method=POST path=/api/v1/ocr request_id=a429", KeyError("x"), "Unexpected server error",
     "Critical"),
    ("Document box detection/crop failed sha256=abc", ValueError("bad image"), "Document cropping failed",
     "Warning"),
    ("Retention sweep failed", OSError("disk full"), "Scheduled clean-up of old files failed", "Warning"),
])
def test_each_kind_of_error_gets_its_own_plain_title(message, cause, title, severity):
    email = build_alert(_error_record(message, exc=cause), _settings())
    assert email["Subject"] == f"OCR API Alert | UAT-test | {severity}: {title}"


def test_secrets_in_an_error_text_are_masked():
    record = _error_record("Gemini OCR request failed",
                           exc=RuntimeError("timeout calling https://host/v1?key=AIzaSECRET&alt=json"))
    text, html = _text_and_html(build_alert(record, _settings()))
    assert "AIzaSECRET" not in text and "AIzaSECRET" not in html and "key=***" in text


def test_the_same_error_is_mailed_once_per_cooldown_and_the_next_one_counts_the_rest(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_email", lambda email, settings: sent.append(email))
    clock = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])
    handler = EmailAlertHandler(_settings(), background=False)

    for document in range(4):  # one broken Gemini key, four documents in the request
        handler.handle(_error_record("Segmentation unavailable after retry request_id=%s", f"R-{document}"))
    handler.handle(_error_record("Job failed unexpectedly job_id=%s", "J-1"))  # a different error: own email
    assert len(sent) == 2

    clock[0] += 15 * 60 + 1
    handler.handle(_error_record("Segmentation unavailable after retry request_id=%s", "R-9"))
    assert len(sent) == 3
    assert "1 + 3 more in the last 15 minutes" in _text_and_html(sent[-1])[0]


def test_warnings_and_info_are_never_mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_email", lambda email, settings: sent.append(email))
    logger = logging.getLogger("uae_ocr.test_alerts_levels")
    handler = EmailAlertHandler(_settings(), background=False)
    logger.addHandler(handler)
    try:
        logger.info("normal line")
        logger.warning("Your National ID expired - a user's document problem")
        logger.error("Unhandled error method=POST path=/api/v1/ocr")
    finally:
        logger.removeHandler(handler)
    assert len(sent) == 1 and "Unexpected server error" in sent[0]["Subject"]


def test_a_mail_server_failure_never_breaks_the_request(monkeypatch, capsys):
    def refuse(email, settings):
        raise OSError("connection refused")
    monkeypatch.setattr(alerts, "send_email", refuse)
    handler = EmailAlertHandler(_settings(), background=False)
    handler.handle(_error_record("Job failed unexpectedly job_id=%s", "J-2"))  # must not raise
    assert "could not send the alert email: OSError: connection refused" in capsys.readouterr().err


def test_smtp_conversation_uses_starttls_and_login(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def starttls(self, context):
            calls.append(("starttls",))
        def login(self, user, password):
            calls.append(("login", user))
        def send_message(self, email):
            calls.append(("send", email["To"]))

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    settings = _settings()
    alerts.send_email(build_alert(_error_record("boom"), settings), settings)
    assert calls == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "alerts@example.com"),
                     ("send", "owner@example.com, prod@example.com")]


@pytest.mark.parametrize("overrides, attached", [
    ({}, True), ({"alert_email_to": ""}, False), ({"smtp_host": ""}, False),
])
def test_alerts_are_on_only_when_recipients_and_mail_server_are_set(overrides, attached, monkeypatch):
    monkeypatch.setattr(alerts.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    name = "uae_ocr.test_alerts_config"
    configure_email_alerts(_settings(**overrides), name)
    configure_email_alerts(_settings(**overrides), name)  # twice: never two handlers
    handlers = [h for h in logging.getLogger(name).handlers if isinstance(h, EmailAlertHandler)]
    assert len(handlers) == (1 if attached else 0)
    configure_email_alerts(_settings(alert_email_to=""), name)


def test_the_running_app_has_alerts_off_without_settings():
    assert not any(isinstance(h, EmailAlertHandler) for h in logging.getLogger("uae_ocr").handlers)


@pytest.mark.parametrize("message, title", [
    ("Storage link refused: its blob storage is not this environment's storage host=x.blob.core.windows.net",
     "Document links refused - file storage settings may be wrong"),
    ("Blob storage upload of a processed file failed path=a/b.pdf error=denied",
     "Processed files could not be saved to file storage (Azure Blob)"),
    ("Blob storage download failed path=a/b.pdf error=AuthenticationFailed",
     "File storage (Azure Blob) is not reachable"),
    ("Source download failed source=files.example.com error=ConnectTimeout", "Document links could not be downloaded"),
    ("Job still running after 10 minutes, it may be stuck job_id=J-7 percentage=40", "A background job is stuck"),
])
def test_storage_and_stuck_job_alerts_get_their_own_titles(message, title):
    email = build_alert(_error_record(message), _settings())
    assert email["Subject"] == f"OCR API Alert | UAT-test | Critical: {title}"


def test_storage_problems_on_our_side_are_errors_and_a_wrong_path_only_a_warning(monkeypatch, caplog):
    import asyncio

    from app.leasing.schemas.leasing import SourceDocument
    from app.leasing.services.blob_storage import BlobStorageError
    from app.leasing.services.leasing import sources

    source = SourceDocument(source="x", document_id="1", document_type="national_id", document_name="eid.pdf")

    class Storage:
        def __init__(self, error):
            self.error = error

        async def download(self, path, *, max_bytes):
            raise self.error

    levels = []
    for error in (BlobStorageError("AuthenticationFailed"), BlobStorageError("BlobNotFound", not_found=True)):
        caplog.clear()
        with caplog.at_level("WARNING", logger="uae_ocr.leasing"), pytest.raises(Exception):
            asyncio.run(sources.download_blob(Storage(error), "a/eid.pdf", source))
        levels.append(caplog.records[-1].levelname)
    assert levels == ["ERROR", "WARNING"]


def test_a_link_to_a_storage_account_this_environment_does_not_use_is_an_error(monkeypatch, caplog):
    import asyncio

    import httpx

    from app.leasing.schemas.leasing import SourceDocument
    from app.leasing.services.leasing import sources

    monkeypatch.setattr(sources.settings, "allowed_source_hosts", "")

    def attempt(url: str) -> str | None:
        caplog.clear()
        source = SourceDocument(source=url, document_id="1", document_type="national_id", document_name="eid.pdf")
        with caplog.at_level("WARNING", logger="uae_ocr.leasing"), pytest.raises(Exception):
            asyncio.run(sources.download_source(httpx.AsyncClient(), source))
        return caplog.records[-1].levelname if caplog.records else None

    assert attempt("https://otherprod.blob.core.windows.net/c/eid.pdf") == "ERROR"
    assert attempt("https://example.com/eid.pdf") is None   # any other site: just refused, no alert

@pytest.mark.parametrize("message, title, severity", [
    ("Requests rejected by the API's own rate limit identity=10.0.0.1:anonymous:anonymous limit=30 per 60s",
     "Requests rejected by the API's rate limit", "Critical"),
    ("Request rejected: API key missing or not valid reason=invalid_api_key",
     "Requests rejected: API key missing or not valid", "Critical"),
    ("Request rejected: not received over HTTPS path=/api/v1/ocr forwarded_proto=http",
     "Requests rejected: not received over HTTPS", "Critical"),
    ("Job results not found (lost after a restart, on another replica, or expired) job_id=J-1",
     "Job results not found (job lost or expired)", "Critical"),
    ("Request rejected method=POST path=/api/v1/ocr/leasing/detect status_code=422 client=x reason=malformed_request",
     "Requests rejected: request format not valid", "Warning"),
    ("Uploaded file rejected status_code=413 extension=.pdf size_kb=20480 reason=This file is too large.",
     "Uploaded file rejected (too large, wrong type or damaged)", "Warning"),
    ("Warm-up failed target=gemini error=ConnectError", "Service warm-up failed at startup", "Warning"),
])
def test_every_blocked_request_gets_its_own_plain_title(message, title, severity):
    email = build_alert(_error_record(message), _settings())
    assert email["Subject"] == f"OCR API Alert | UAT-test | {severity}: {title}"


def _levels(caplog, action) -> list[str]:
    caplog.clear()
    with caplog.at_level("WARNING"):
        try:
            action()
        except Exception:
            pass
    return [record.levelname for record in caplog.records if record.name.startswith("uae_ocr")]


def test_rate_limit_and_refused_files_and_lost_jobs_are_logged_as_errors(monkeypatch, caplog):
    from types import SimpleNamespace

    from app.leasing.services import jobs, validation

    settings = _settings(rate_limit_requests=1, rate_limit_window_seconds=60)
    validation.enforce_rate_limit("ip:test-alerts:u", settings)
    assert _levels(caplog, lambda: validation.enforce_rate_limit("ip:test-alerts:u", settings)) == ["ERROR"]

    upload = SimpleNamespace(filename="contract.docx", content_type="application/msword")
    assert _levels(caplog, lambda: validation.validate_document_request(
        file=upload, content=b"x", document_name="Contract", document_type="other", settings=settings)) == ["ERROR"]

    assert _levels(caplog, lambda: jobs.get_status("no-such-job")) == ["ERROR"]


def test_a_wrong_api_key_and_plain_http_are_logged_as_errors(monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from app.factory import build_app
    from app.leasing.core.config import settings
    app = build_app(["leasing"])

    client = TestClient(app)
    monkeypatch.setattr(settings, "api_keys", "right-key")
    monkeypatch.setattr(settings, "require_https", False)
    assert "ERROR" in _levels(caplog, lambda: client.get("/api/v1/ocr/leasing/jobs/x", headers={"X-API-Key": "wrong"}))

    monkeypatch.setattr(settings, "api_keys", "")
    monkeypatch.setattr(settings, "require_https", True)
    levels = _levels(caplog, lambda: client.get("/api/v1/ocr/leasing/jobs/x", headers={"X-Forwarded-Proto": "http"}))
    assert "ERROR" in levels
    assert any("not received over HTTPS" in r.getMessage() for r in caplog.records)