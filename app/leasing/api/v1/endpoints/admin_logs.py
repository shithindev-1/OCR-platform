"""GET /admin/logs - read the service's log files from anywhere (admin key only).

For watching a hosted server's logs from another machine (tools/watch_logs.py follows them live).
Off unless ADMIN_API_KEYS is set: the routes then answer 404 as if they did not exist. The key goes in
the X-Admin-Key header and is separate from API_KEYS, so a caller's API key can never read logs. Only
the named log files can be read (no paths), and never results.log (extracted ID data). Not in Swagger.

    GET /admin/logs/files                      -> the log files, their sizes and dated (rotated) copies
    GET /admin/logs?file=errors&lines=200      -> the last 200 lines, and `offset` (the file's size)
    GET /admin/logs?file=errors&after=<offset> -> only what was written since (for following live)
    GET /admin/logs?file=errors&date=2026-11-11 -> that day's rotated file
"""

import logging
import secrets
from datetime import date as Date
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.common.config import get_platform_settings
from app.leasing.core.config import settings

logger = logging.getLogger("uae_ocr")

router = APIRouter(prefix="/admin/logs", include_in_schema=False)

# The readable logs (file name without .log). results.log is never served: it can hold ID data.
LOG_FILES = (
    "alerts", "errors", "ocr-api", "document-results", "usage", "timing", "document-usage", "hits",
    "token-breakdown", "rotation", "service-stdout", "service-stderr",
)
_MAX_BYTES = 1_000_000  # per answer; a follower simply asks again from the new offset
_MAX_LINES = 5000


def log_directory() -> Path:
    # Leasing's own log folder in the merged project: <log_dir>/leasing (see app/leasing/service.py).
    return get_platform_settings().log_directory / "leasing"


def _require_admin(request: Request, key: str | None) -> None:
    keys = settings.admin_api_key_list
    if not keys:
        raise HTTPException(status_code=404, detail="Not Found")
    if settings.require_https and request.url.scheme != "https":
        raise HTTPException(status_code=400, detail="Please use a secure https:// address for this request.")
    if not key or not any(secrets.compare_digest(key, k) for k in keys):
        logger.warning("Admin log request rejected reason=invalid_admin_key path=%s", request.url.path)
        raise HTTPException(status_code=401, detail="Your admin key is missing or not valid.")


def _path(file: str, day: str | None) -> Path:
    if file not in LOG_FILES:
        raise HTTPException(status_code=400, detail=f"Unknown log '{file}'. Use one of: {', '.join(LOG_FILES)}.")
    name = f"{file}.log"
    if day:
        try:
            name += f".{Date.fromisoformat(day).isoformat()}"
        except ValueError:
            raise HTTPException(status_code=400, detail="Please give the date as YYYY-MM-DD.") from None
    return log_directory() / name


def _read(path: Path, start: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(min(size - start, _MAX_BYTES))


@router.get("/files")
def list_logs(request: Request, x_admin_key: str | None = Header(default=None)) -> dict:
    _require_admin(request, x_admin_key)
    files = []
    for file in LOG_FILES:
        for path in sorted(log_directory().glob(f"{file}.log*")):
            stat = path.stat()
            files.append({"file": file, "name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
    return {"files": files}


@router.get("")
def read_log(
    request: Request,
    file: str = Query("errors"),
    lines: int = Query(200, ge=1, le=_MAX_LINES),
    after: int | None = Query(None, ge=0),
    date: str | None = Query(None),
    x_admin_key: str | None = Header(default=None),
) -> dict:
    _require_admin(request, x_admin_key)
    path = _path(file, date)
    if not path.is_file():
        return {"file": file, "date": date, "exists": False, "size": 0, "offset": 0, "restarted": False, "lines": []}
    size = path.stat().st_size
    restarted = False
    if after is None:
        # The last `lines` lines: read the end of the file and keep whole lines only.
        start = max(0, size - _MAX_BYTES)
        text = _read(path, start, size).decode("utf-8", errors="replace")
        found = text.splitlines()
        if start > 0 and found:
            found = found[1:]  # the first one is cut in the middle
        return {"file": file, "date": date, "exists": True, "size": size, "offset": size,
                "restarted": False, "lines": found[-lines:]}
    if after > size:
        # The file was rotated at midnight (a new, shorter file): start it from the top.
        after, restarted = 0, True
    chunk = _read(path, after, size)
    end = chunk.rfind(b"\n")
    if end == -1 and len(chunk) < _MAX_BYTES:
        chunk = b""  # only half a line written so far: wait for the rest
    elif end != -1:
        chunk = chunk[: end + 1]
    return {"file": file, "date": date, "exists": True, "size": size, "offset": after + len(chunk),
            "restarted": restarted, "lines": chunk.decode("utf-8", errors="replace").splitlines()}
