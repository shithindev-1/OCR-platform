"""Log file setup: rotating per-purpose log files with UTC/UAE/IST timestamps."""

import logging
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.common.timezones import IST_TZ as IST_TIME
from app.common.timezones import UAE_TZ as UAE_TIME
from app.leasing.core.config import Settings


def log_timestamps() -> dict[str, str]:
    """The three stamps every JSON log line carries.

    UTC stays the canonical one — it is what sorts correctly and what correlates with anything
    outside this service — with the two local readings alongside it so a log can be read against
    a wall clock in either office without converting in your head.
    """
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.isoformat(),
        "timestamp_uae": now.astimezone(UAE_TIME).isoformat(),
        "timestamp_ist": now.astimezone(IST_TIME).isoformat(),
    }


class MultiZoneFormatter(logging.Formatter):
    """Stamps the plain-text logs with UTC, UAE, and IST on every line.

    The JSON logs carry the three as separate fields; these lines have only the one asctime slot,
    so all three are rendered into it. Each keeps its own date — past 20:00 UTC the UAE and Indian
    dates have already rolled over, and a bare time would read as the wrong day.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return (
            f"{moment.isoformat(timespec='milliseconds')}"
            f" | UAE {moment.astimezone(UAE_TIME).strftime('%Y-%m-%d %H:%M:%S')}"
            f" | IST {moment.astimezone(IST_TIME).strftime('%Y-%m-%d %H:%M:%S')}"
        )


_RED, _YELLOW, _BOLD, _RESET = "\033[31m", "\033[33m", "\033[1m", "\033[0m"
_RULE = "=" * 100


class ConsoleAlertFormatter(MultiZoneFormatter):
    """Every log line on the console, tagged with the log file it also goes to, warnings and errors
    marked so they stand out.

    A normal line starts `[<file>]` (e.g. `[ocr-api]`, `[usage]`); the JSON logs' lines are printed
    as their bare JSON after the tag, exactly as in the file. A WARNING is one line starting
    `!! [<file>]`; an ERROR (or worse) sits between two rules with `!!! [<file>]` in front and its
    traceback below - the last traceback line is the real cause (a refused Gemini call, say). ASCII
    only, so any console code page prints it. Colour (yellow / bold red) only on an interactive
    terminal: in a service's log file or `docker logs` escape codes would be noise, and the marks
    alone already stand out there.
    """

    def __init__(self, *, colour: bool, label: str = "ocr-api", bare_message: bool = False) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.colour = colour
        self.label = label
        self.bare_message = bare_message

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage() if self.bare_message else super().format(record)
        tag = f"[{self.label}] " if self.label else ""
        if record.levelno >= logging.ERROR:
            text = f"{_RULE}\n!!! {tag}{text}\n{_RULE}"
            return f"{_BOLD}{_RED}{text}{_RESET}" if self.colour else text
        if record.levelno >= logging.WARNING:
            text = f"!! {tag}{text}"
            return f"{_YELLOW}{text}{_RESET}" if self.colour else text
        return f"{tag}{text}"


class AlertsFileHandler(TimedRotatingFileHandler):
    """logs/alerts.log: one extra handler, on the root logger (and Uvicorn's, which does not pass its
    records up), so it receives every WARNING and ERROR of the whole service - every app module, the
    Gemini SDK and other libraries, Uvicorn - in the stand-out format (`!!` warning lines, `====`
    blocks with `!!!` and the traceback for errors). The existing log files are not touched.
    A record reaching it twice (through both loggers) is written once."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "_in_alerts_log", False):
            return
        record._in_alerts_log = True
        super().emit(record)


def _console_supports_colour(stream: object) -> bool:
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if sys.platform == "win32":
        try:  # Older Windows consoles need this to read escape codes; colorama comes with uvicorn there.
            import colorama
            colorama.just_fix_windows_console()
        except (ImportError, AttributeError):
            return False
    return True


class ConsoleAlertHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at the moment of each record (test runners and service
    wrappers replace it), never to a stream captured once at startup. One stream for every line, so
    the console keeps the order things happened in (and hosts that flag stderr as errors don't)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


class LibraryConsoleHandler(ConsoleAlertHandler):
    """Libraries' warnings and errors on the console, now that the root logger has a handler (the
    alerts file) and Python no longer prints them itself. App records are left to the app's own
    console handler."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uae_ocr" or record.name.startswith("uae_ocr."):
            return
        super().emit(record)


def _configure_alerts_file(log_path: Path, settings: Settings, *, console: bool) -> None:
    handler: AlertsFileHandler | None = None
    for logger in (logging.getLogger(), logging.getLogger("uvicorn")):
        if any(isinstance(existing, AlertsFileHandler) for existing in logger.handlers):
            continue
        if handler is None:
            handler = AlertsFileHandler(log_path, when="midnight", backupCount=settings.log_retention_days,
                                        encoding="utf-8")
            handler.setLevel(logging.WARNING)
            handler.setFormatter(ConsoleAlertFormatter(colour=False, label=""))
        logger.addHandler(handler)
    root = logging.getLogger()
    library_console = [h for h in root.handlers if isinstance(h, LibraryConsoleHandler)]
    if console and not library_console:
        console_handler = LibraryConsoleHandler()
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(ConsoleAlertFormatter(colour=_console_supports_colour(sys.stdout), label="library"))
        root.addHandler(console_handler)
    elif not console:
        for existing in library_console:
            root.removeHandler(existing)


def _configure_console(logger_name: str, label: str, *, level: int, bare_message: bool = False) -> None:
    """One console handler on `logger_name` (never two; a second call only updates its level)."""
    logger = logging.getLogger(logger_name)
    for handler in logger.handlers:
        if isinstance(handler, ConsoleAlertHandler):
            handler.setLevel(level)
            return
    handler = ConsoleAlertHandler()
    handler.setLevel(level)
    handler.setFormatter(ConsoleAlertFormatter(
        colour=_console_supports_colour(sys.stdout), label=label, bare_message=bare_message,
    ))
    logger.addHandler(handler)


def _remove_console(logger_name: str) -> None:
    logger = logging.getLogger(logger_name)
    for handler in [h for h in logger.handlers if isinstance(h, ConsoleAlertHandler)]:
        logger.removeHandler(handler)


# The JSON logs (logger -> file name without .log); with LOG_TO_CONSOLE=all their lines go to the
# console too, tagged with that name.
_JSON_LOGS = {
    "uae_ocr.hits": "hits",
    "uae_ocr.results": "results",
    "uae_ocr.token_breakdown": "token-breakdown",
    "uae_ocr.usage": "usage",
    "uae_ocr.document_usage": "document-usage",
    "uae_ocr.timing": "timing",
    "uae_ocr.document_results": "document-results",
    "uae_ocr.rotation": "rotation",
}


def console_mode(value: object) -> str:
    """LOG_TO_CONSOLE as one of all | warnings | off (true/false read as all/off)."""
    text = str(value).strip().lower()
    return {"true": "all", "1": "all", "yes": "all", "false": "off", "0": "off", "no": "off",
            "none": "off", "errors": "warnings"}.get(text, text if text in {"all", "warnings", "off"} else "all")


def _configure_console_logs(mode: str) -> None:
    """LOG_TO_CONSOLE: all = every line of every log file also on the console (the main log at INFO
    plus each JSON log, tagged with its file); warnings = only warnings and errors; off = files only."""
    if mode == "off":
        for name in ("uae_ocr", *_JSON_LOGS):
            _remove_console(name)
        return
    _configure_console("uae_ocr", "ocr-api", level=logging.INFO if mode == "all" else logging.WARNING)
    for name, label in _JSON_LOGS.items():
        if mode == "all":
            _configure_console(name, label, level=logging.INFO, bare_message=True)
        else:
            _remove_console(name)


def _configure_file_logger(
    logger_name: str,
    log_path: Path,
    settings: Settings,
    formatter: logging.Formatter,
    *,
    level: int = logging.INFO,
    propagate: bool = True,
) -> None:
    logger = logging.getLogger(logger_name)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    logger.propagate = propagate
    resolved_path = str(log_path.resolve())
    already_attached = any(
        isinstance(existing, TimedRotatingFileHandler) and getattr(existing, "baseFilename", None) == resolved_path
        for existing in logger.handlers
    )
    if not already_attached:
        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            backupCount=settings.log_retention_days,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def configure_logging(settings: Settings, log_directory: Path) -> None:
    # `log_directory` is anchored to the project root by the caller, so a service started from another
    # directory logs in the same place.
    log_directory.mkdir(parents=True, exist_ok=True)

    _configure_file_logger(
        "uae_ocr",
        log_directory / "ocr-api.log",
        settings,
        MultiZoneFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"),
    )
    # Same logger, second handler: WARNING+ records are duplicated here for fast incident triage.
    _configure_file_logger(
        "uae_ocr",
        log_directory / "errors.log",
        settings,
        MultiZoneFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"),
        level=logging.WARNING,
    )
    # Separate file, extra handler (nothing above changes): every WARNING/ERROR of the whole service -
    # all app modules, libraries, Uvicorn - marked to stand out, errors with their traceback.
    _configure_alerts_file(
        log_directory / "alerts.log", settings, console=console_mode(settings.log_to_console) != "off",
    )
    # One JSON object per line: hit_id/timestamp/tokens/duration for every Gemini call.
    _configure_file_logger(
        "uae_ocr.hits",
        log_directory / "hits.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per line: hit_id + the final parsed result for that hit.
    _configure_file_logger(
        "uae_ocr.results",
        log_directory / "results.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per line: input/output tokens split by thinking vs. image vs. pdf for every Gemini call.
    _configure_file_logger(
        "uae_ocr.token_breakdown",
        log_directory / "token-breakdown.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per line: every model call one request made, each under its own key, with
    # per-pipeline and overall totals. The only file that accounts for a whole request - hits.log
    # and token-breakdown.log cover the extraction call alone.
    _configure_file_logger(
        "uae_ocr.usage",
        log_directory / "usage.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per line: one request's tokens per document (with its document type), so the
    # cost of each document type can be estimated. Sums to the request's line in usage.log.
    _configure_file_logger(
        "uae_ocr.document_usage",
        log_directory / "document-usage.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per request: its total time and each stage's time, and under "documents" each
    # document's type and the time of each step done for it.
    _configure_file_logger(
        "uae_ocr.timing",
        log_directory / "timing.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per request: every document's outcome - its status, which step failed it and why,
    # the message the caller got, and why a copy was set aside (ignored) - so any failed request can be
    # explained from the logs alone.
    _configure_file_logger(
        "uae_ocr.document_results",
        log_directory / "document-results.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # One JSON object per line: the rotation Gemini asked for and what OpenCV actually applied,
    # per cropped document, so bad rotations can be audited without re-running anything.
    _configure_file_logger(
        "uae_ocr.rotation",
        log_directory / "rotation.log",
        settings,
        logging.Formatter("%(message)s"),
        propagate=False,
    )
    # The same lines on the console too (where a hosted server's logs are read: the service output,
    # `docker logs`), each tagged with its file, warnings and errors marked to stand out.
    _configure_console_logs(console_mode(settings.log_to_console))
    # System errors also mailed in short to ALERT_EMAIL_TO (off until it and SMTP_HOST are set).
    from app.leasing.core.alerts import configure_email_alerts  # here: alerts.py imports this module
    configure_email_alerts(settings)