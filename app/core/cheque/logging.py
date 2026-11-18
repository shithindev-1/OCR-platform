"""Cheque logging: its own logger namespace ("cheque"), its own files, its own console tag.

    logs/cheque/app/<date>.log              application log (INFO and up, every step of every flow)
    logs/cheque/errors/<date>.log           WARNING and up, a copy of the above for quick triage
    logs/cheque/results/<date>.jsonl        one JSON line per request with the full result
    logs/cheque/hits/<date>.jsonl           one JSON line per Gemini call (tokens, time, document type)
    logs/cheque/token-breakdown/<date>.jsonl  input / output token split of each Gemini call
    logs/cheque/usage/<date>.jsonl          one JSON line per request: every call made and the totals
    logs/cheque/document-usage/<date>.jsonl one JSON line per document: its type and its tokens
    logs/cheque/timing/<date>.jsonl         one JSON line per request: stage and per-document times

Files are named by the UAE date and roll over at UAE midnight. Every line also goes to the console
(stdout), tagged with its file: `[cheque-api] ...`, `[cheque-hits] {...}`.

Only Cheque's own records (logger names `cheque`, `cheque.*`) are written here. When another service
shares the process they stop at the `cheque` logger, so they never reach the other service's files;
when Cheque runs alone its handlers sit on the root logger, as before, and also take library output.
"""

import json
import logging
from pathlib import Path

from app.core.logging import (
    APP_LOG_FORMAT,
    ConsoleTagFormatter,
    DailyFileHandler,
    DualTimezoneFormatter,
    StdoutHandler,
)
from app.core.timezones import now_ist, now_uae

APP_LOGGER_NAME = "cheque"
RESULTS_LOGGER_NAME = "cheque.results"
HITS_LOGGER_NAME = "cheque.hits"
TOKEN_BREAKDOWN_LOGGER_NAME = "cheque.token_breakdown"
USAGE_LOGGER_NAME = "cheque.usage"
DOCUMENT_USAGE_LOGGER_NAME = "cheque.document_usage"
TIMING_LOGGER_NAME = "cheque.timing"

# JSON logs: logger name -> (folder under logs/cheque/, console tag)
JSON_LOGS = {
    RESULTS_LOGGER_NAME: ("results", "cheque-results"),
    HITS_LOGGER_NAME: ("hits", "cheque-hits"),
    TOKEN_BREAKDOWN_LOGGER_NAME: ("token-breakdown", "cheque-token-breakdown"),
    USAGE_LOGGER_NAME: ("usage", "cheque-usage"),
    DOCUMENT_USAGE_LOGGER_NAME: ("document-usage", "cheque-document-usage"),
    TIMING_LOGGER_NAME: ("timing", "cheque-timing"),
}

_MARK = "_cheque_log_handler"


def _marked(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _MARK, True)
    return handler


def _drop_ours(logger: logging.Logger) -> None:
    """Remove the handlers an earlier call put here (the app is rebuilt in one process by tests)."""
    for handler in [h for h in logger.handlers if getattr(h, _MARK, False)]:
        logger.removeHandler(handler)
        handler.close()


def setup_logging(
    level: str = "INFO", logs_dir: str = "logs", *, shared: bool = False, console: str = "all",
) -> None:
    """`shared`: another service runs in this process, so Cheque keeps its records to itself.
    `console`: all (every log also on stdout) | warnings (only warnings and errors) | off."""
    base = Path(logs_dir)
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    root = logging.getLogger()
    # Alone: handlers on the root logger (Cheque's lines and everything else). Shared: on the "cheque"
    # logger only, and its records stop there.
    home = app_logger if shared else root
    _drop_ours(root)
    _drop_ours(app_logger)
    app_logger.propagate = not shared
    (app_logger if shared else root).setLevel(level.upper())

    formatter = DualTimezoneFormatter(APP_LOG_FORMAT)
    app_file = _marked(DailyFileHandler(base / "app", ".log"))
    app_file.setFormatter(formatter)
    errors_file = _marked(DailyFileHandler(base / "errors", ".log"))
    errors_file.setLevel(logging.WARNING)
    errors_file.setFormatter(formatter)
    home.addHandler(app_file)
    home.addHandler(errors_file)
    if console != "off":
        stdout = _marked(StdoutHandler())
        stdout.setLevel(logging.INFO if console == "all" else logging.WARNING)
        stdout.setFormatter(ConsoleTagFormatter("cheque-api"))
        home.addHandler(stdout)

    for name, (folder, label) in JSON_LOGS.items():
        logger = logging.getLogger(name)
        _drop_ours(logger)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        file_handler = _marked(DailyFileHandler(base / folder, ".jsonl"))
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(file_handler)
        if console == "all":
            stdout = _marked(StdoutHandler())
            stdout.setFormatter(ConsoleTagFormatter(label, bare=True))
            logger.addHandler(stdout)

    # Keep third-party HTTP noise out of the app log
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_result(payload: dict) -> None:
    """Append one request's result as a JSON line to today's results log."""
    record = {
        "timestamp_uae": now_uae().isoformat(timespec="seconds"),
        "timestamp_ist": now_ist().isoformat(timespec="seconds"),
        **payload,
    }
    logging.getLogger(RESULTS_LOGGER_NAME).info(
        json.dumps(record, ensure_ascii=False, default=str)
    )
