"""logs/alerts.log: every warning and error of the whole service, marked to stand out - added as a
separate handler, so the existing log files keep their lines and format."""

import logging
from pathlib import Path

import pytest

from app.leasing.core.config import settings
from app.leasing.core.logging import AlertsFileHandler, configure_logging


@pytest.fixture
def log_dir(tmp_path, monkeypatch) -> Path:
    yield tmp_path
    # Back to the session's log folder; drop the handlers pointing at tmp_path.
    for name in ("", "uvicorn", "uae_ocr"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if str(tmp_path) in str(getattr(handler, "baseFilename", "")):
                logger.removeHandler(handler)
                handler.close()


def _flush() -> None:
    for name in ("", "uvicorn", "uae_ocr"):
        for handler in logging.getLogger(name).handlers:
            handler.flush()


def _alerts_handler_for(log_dir: Path):
    for logger in (logging.getLogger(), logging.getLogger("uvicorn")):
        for handler in list(logger.handlers):
            if isinstance(handler, AlertsFileHandler):
                logger.removeHandler(handler)  # the session's own one: replaced by tmp_path's below
                handler.close()
    configure_logging(settings, log_dir)


def test_every_warning_and_error_from_app_libraries_and_uvicorn_is_marked(log_dir):
    _alerts_handler_for(log_dir)
    logging.getLogger("uae_ocr.leasing").info("normal request line")
    logging.getLogger("uae_ocr").warning("Segmentation call status=error:ClientError error=boom")
    try:
        raise ValueError("400 API key not valid")
    except ValueError:
        logging.getLogger("uae_ocr").exception("Segmentation unavailable after retry request_id=R1")
    logging.getLogger("google_genai.models").warning("a library warning")
    logging.getLogger("uvicorn.error").error("a server error")
    _flush()

    text = (log_dir / "alerts.log").read_text(encoding="utf-8")
    assert "normal request line" not in text
    assert "\n!! " in "\n" + text and "error=boom" in text
    assert "=" * 100 + "\n!!! " in text and "ValueError: 400 API key not valid" in text
    assert "a library warning" in text and "a server error" in text
    assert text.count("Segmentation unavailable after retry") == 1  # never twice


def test_the_existing_logs_are_unchanged(log_dir):
    _alerts_handler_for(log_dir)
    logging.getLogger("uae_ocr").warning("plain warning line")
    _flush()
    for name in ("errors.log", "ocr-api.log"):
        line = next(line for line in (log_dir / name).read_text(encoding="utf-8").splitlines()
                    if "plain warning line" in line)
        assert not line.startswith("!!") and " WARNING uae_ocr plain warning line" in line
    assert "google" not in (log_dir / "errors.log").read_text(encoding="utf-8")


def test_set_up_twice_still_one_alerts_handler(log_dir):
    _alerts_handler_for(log_dir)
    configure_logging(settings, log_dir)
    for name in ("", "uvicorn"):
        assert sum(isinstance(h, AlertsFileHandler) for h in logging.getLogger(name).handlers) == 1
