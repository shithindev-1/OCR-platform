"""Every log line also goes to the console, tagged with its log file; warnings and errors stand out."""

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from app.leasing.core.config import settings
from app.leasing.core.logging import (
    ConsoleAlertFormatter,
    ConsoleAlertHandler,
    configure_logging,
    console_mode,
)

# Where the test session's logs go (conftest points LOG_DIR at a throwaway folder).
SESSION_LOGS = Path(os.environ["LOG_DIR"]) / "leasing"


def _record(level: int, message: str, exc_info=None, name: str = "uae_ocr") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, None, exc_info)


@pytest.fixture
def console(monkeypatch):
    """Configure logging with a given LOG_TO_CONSOLE; restores the default afterwards."""
    def configure(mode: str) -> None:
        monkeypatch.setattr(settings, "log_to_console", mode)
        configure_logging(settings, SESSION_LOGS)
    yield configure
    monkeypatch.setattr(settings, "log_to_console", "all")
    configure_logging(settings, SESSION_LOGS)


def test_an_error_stands_out_between_rules_with_its_cause():
    try:
        raise RuntimeError("400 API key not valid")
    except RuntimeError:
        exc_info = sys.exc_info()
    text = ConsoleAlertFormatter(colour=False).format(_record(logging.ERROR, "Segmentation unavailable", exc_info))
    lines = text.splitlines()
    assert lines[0] == lines[-1] == "=" * 100
    assert lines[1].startswith("!!! [ocr-api] ") and "ERROR" in lines[1] and "Segmentation unavailable" in lines[1]
    assert "RuntimeError: 400 API key not valid" in text


def test_a_warning_is_one_marked_line_and_colour_only_when_asked():
    plain = ConsoleAlertFormatter(colour=False).format(_record(logging.WARNING, "Segmentation call failed"))
    assert plain.startswith("!! [ocr-api] ") and "\n" not in plain and "\033" not in plain
    coloured = ConsoleAlertFormatter(colour=True).format(_record(logging.WARNING, "Segmentation call failed"))
    assert coloured.startswith("\033[33m") and coloured.endswith("\033[0m")


def test_a_json_log_line_is_its_bare_json_behind_the_file_tag():
    line = json.dumps({"request_id": "R1", "total_tokens": 10})
    text = ConsoleAlertFormatter(colour=True, label="usage", bare_message=True).format(
        _record(logging.INFO, line, name="uae_ocr.usage"))
    assert text == f"[usage] {line}"  # info lines are never coloured


def test_all_sends_every_log_to_the_console_once(console, capsys):
    console("all")
    configure_logging(settings, SESSION_LOGS)  # set up twice (app restart in one process): still one handler each
    for name in ("uae_ocr", "uae_ocr.usage", "uae_ocr.document_results", "uae_ocr.timing"):
        handlers = logging.getLogger(name).handlers
        assert sum(isinstance(handler, ConsoleAlertHandler) for handler in handlers) == 1, name

    logging.getLogger("uae_ocr.leasing").info("normal request line")
    logging.getLogger("uae_ocr.leasing").warning("Segmentation call status=error:ClientError error=boom")
    logging.getLogger("uae_ocr.usage").info('{"request_id": "R1"}')
    logging.getLogger("uae_ocr.document_results").info('{"request_id": "R1", "status": "failed"}')
    out = capsys.readouterr().out
    assert out.count("[ocr-api]") == 2 and "normal request line" in out
    assert out.count("!! [ocr-api]") == 1 and "error=boom" in out
    assert '[usage] {"request_id": "R1"}' in out
    assert '[document-results] {"request_id": "R1", "status": "failed"}' in out


def test_warnings_mode_prints_only_warnings_and_errors(console, capsys):
    console("warnings")
    logging.getLogger("uae_ocr.leasing").info("normal request line")
    logging.getLogger("uae_ocr.usage").info('{"request_id": "R2"}')
    logging.getLogger("uae_ocr.leasing").warning("something to look at")
    out = capsys.readouterr().out
    assert "normal request line" not in out and "R2" not in out
    assert "!! [ocr-api]" in out and "something to look at" in out


def test_off_mode_prints_nothing(console, capsys):
    console("off")
    logging.getLogger("uae_ocr.leasing").warning("only in the files")
    logging.getLogger("uae_ocr.usage").info('{"request_id": "R3"}')
    assert capsys.readouterr().out == ""
    for name in ("uae_ocr", "uae_ocr.usage"):
        assert not any(isinstance(h, ConsoleAlertHandler) for h in logging.getLogger(name).handlers)


@pytest.mark.parametrize("value, mode", [
    ("all", "all"), ("ALL", "all"), ("true", "all"), (True, "all"), ("warnings", "warnings"),
    ("errors", "warnings"), ("off", "off"), ("false", "off"), (False, "off"), ("nonsense", "all"),
])
def test_log_to_console_values(value, mode):
    assert console_mode(value) == mode
