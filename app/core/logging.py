"""Logging pieces both services share, and the "common" log bucket.

Every log record belongs to exactly one bucket, decided by its logger name:

    uae_ocr, uae_ocr.*   Leasing   logs/leasing/...
    cheque, cheque.*     Cheque    logs/cheque/...
    anything else        common    logs/common/...   (Uvicorn, the Gemini SDK, httpx, shared helpers)

When one service runs alone it keeps everything, as it always did. Only when two services share a
process do their records stay apart (each service stops its records at its own logger) and the common
bucket exists, so a line from one service never lands in the other's files, and a library warning is
written once, in `logs/common/`, instead of in a service's log.

Every file line also goes to the console (stdout), tagged with the file it is written to.
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from app.core.timezones import IST_TZ, UAE_TZ, format_timestamp, now_uae

LEASING_NAMESPACE = "uae_ocr"
CHEQUE_NAMESPACE = "cheque"
NAMESPACES = (LEASING_NAMESPACE, CHEQUE_NAMESPACE)

APP_LOG_FORMAT = "%(uae_time)s UAE | %(ist_time)s IST | %(levelname)-8s | %(name)s | %(message)s"
_RULE = "=" * 100


def in_namespace(name: str, namespace: str) -> bool:
    return name == namespace or name.startswith(namespace + ".")


def belongs_to_a_service(name: str) -> bool:
    return any(in_namespace(name, namespace) for namespace in NAMESPACES)


def log_timestamps() -> dict[str, str]:
    """The three stamps every JSON log line carries: UTC, UAE and IST."""
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.isoformat(),
        "timestamp_uae": now.astimezone(UAE_TZ).isoformat(),
        "timestamp_ist": now.astimezone(IST_TZ).isoformat(),
    }


class DualTimezoneFormatter(logging.Formatter):
    """Adds `uae_time` and `ist_time` to every record."""

    def format(self, record: logging.LogRecord) -> str:
        record.uae_time = format_timestamp(record.created, UAE_TZ)
        record.ist_time = format_timestamp(record.created, IST_TZ)
        return super().format(record)


class ConsoleTagFormatter(DualTimezoneFormatter):
    """A log line for the console, tagged with the file it is also written to.

    A normal line starts `[<label>]`; a JSON log's line (`bare=True`) is printed as its bare JSON after
    the tag, exactly as in the file. A WARNING is one line starting `!! [<label>]`; an ERROR sits between
    two rules with `!!! [<label>]` in front and its traceback below. ASCII only, so any console prints it.
    """

    def __init__(self, label: str, *, bare: bool = False) -> None:
        super().__init__(APP_LOG_FORMAT)
        self.label = label
        self.bare = bare

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage() if self.bare else super().format(record)
        tag = f"[{self.label}] "
        if record.levelno >= logging.ERROR:
            return f"{_RULE}\n!!! {tag}{text}\n{_RULE}"
        if record.levelno >= logging.WARNING:
            return f"!! {tag}{text}"
        return f"{tag}{text}"


class StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at the moment of each record (test runners and service wrappers
    replace it), never to a stream captured once at startup."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


class DailyFileHandler(logging.Handler):
    """Writes to `<directory>/<UAE date><suffix>`, switching files at UAE midnight.

    It never renames a file, so a second process reading or tailing the folder cannot make it fail."""

    def __init__(self, directory: Path, suffix: str) -> None:
        super().__init__()
        self.directory = directory
        self.suffix = suffix
        self.directory.mkdir(parents=True, exist_ok=True)
        self._date: Optional[str] = None
        self._stream: Optional[TextIO] = None

    def _get_stream(self) -> TextIO:
        today = now_uae().strftime("%Y-%m-%d")
        if today != self._date or self._stream is None:
            if self._stream:
                self._stream.close()
            path = self.directory / f"{today}{self.suffix}"
            self._stream = open(path, "a", encoding="utf-8")
            self._date = today
        return self._stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._get_stream()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self.lock:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


class _NotAServiceRecord(logging.Filter):
    """Lets through only the records no service owns: libraries, Uvicorn, shared helpers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not belongs_to_a_service(record.name)


class _CommonFileHandler(DailyFileHandler):
    """The common alerts file: on the root logger and on Uvicorn's (which does not pass its records up),
    so a record reaching it through both is written once."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "_in_common_log", False):
            return
        record._in_common_log = True
        super().emit(record)


_COMMON_MARK = "_ocr_platform_common"


def configure_common_logging(log_directory: Path) -> None:
    """The common bucket: `logs/common/alerts/<date>.log` and the console, WARNING and above, for every
    record no service owns. Only set up when two services share a process (see the module docstring)."""
    for logger in (logging.getLogger(), logging.getLogger("uvicorn")):
        for existing in [h for h in logger.handlers if getattr(h, _COMMON_MARK, False)]:
            logger.removeHandler(existing)
            existing.close()

    only_libraries = _NotAServiceRecord()
    file_handler = _CommonFileHandler(log_directory / "common" / "alerts", ".log")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(DualTimezoneFormatter(APP_LOG_FORMAT))
    file_handler.addFilter(only_libraries)
    setattr(file_handler, _COMMON_MARK, True)

    console = StdoutHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(ConsoleTagFormatter("common"))
    console.addFilter(only_libraries)
    setattr(console, _COMMON_MARK, True)

    root = logging.getLogger()
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("uvicorn").addHandler(file_handler)  # Uvicorn's own handler already prints its lines
