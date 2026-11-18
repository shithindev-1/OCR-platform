"""Global exception handlers — every error response is `{"error": "<general message>"}`."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.cheque.exceptions import AppError
from app.core.cheque.tracing import current_trace
from app.schemas.cheque.matching import CHEQUE_INFO_IN_PAYMENT, CHEQUE_INFO_IN_PAYMENT_MESSAGE

logger = logging.getLogger("cheque.api")


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _trace_error(kind: str, message: str, **details) -> None:
    trace = current_trace()
    if trace:
        trace.error(kind, message, **details)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("%s %s -> %s: %s", request.method, request.url.path,
                   type(exc).__name__, exc.message)
    _trace_error(type(exc).__name__, exc.message)
    return _error(exc.status_code, exc.message)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning("%s %s -> invalid request: %s", request.method, request.url.path, exc.errors())
    # Field path + reason only: the rejected input itself may be large or personal
    _trace_error("RequestValidationError", "Invalid request body",
                 fields=[{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()])
    if any(e.get("type") == CHEQUE_INFO_IN_PAYMENT for e in exc.errors()):
        return _error(422, CHEQUE_INFO_IN_PAYMENT_MESSAGE)
    if request.url.path.endswith("/match/upload"):
        return _error(422, "Invalid request. Send the cheque files in 'files', the payments as a "
                           "JSON list in 'payments' and, optionally, 'cheque_info' as a JSON object "
                           "(form-data).")
    if request.url.path.endswith("/match"):
        return _error(422, "Invalid request. Send 'file_urls', optional 'cheque_info' and "
                           "'payments' (each with Amount and a valid DueDate) as JSON.")
    return _error(422, "Invalid request. Upload one or more files using the 'files' field.")


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    _trace_error("HTTPException", str(exc.detail))
    return _error(exc.status_code, str(exc.detail))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("%s %s -> unhandled error", request.method, request.url.path)
    _trace_error(type(exc).__name__, str(exc)[:500])
    return _error(500, AppError.message)


# Exception class -> handler, mounted by the platform (app/core/errors.py).
ERROR_HANDLERS = {
    AppError: app_error_handler,
    RequestValidationError: validation_error_handler,
    StarletteHTTPException: http_error_handler,
    Exception: unhandled_error_handler,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Mounts Cheque's handlers on an app of its own (the platform mounts them by URL instead)."""
    for exc_type, handler in ERROR_HANDLERS.items():
        app.add_exception_handler(exc_type, handler)
