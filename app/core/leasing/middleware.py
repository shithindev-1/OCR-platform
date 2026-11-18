"""HTTPS enforcement and per-client rate limiting for the OCR endpoints (/api/v1/ocr and below)."""

import logging
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.leasing.config import settings
from app.core.leasing.exceptions import OCR_PATH
from app.services.leasing.post_processing import error_response
from app.services.leasing.validation import enforce_rate_limit, request_identity

logger = logging.getLogger("uae_ocr")


def _is_ocr_route(path: str) -> bool:
    return path == OCR_PATH or path.startswith(f"{OCR_PATH}/")


def _is_job_poll(request: Request) -> bool:
    # A progress bar asks for a job's progress every second or two - reading it starts no new work
    # and costs nothing, so it must never use up the caller's request allowance.
    return request.method == "GET" and "/ocr/leasing/jobs/" in request.url.path


def _rejection(path: str, status_code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    if path != OCR_PATH:
        return JSONResponse(status_code=status_code, content={"detail": message}, headers=headers)
    # No multipart body has been parsed yet at this point (middleware runs before the
    # route handler), so there is no real documentName/filename to report.
    return JSONResponse(
        status_code=status_code,
        content=error_response(document_name="unknown", document_filename="unknown", message=message),
        headers=headers,
    )


class OcrSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_ocr_route(path):
            if settings.require_https and request.headers.get("x-forwarded-proto", request.url.scheme).lower() != "https":
                # An ERROR, so it is mailed: usually a proxy not passing X-Forwarded-Proto, which blocks every user.
                logger.error("Request rejected: not received over HTTPS path=%s forwarded_proto=%s",
                             path, request.headers.get("x-forwarded-proto") or "-")
                return _rejection(path, 400, "Please use a secure https:// address for this request.")
            if _is_job_poll(request):
                return await call_next(request)
            client_ip = request.client.host if request.client else "unknown"
            identity = request_identity(
                client_ip=client_ip,
                tenant_id=request.headers.get("x-tenant-id"),
                user_id=request.headers.get("x-user-id"),
            )
            try:
                enforce_rate_limit(identity, settings)
            except HTTPException as exc:
                return _rejection(path, exc.status_code, exc.detail, exc.headers)
            # One start and one end line for every OCR request, so each flow can be followed in the log
            started = time.perf_counter()
            logger.info("Request started method=%s path=%s client=%s", request.method, path, client_ip)
            response = await call_next(request)
            logger.info("Request finished method=%s path=%s status_code=%s duration_sec=%.2f",
                        request.method, path, response.status_code, time.perf_counter() - started)
            return response
        return await call_next(request)
